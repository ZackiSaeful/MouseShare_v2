"""Small Tk UI shared by the V3 macOS client and Windows receiver."""
from __future__ import annotations

import logging
import platform
import queue
import secrets
import tkinter as tk
from tkinter import messagebox, ttk

from .windows_receiver import WindowsReceiver, enable_dpi_awareness

log = logging.getLogger(__name__)


class MousehareWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.system = platform.system()
        self.messages: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.receiver = None
        self.controller = None
        root.title("Mousehare V3")
        root.geometry("590x430")
        root.minsize(540, 400)
        root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Mousehare V3", font=("TkDefaultFont", 21, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Secure mouse and keyboard sharing on your local network.",
                  wraplength=520).pack(anchor="w", pady=(4, 20))

        self.form = ttk.Frame(frame)
        self.form.pack(fill="x")
        if self.system == "Darwin":
            self._build_mac_form()
        elif self.system == "Windows":
            self._build_windows_form()
        else:
            ttk.Label(self.form, text="V3 runs on macOS and Windows.").pack(anchor="w")

        ttk.Separator(frame).pack(fill="x", pady=18)
        ttk.Label(frame, text="Status", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(frame, textvariable=self.status_var, wraplength=520, justify="left").pack(anchor="w", pady=(4, 0))
        self.root.after(100, self._drain_messages)

    def _build_mac_form(self):
        self.host_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.position_var = tk.StringVar(value="right")
        ttk.Label(self.form, text="Windows IP address").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(self.form, textvariable=self.host_var, width=32).grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(self.form, text="Pairing key").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(self.form, textvariable=self.token_var, width=32, show="•").grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(self.form, text="Windows is located").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Combobox(self.form, textvariable=self.position_var, values=("left", "right", "top", "bottom"),
                     state="readonly", width=29).grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=5)
        self.form.columnconfigure(1, weight=1)
        ttk.Label(self.form, text="The pairing key is shown in the Windows app. Both devices must run V3.",
                  wraplength=520).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 8))
        buttons = ttk.Frame(self.form)
        buttons.grid(row=4, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Button(buttons, text="Connect", command=self.connect).pack(side="left")
        ttk.Button(buttons, text="Disconnect", command=self.disconnect).pack(side="left", padx=8)

    def _build_windows_form(self):
        self.token = secrets.token_urlsafe(15)
        ttk.Label(self.form, text="This device is the receiver. Start sharing, then enter the IP and key on the Mac.",
                  wraplength=520).pack(anchor="w", pady=(0, 14))
        ttk.Label(self.form, text="Pairing key", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.token_var = tk.StringVar(value=self.token)
        ttk.Entry(self.form, textvariable=self.token_var, state="readonly", font=("TkFixedFont", 12)).pack(fill="x", pady=(5, 12))
        buttons = ttk.Frame(self.form)
        buttons.pack(anchor="w")
        ttk.Button(buttons, text="Start receiving", command=self.start_receiver).pack(side="left")
        ttk.Button(buttons, text="Copy key", command=self.copy_token).pack(side="left", padx=8)
        ttk.Label(self.form, text="TCP port 5555 · Allow Mousehare V3 through Windows Firewall on Private networks.",
                  wraplength=520).pack(anchor="w", pady=(12, 0))

    def connect(self):
        host, token = self.host_var.get().strip(), self.token_var.get().strip()
        if not host or not token:
            messagebox.showerror("Missing connection details", "Enter the Windows IP address and pairing key.", parent=self.root)
            return
        try:
            from .mac_controller import MacController
            self.disconnect()
            controller = MacController(host, 5555, token, self.position_var.get(), self.post_status)
            controller.start()
            self.controller = controller
        except (PermissionError, OSError, RuntimeError) as exc:
            self.post_status(str(exc))
            messagebox.showerror("Mousehare V3", str(exc), parent=self.root)
        except Exception as exc:
            log.exception("Could not start V3 controller")
            messagebox.showerror("Mousehare V3", str(exc), parent=self.root)

    def disconnect(self):
        controller, self.controller = self.controller, None
        if controller:
            controller.stop()
        self.post_status("Disconnected.")

    def start_receiver(self):
        if self.receiver:
            return
        try:
            self.receiver = WindowsReceiver(self.token, self.post_status)
            self.receiver.start()
        except Exception as exc:
            self.receiver = None
            log.exception("Could not start Windows receiver")
            messagebox.showerror("Mousehare V3", str(exc), parent=self.root)

    def copy_token(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.token)
        self.post_status("Pairing key copied.")

    def post_status(self, message: str):
        self.messages.put(str(message))

    def _drain_messages(self):
        while not self.messages.empty():
            try:
                self.status_var.set(self.messages.get_nowait())
            except queue.Empty:
                break
        self.root.after(100, self._drain_messages)

    def close(self):
        self.disconnect()
        if self.receiver:
            self.receiver.stop()
            self.receiver = None
        self.root.destroy()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    enable_dpi_awareness()
    root = tk.Tk()
    MousehareWindow(root)
    root.mainloop()
