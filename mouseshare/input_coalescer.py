"""Ordered, bounded delivery for high-rate remote input events.

Mouse devices can generate hundreds of relative-motion events per second.  This
queue keeps the input callbacks off the networking and OS-injection paths,
while preserving the order of movements, clicks, keys, and scrolling.  Adjacent
motion events from one secure session are combined over a very short window.
"""
from __future__ import annotations

from collections import deque
import threading
import time
from collections.abc import Callable


MOUSE_MOVE_COALESCE_SECONDS = 0.004  # 250 Hz: low latency without packet bursts.
MAX_QUEUED_EVENTS = 1024
MAX_MOUSE_DELTA = 10_000
MAX_PENDING_MOUSE_DELTA = 100_000


class OrderedInputQueue:
    """Deliver input in order and coalesce only adjacent mouse movements.

    ``session`` is an opaque object with identity semantics.  It lets callers
    discard input that was captured for a connection which has since closed,
    instead of ever delivering it to a reconnected peer.
    """

    def __init__(
        self,
        deliver: Callable[[object, dict], bool],
        on_delivery_failed: Callable[[object], None] | None = None,
        *,
        coalesce_seconds: float = MOUSE_MOVE_COALESCE_SECONDS,
        max_events: int = MAX_QUEUED_EVENTS,
    ) -> None:
        self._deliver = deliver
        self._on_delivery_failed = on_delivery_failed
        self._coalesce_seconds = max(0.0, coalesce_seconds)
        self._max_events = max(1, max_events)
        self._condition = threading.Condition()
        self._events: deque[tuple[object, dict]] = deque()
        self._stopping = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="OrderedInputQueue",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._events.clear()
            self._condition.notify_all()
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)
        with self._condition:
            if self._thread is worker and (worker is None or not worker.is_alive()):
                self._thread = None

    def discard_session(self, session: object) -> None:
        """Drop work captured for a closed or superseded connection."""
        with self._condition:
            self._events = deque(
                (event_session, event)
                for event_session, event in self._events
                if event_session is not session
            )
            self._condition.notify_all()

    def enqueue(self, session: object, event: dict) -> bool:
        """Queue an input event without doing I/O on the caller's thread."""
        if session is None or not isinstance(event, dict):
            return False
        event_type = event.get("type")
        if event_type == "mouse_move":
            dx, dy = event.get("dx"), event.get("dy")
            if type(dx) is not int or type(dy) is not int or not (dx or dy):
                return False
            event = {"type": "mouse_move", "dx": dx, "dy": dy}
        else:
            # Input producers create the remaining event dictionaries.  Copy it
            # so a caller cannot mutate a queued event after returning.
            event = dict(event)

        with self._condition:
            if self._stopping or self._thread is None:
                return False
            if event_type == "mouse_move" and self._events:
                previous_session, previous = self._events[-1]
                if previous_session is session and previous.get("type") == "mouse_move":
                    combined_x = previous["dx"] + event["dx"]
                    combined_y = previous["dy"] + event["dy"]
                    # A broken or stalled connection must not let a single
                    # aggregate grow without bound.  Callers treat False as a
                    # failed connection and fall back to local control.
                    if (abs(combined_x) > MAX_PENDING_MOUSE_DELTA
                            or abs(combined_y) > MAX_PENDING_MOUSE_DELTA):
                        return False
                    previous["dx"] = combined_x
                    previous["dy"] = combined_y
                    self._condition.notify()
                    return True
            if len(self._events) >= self._max_events:
                return False
            self._events.append((session, event))
            self._condition.notify()
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._events and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return

                # Let the next movement collect more deltas for at most 4 ms.
                # If a click/key/scroll arrives after it, flush immediately so
                # that non-motion input never waits for the full window.
                if self._events[0][1].get("type") == "mouse_move":
                    deadline = time.monotonic() + self._coalesce_seconds
                    while not self._stopping:
                        if (len(self._events) > 1
                                and self._events[1][1].get("type") != "mouse_move"):
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._condition.wait(remaining)
                    if self._stopping:
                        return

                session, event = self._events.popleft()

            if event.get("type") == "mouse_move":
                delivered = self._deliver_movement(session, event)
            else:
                delivered = self._safe_deliver(session, event)
            if not delivered:
                self.discard_session(session)
                if self._on_delivery_failed is not None:
                    try:
                        self._on_delivery_failed(session)
                    except Exception:
                        # The connection owner is responsible for reporting its
                        # own failure; the queue must remain available to stop.
                        pass

    def _deliver_movement(self, session: object, event: dict) -> bool:
        """Split an unusual aggregate into receiver-safe relative deltas."""
        dx, dy = event["dx"], event["dy"]
        while dx or dy:
            step_x = max(-MAX_MOUSE_DELTA, min(MAX_MOUSE_DELTA, dx))
            step_y = max(-MAX_MOUSE_DELTA, min(MAX_MOUSE_DELTA, dy))
            if not self._safe_deliver(
                session,
                {"type": "mouse_move", "dx": step_x, "dy": step_y},
            ):
                return False
            dx -= step_x
            dy -= step_y
        return True

    def _safe_deliver(self, session: object, event: dict) -> bool:
        try:
            return bool(self._deliver(session, event))
        except Exception:
            return False
