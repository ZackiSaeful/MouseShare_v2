#!/usr/bin/env python3
"""Mousehare desktop control panel.

The existing wire protocol is intentionally retained: macOS is the input
client and Windows is the receiving server. The UI calls those real agents
in-process so permissions apply to the signed application and shutdown can
restore the local cursor cleanly.
"""
from __future__ import annotations

import ipaddress
import json
import os
import queue
import secrets
import socket
import sys
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

if sys.platform == "darwin":
    from mouseshare.mac import mac_app as agent
    ROLE = "client"
elif sys.platform == "win32":
    from mouseshare.windows import windows_app as agent
    ROLE = "server"
else:
    raise RuntimeError("Mousehare currently supports macOS and Windows only.")


PORT_DEFAULT = 5555  # Existing framed TCP protocol uses port 5555.
APP_NAME = "Mousehare"
MAC_POSITIONS = ("left", "right", "top", "bottom")

PALETTES = {
    "TVA": {
        "bg": "#12110F", "panel": "#1C1915", "panel2": "#242019",
        "line": "#63401F", "accent": "#D87932", "accent_hi": "#F19A4D",
        "text": "#F0DEC5", "muted": "#B29A7A", "green": "#87A77A",
        "red": "#CE6F5D", "field": "#171512", "disabled": "#514638",
    },
    "Dark": {
        "bg": "#16181A", "panel": "#222528", "panel2": "#2A2F32",
        "line": "#4D5558", "accent": "#D28A48", "accent_hi": "#F0B26E",
        "text": "#E9E6DF", "muted": "#A5A8A4", "green": "#82A888",
        "red": "#CF7771", "field": "#1A1D1F", "disabled": "#515558",
    },
    "Graphite": {
        "bg": "#171819", "panel": "#202225", "panel2": "#292C30",
        "line": "#51463A", "accent": "#CD7C39", "accent_hi": "#F0AE69",
        "text": "#F1E1CD", "muted": "#B9A48C", "green": "#8DAF88",
        "red": "#D17A6B", "field": "#151618", "disabled": "#595148",
    },
}


def app_data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform != "win32":
        os.chmod(path, 0o700)
    return path


def get_local_ipv4() -> str:
    """Return the address selected for the current LAN default route."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 1))
        ip = probe.getsockname()[0]
        return ip if not ip.startswith("127.") else "127.0.0.1"
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def default_settings() -> dict:
    return {
        "client_ip": "",
        "client_port": PORT_DEFAULT,
        "server_port": PORT_DEFAULT,
        "client_token": "",
        "server_token": secrets.token_urlsafe(16),
        "edge_threshold": 3,
        "mac_position": "left",
        "theme": "TVA",
        "local_discovery": False,
        "clipboard_sync": False,
        "debug": False,
    }


class MousehareApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.data_path = app_data_dir() / "settings.json"
        self.settings = self._load_settings()
        if self.settings.get("mac_position") not in MAC_POSITIONS:
            self.settings["mac_position"] = "left"
        self.palette = PALETTES.get(self.settings.get("theme"), PALETTES["TVA"])
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.backend_active = False
        self.connection_status = "disconnected"
        self.current_page = "connect"
        self.settings_section = "General"
        self.local_ip = get_local_ipv4()
        self.ip_var = tk.StringVar(value=self.settings.get("client_ip", ""))
        self.port_var = tk.StringVar(value=str(self._current_port()))
        self.token_var = tk.StringVar(value=self._current_token())
        self.mac_position_var = tk.StringVar(
            value=self.settings.get("mac_position", "left"))
        self.status_var = tk.StringVar(value="Disconnected · Ready to start")
        self.show_token = tk.BooleanVar(value=False)
        self.mac_position_selector = None
        self.network_mac_position_selector = None
        self.mac_position_hint = None

        self.root.title("Mousehare — Device Link")
        self.root.geometry("1040x720")
        self.root.minsize(720, 500)
        self.root.configure(bg=self.palette["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_ttk()
        self._build_shell()
        self._show_page("connect")
        self.root.after(120, self._poll_events)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<Button-4>", self._on_mousewheel)
        self.root.bind_all("<Button-5>", self._on_mousewheel)

    def _load_settings(self) -> dict:
        values = default_settings()
        try:
            loaded = json.loads(self.data_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                values.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
        if not values.get("server_token") or values["server_token"] == "CHANGE_ME":
            values["server_token"] = secrets.token_urlsafe(16)
        if not isinstance(values.get("server_token"), str) or not values["server_token"]:
            values["server_token"] = secrets.token_urlsafe(16)
        if not isinstance(values.get("client_token"), str) or \
                not values["client_token"].strip():
            # Earlier builds saved the placeholder "CHANGE_ME" as the client
            # key. It is intentionally no longer accepted by secure pairing.
            # Do not invent a replacement: the Mac must use the Windows
            # server's actual key. Clearing it makes the recovery path clear.
            values["client_token"] = ""
        # Older builds offered a white "Light" theme. Migrate it to the
        # dark neutral alternative so amber text stays legible on every screen.
        if values.get("theme") == "Light":
            values["theme"] = "Graphite"
        if values.get("theme") not in PALETTES:
            values["theme"] = "TVA"
        return values

    def _save_settings(self) -> bool:
        temp_path = self.data_path.with_name(self.data_path.name + ".tmp")
        try:
            mode = 0o666 if sys.platform == "win32" else 0o600
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(self.settings, indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            if sys.platform != "win32":
                os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.data_path)
            return True
        except OSError as exc:
            self._set_status("error", f"Could not save settings: {exc}")
            return False

    def _current_port(self) -> int:
        key = "client_port" if ROLE == "client" else "server_port"
        try:
            return int(self.settings.get(key, PORT_DEFAULT))
        except (ValueError, TypeError):
            return PORT_DEFAULT

    def _current_token(self) -> str:
        key = "client_token" if ROLE == "client" else "server_token"
        return str(self.settings.get(key, ""))

    def _configure_ttk(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        p = self.palette
        style.configure("Mousehare.TCombobox", fieldbackground=p["field"],
                        background=p["panel2"], foreground=p["text"],
                        arrowcolor=p["accent"], bordercolor=p["line"],
                        lightcolor=p["line"], darkcolor=p["line"], padding=7)
        style.map("Mousehare.TCombobox", fieldbackground=[("readonly", p["field"])],
                  foreground=[("readonly", p["text"])])
        style.configure("Mousehare.Readonly.TEntry", fieldbackground=p["field"],
                        foreground=p["text"], insertcolor=p["text"],
                        bordercolor=p["line"], lightcolor=p["line"],
                        darkcolor=p["line"], padding=8, relief="flat")
        style.map("Mousehare.Readonly.TEntry",
                  fieldbackground=[("readonly", p["field"])],
                  foreground=[("readonly", p["text"])])
        style.configure("Mousehare.Vertical.TScrollbar", background=p["panel2"],
                        troughcolor=p["bg"], bordercolor=p["line"],
                        arrowcolor=p["muted"], lightcolor=p["panel2"],
                        darkcolor=p["line"], width=12)
        style.map("Mousehare.Vertical.TScrollbar",
                  background=[("active", p["accent"])])
        style.configure(
            "Mousehare.TButton",
            background=p["panel2"], foreground=p["text"],
            bordercolor=p["line"], lightcolor=p["panel2"],
            darkcolor=p["line"], focuscolor=p["accent"],
            padding=(16, 10), relief="flat",
            font=("Arial", 10, "bold"),
        )
        style.map(
            "Mousehare.TButton",
            background=[("disabled", p["panel"]), ("pressed", p["line"]),
                        ("active", p["line"])],
            foreground=[("disabled", p["disabled"]), ("active", p["text"])],
        )
        style.configure(
            "Mousehare.Primary.TButton",
            background=p["accent"], foreground="#18130F",
            bordercolor=p["accent"], lightcolor=p["accent"],
            darkcolor=p["accent"], focuscolor=p["accent_hi"],
            padding=(16, 10), relief="flat",
            font=("Arial", 10, "bold"),
        )
        style.map(
            "Mousehare.Primary.TButton",
            background=[("disabled", p["panel2"]), ("pressed", p["accent_hi"]),
                        ("active", p["accent_hi"])],
            foreground=[("disabled", p["disabled"]), ("active", "#18130F")],
        )
        style.configure(
            "Mousehare.Nav.TButton",
            background=p["panel"], foreground=p["text"],
            bordercolor=p["panel"], lightcolor=p["panel"],
            darkcolor=p["panel"], padding=(14, 12), anchor="w",
            relief="flat", font=("Arial", 11),
        )
        style.map(
            "Mousehare.Nav.TButton",
            background=[("pressed", p["line"]), ("active", p["panel2"])],
            foreground=[("active", p["text"])],
        )
        style.configure(
            "Mousehare.NavSelected.TButton",
            background=p["accent"], foreground="#18130F",
            bordercolor=p["accent"], lightcolor=p["accent"],
            darkcolor=p["accent"], padding=(14, 12), anchor="w",
            relief="flat", font=("Arial", 11, "bold"),
        )
        style.map(
            "Mousehare.NavSelected.TButton",
            background=[("pressed", p["accent_hi"]), ("active", p["accent_hi"])],
            foreground=[("active", "#18130F")],
        )
        style.configure(
            "Mousehare.Small.TButton",
            background=p["panel2"], foreground=p["text"],
            bordercolor=p["line"], lightcolor=p["panel2"],
            darkcolor=p["line"], padding=(10, 4), relief="flat",
            font=("Arial", 8, "bold"),
        )
        style.map(
            "Mousehare.Small.TButton",
            background=[("pressed", p["line"]), ("active", p["line"])],
            foreground=[("active", p["text"])],
        )

    def _build_shell(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()
        p = self.palette
        shell = tk.Frame(self.root, bg=p["bg"])
        shell.pack(fill="both", expand=True)
        self.sidebar = tk.Frame(shell, width=190, bg=p["panel"],
                                highlightbackground=p["line"], highlightthickness=1)
        self.sidebar.pack(side="left", fill="y", padx=(14, 0), pady=14)
        self.sidebar.pack_propagate(False)
        logo = tk.Canvas(self.sidebar, width=48, height=48, bg=p["panel"],
                         highlightthickness=0)
        logo.pack(anchor="w", padx=18, pady=(20, 8))
        logo.create_oval(4, 4, 44, 44, outline=p["accent"], width=2)
        logo.create_line(24, 8, 24, 40, fill=p["accent"], width=2)
        logo.create_line(10, 24, 38, 24, fill=p["line"], width=1)
        logo.create_oval(18, 18, 30, 30, fill=p["accent"], outline="")
        tk.Label(self.sidebar, text="MOUSEHARE", bg=p["panel"], fg=p["text"],
                 font=("Arial", 13, "bold")).pack(anchor="w", padx=18)
        tk.Label(self.sidebar, text="DEVICE LINK / TVA SERIES", bg=p["panel"],
                 fg=p["muted"], font=("Arial", 8)).pack(anchor="w", padx=18, pady=(2, 28))

        for name, label in (("connect", "Connect"), ("settings", "Settings"), ("about", "About")):
            self._nav_button(name, label)

        tk.Frame(self.sidebar, height=1, bg=p["line"]).pack(fill="x", padx=16, pady=(25, 12))
        role_text = "MAC CLIENT" if ROLE == "client" else "WINDOWS SERVER"
        tk.Label(self.sidebar, text="ACTIVE PLATFORM", bg=p["panel"], fg=p["muted"],
                 font=("Arial", 8, "bold")).pack(anchor="w", padx=18)
        tk.Label(self.sidebar, text=role_text, bg=p["panel"], fg=p["accent_hi"],
                 font=("Courier New", 10, "bold")).pack(anchor="w", padx=18, pady=(6, 0))
        tk.Label(self.sidebar, text="Mouse and keyboard\nshare over your LAN", bg=p["panel"],
                 fg=p["muted"], justify="left", font=("Arial", 9)).pack(
                     side="bottom", anchor="w", padx=18, pady=20)

        self.content_area = tk.Frame(shell, bg=p["bg"])
        self.content_area.pack(side="left", fill="both", expand=True, padx=22, pady=18)
        self._draw_status_bar()

        self.page_scrollbar = ttk.Scrollbar(
            self.content_area, orient="vertical", style="Mousehare.Vertical.TScrollbar"
        )
        self.page_scrollbar.pack(side="right", fill="y")
        self.page_canvas = tk.Canvas(
            self.content_area, bg=p["bg"], highlightthickness=0,
            yscrollcommand=self.page_scrollbar.set,
        )
        self.page_canvas.pack(side="left", fill="both", expand=True)
        self.page_scrollbar.configure(command=self.page_canvas.yview)

        self.content = tk.Frame(self.page_canvas, bg=p["bg"])
        self.page_window = self.page_canvas.create_window(
            (0, 0), window=self.content, anchor="nw"
        )
        self.content.bind("<Configure>", self._update_page_scrollregion)
        self.page_canvas.bind("<Configure>", self._resize_page_content)

    def _nav_button(self, page: str, label: str) -> None:
        active = page == self.current_page
        btn = ttk.Button(
            self.sidebar, text=label,
            style="Mousehare.NavSelected.TButton" if active else "Mousehare.Nav.TButton",
            command=lambda value=page: self._show_page(value),
        )
        btn.pack(fill="x", padx=10, pady=3)

    def _draw_status_bar(self) -> None:
        p = self.palette
        self.status_bar = tk.Frame(self.content_area, bg=p["panel"],
                                   highlightbackground=p["line"], highlightthickness=1)
        self.status_bar.pack(side="bottom", fill="x", pady=(14, 0))
        self.status_dot = tk.Canvas(self.status_bar, width=16, height=16, bg=p["panel"],
                                    highlightthickness=0)
        self.status_dot.pack(side="left", padx=(14, 8), pady=12)
        self.status_dot.create_oval(3, 3, 13, 13, fill=p["accent"], outline="")
        self.status_label = tk.Label(self.status_bar, textvariable=self.status_var,
                                     bg=p["panel"], fg=p["muted"],
                                     font=("Arial", 9), wraplength=280,
                                     justify="left")
        self.status_label.pack(side="left", pady=11)
        tk.Label(self.status_bar, text="AES-GCM / TCP", bg=p["panel"], fg=p["muted"],
                 font=("Courier New", 8)).pack(side="right", padx=14)

    def _update_page_scrollregion(self, _event=None) -> None:
        self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all"))

    def _resize_page_content(self, event) -> None:
        self.page_canvas.itemconfigure(self.page_window, width=event.width)

    def _on_mousewheel(self, event):
        if getattr(event, "num", None) == 4:
            direction, steps = -1, 1
        elif getattr(event, "num", None) == 5:
            direction, steps = 1, 1
        else:
            delta = int(getattr(event, "delta", 0))
            if not delta:
                return "break"
            direction = -1 if delta > 0 else 1
            steps = max(1, abs(delta) // 120)
        self.page_canvas.yview_scroll(direction * steps * 3, "units")
        return "break"

    def _show_page(self, page: str) -> None:
        self.current_page = page
        self.mac_position_selector = None
        self.network_mac_position_selector = None
        self.mac_position_hint = None
        for child in self.root.winfo_children():
            child.destroy()
        self._build_shell()
        if page == "connect":
            self._render_connect()
        elif page == "settings":
            self._render_settings()
        else:
            self._render_about()
        self._refresh_status_visual()

    def _title(self, title: str, subtitle: str) -> None:
        p = self.palette
        tk.Label(self.content, text=title.upper(), bg=p["bg"], fg=p["text"],
                 font=("Arial", 20, "bold")).pack(anchor="w")
        tk.Label(self.content, text=subtitle, bg=p["bg"], fg=p["muted"],
                 font=("Arial", 10), wraplength=520, justify="left").pack(
                     anchor="w", pady=(3, 14))

    def _panel(self, parent, **pack_options):
        p = self.palette
        frame = tk.Frame(parent, bg=p["panel"], highlightbackground=p["line"],
                         highlightthickness=1)
        frame.pack(**pack_options)
        return frame

    def _section_label(self, parent, text: str) -> None:
        p = self.palette
        tk.Label(parent, text=text.upper(), bg=p["panel"], fg=p["muted"],
                 font=("Arial", 8, "bold")).pack(anchor="w")

    def _button(self, parent, text, command, primary=False, **pack_options):
        button = ttk.Button(
            parent, text=text, command=command,
            style="Mousehare.Primary.TButton" if primary else "Mousehare.TButton",
            cursor="hand2",
        )
        button.pack(**pack_options)
        return button

    def _entry(self, parent, variable, width=24, show=None, state="normal"):
        p = self.palette
        entry = tk.Entry(parent, textvariable=variable, width=width, show=show,
                         state=state, bg=p["field"], fg=p["text"], insertbackground=p["accent"],
                         disabledbackground=p["field"], disabledforeground=p["muted"],
                         relief="flat", highlightthickness=1, highlightbackground=p["line"],
                         highlightcolor=p["accent"], font=("Courier New", 11))
        return entry

    def _readonly_entry(self, parent, variable, width=24):
        return ttk.Entry(parent, textvariable=variable, width=width,
                         state="readonly", style="Mousehare.Readonly.TEntry",
                         font=("Courier New", 11))

    def _render_connect(self) -> None:
        p = self.palette
        role = "Mac → Windows" if ROLE == "client" else "Windows receiver"
        self._title("Device Link", f"{role} · Pairing key on your local network")

        visual = self._panel(self.content, fill="x", pady=(0, 13))
        self._draw_link_visual(visual)

        mode_panel = self._panel(self.content, fill="x", pady=(0, 13))
        row = tk.Frame(mode_panel, bg=p["panel"])
        row.pack(fill="x", padx=13, pady=12)
        self._mode_card(row, "Server", "Share this computer", ROLE == "server",
                        enabled=ROLE == "server").pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._mode_card(row, "Client", "Connect to another", ROLE == "client",
                        enabled=ROLE == "client").pack(side="left", fill="x", expand=True, padx=(6, 0))
        # The original input-capture protocol is intentionally platform-specific.
        # Explain the companion roles instead of presenting a fake selectable mode.
        note = "This Mac is the controller. Install Mousehare Server on the Windows computer." if ROLE == "client" else \
               "This PC receives input. Connect from Mousehare Client on the Mac."
        tk.Label(mode_panel, text=note, bg=p["panel"], fg=p["muted"],
                 font=("Arial", 9), wraplength=520, justify="left").pack(
                     anchor="w", padx=18, pady=(0, 10))

        details = self._panel(self.content, fill="x")
        details_inner = tk.Frame(details, bg=p["panel"])
        details_inner.pack(fill="x", padx=18, pady=15)
        self._section_label(details_inner, "Connection Details")
        fields = tk.Frame(details_inner, bg=p["panel"])
        fields.pack(fill="x", pady=(12, 10))
        fields.columnconfigure(0, weight=3)
        fields.columnconfigure(1, weight=1)
        fields.columnconfigure(2, weight=2)

        if ROLE == "client":
            tk.Label(fields, text="Destination IP", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 9)).grid(row=0, column=0, sticky="w", padx=(0, 8))
            tk.Label(fields, text="Port", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 9)).grid(row=0, column=1, sticky="w", padx=(0, 8))
            tk.Label(fields, text="Pairing Key", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 9)).grid(row=0, column=2, sticky="w")
            self._entry(fields, self.ip_var).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(5, 0), ipady=8)
            self._entry(fields, self.port_var, width=8).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(5, 0), ipady=8)
            self.token_entry = self._entry(fields, self.token_var, show="*")
            self.token_entry.grid(row=1, column=2, sticky="ew", pady=(5, 0), ipady=8)
        else:
            self.ip_var.set(self.local_ip)
            tk.Label(fields, text="Local IP", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 9)).grid(row=0, column=0, sticky="w", padx=(0, 8))
            tk.Label(fields, text="Port", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 9)).grid(row=0, column=1, sticky="w", padx=(0, 8))
            tk.Label(fields, text="Pairing Key", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 9)).grid(row=0, column=2, sticky="w")
            self._readonly_entry(fields, self.ip_var).grid(
                row=1, column=0, sticky="ew", padx=(0, 8), pady=(5, 0), ipady=8
            )
            self._entry(fields, self.port_var, width=8).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(5, 0), ipady=8)
            self.token_entry = self._entry(fields, self.token_var, show="*", state="readonly")
            self.token_entry.grid(row=1, column=2, sticky="ew", pady=(5, 0), ipady=8)
            self.token_var.set(self.settings["server_token"])
        self.show_token_button = ttk.Button(
            fields, text="Show", command=self._toggle_token_visibility,
            style="Mousehare.Small.TButton", cursor="hand2",
        )
        self.show_token_button.grid(row=2, column=2, sticky="e", pady=(5, 0))

        if ROLE == "client":
            pairing_actions = tk.Frame(details_inner, bg=p["panel"])
            pairing_actions.pack(fill="x", pady=(0, 8))
            self._button(pairing_actions, "Paste connection details",
                         self._paste_connection_details, side="left", padx=0, pady=0)
            tk.Label(
                pairing_actions,
                text="On Windows, choose Copy connection details first.",
                bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                wraplength=330, justify="left",
            ).pack(side="left", padx=(10, 0))

            position_row = tk.Frame(details_inner, bg=p["panel"])
            position_row.pack(fill="x", pady=(0, 10))
            tk.Label(position_row, text="Mac position relative to Windows",
                     bg=p["panel"], fg=p["muted"], font=("Arial", 9)).pack(
                         side="left", padx=(0, 10))
            self.mac_position_selector = ttk.Combobox(
                position_row, textvariable=self.mac_position_var,
                values=MAC_POSITIONS, state="readonly",
                style="Mousehare.TCombobox", width=10)
            self.mac_position_selector.pack(side="left")
            self.mac_position_selector.bind(
                "<<ComboboxSelected>>", lambda _event: self._save_mac_position())
            self.mac_position_hint = tk.Label(
                details_inner, text=self._mac_position_summary(),
                bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                wraplength=520, justify="left")
            self.mac_position_hint.pack(anchor="w", pady=(0, 10))

        controls = tk.Frame(details_inner, bg=p["panel"])
        controls.pack(fill="x")
        self.primary_button = self._button(
            controls, "Connect" if ROLE == "client" else "Start Server",
            self._start_or_retry, primary=True, side="left", fill="x", expand=True,
        )
        self.disconnect_button = self._button(
            controls, "Disconnect", self._disconnect, side="left", padx=(9, 0)
        )
        self._sync_connection_controls()

        self._refresh_token_visibility()
        if ROLE == "client":
            permissions = tk.Frame(details_inner, bg=p["panel"])
            permissions.pack(anchor="w", pady=(8, 0))
            self._button(permissions, "Accessibility settings",
                         lambda: self._open_macos_privacy("Privacy_Accessibility"),
                         side="left", padx=9, pady=6)
            self._button(permissions, "Input Monitoring settings",
                         lambda: self._open_macos_privacy("Privacy_ListenEvent"),
                         side="left", padx=(8, 9), pady=6)
        else:
            tk.Label(details_inner,
                     text="The paired Mac selects the return edge. Allow Mousehare through Windows Firewall on Private networks only.",
                     bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                     wraplength=520, justify="left").pack(anchor="w", pady=(10, 0))
            self._button(details_inner, "Copy connection details", self._copy_connection_details,
                         anchor="w", pady=(8, 0))

    def _mode_card(self, parent, title, subtitle, selected, enabled=True):
        p = self.palette
        bg = p["accent"] if selected else (p["panel2"] if enabled else p["panel"])
        fg = "#18130F" if selected else (p["text"] if enabled else p["disabled"])
        dim = "#4B2E17" if selected else (p["muted"] if enabled else p["disabled"])
        frame = tk.Frame(parent, bg=bg, highlightbackground=p["accent"] if selected else p["line"],
                         highlightthickness=1)
        tk.Label(frame, text=title.upper(), bg=bg, fg=fg,
                 font=("Arial", 11, "bold")).pack(anchor="w", padx=13, pady=(9, 0))
        tk.Label(frame, text=subtitle, bg=bg, fg=dim,
                 font=("Arial", 9)).pack(anchor="w", padx=13, pady=(3, 9))
        return frame

    def _draw_link_visual(self, parent) -> None:
        p = self.palette
        canvas = tk.Canvas(parent, height=168, bg=p["panel"], highlightthickness=0)
        self.link_canvas = canvas
        canvas.pack(fill="x", padx=8, pady=8)
        canvas.bind("<Configure>", lambda event: self._paint_link(canvas, event.width, event.height))
        self._paint_link(canvas, 780, 168)

    def _paint_link(self, canvas, width, height) -> None:
        p = self.palette
        canvas.delete("all")
        for x in range(0, width, 28):
            canvas.create_line(x, 0, x, height, fill=p["panel2"], width=1)
        for y in range(0, height, 28):
            canvas.create_line(0, y, width, y, fill=p["panel2"], width=1)

        position = self.settings.get("mac_position", "left")
        if ROLE == "server":
            position = getattr(agent, "CONFIG", {}).get("mac_position", position)
        if position not in MAC_POSITIONS:
            position = "left"
        hub = (int(width * .50), height // 2)
        if position in ("left", "right"):
            mac = (int(width * (.20 if position == "left" else .80)), hub[1])
            windows = (int(width * (.80 if position == "left" else .20)), hub[1])
        else:
            mac = (hub[0], int(height * (.25 if position == "top" else .75)))
            windows = (hub[0], int(height * (.75 if position == "top" else .25)))
        local, peer = (mac, windows) if ROLE == "client" else (windows, mac)

        for point in (local, peer):
            if abs(point[0] - hub[0]) >= abs(point[1] - hub[1]):
                sign = 1 if point[0] < hub[0] else -1
                canvas.create_line(point[0] + 38 * sign, point[1],
                                   hub[0] - 27 * sign, hub[1],
                                   fill=p["accent"], width=2)
            else:
                sign = 1 if point[1] < hub[1] else -1
                canvas.create_line(point[0], point[1] + 25 * sign,
                                   hub[0], hub[1] - 27 * sign,
                                   fill=p["accent"], width=2)

        canvas.create_oval(hub[0] - 30, hub[1] - 30, hub[0] + 30, hub[1] + 30,
                           outline=p["line"], width=1)
        canvas.create_oval(hub[0] - 20, hub[1] - 20, hub[0] + 20, hub[1] + 20,
                           fill=p["panel2"], outline=p["accent"], width=2)
        canvas.create_text(*hub, text="M", fill=p["accent_hi"], font=("Arial", 15, "bold"))
        for point, label in ((local, "MAC CONTROLLER" if ROLE == "client" else "WINDOWS HOST"),
                             (peer, "WINDOWS HOST" if ROLE == "client" else "MAC CONTROLLER")):
            x, y = point
            canvas.create_rectangle(x - 29, y - 20, x + 29, y + 14,
                                    outline=p["accent"], width=2)
            canvas.create_line(x - 38, y + 19, x + 38, y + 19,
                               fill=p["accent"], width=2)
            canvas.create_line(x, y + 14, x, y + 19, fill=p["accent"], width=2)
            if position in ("left", "right"):
                canvas.create_text(x, y + 35, text=label, fill=p["text"],
                                   font=("Arial", 8, "bold"))
            else:
                label_x = min(width - 70, x + 48)
                canvas.create_text(label_x, y - 5, text=label, anchor="w",
                                   fill=p["text"], font=("Arial", 8, "bold"))
                canvas.create_text(label_x, y + 9, text=f"Mac {position} of Windows",
                                   anchor="w", fill=p["muted"], font=("Arial", 8))

    def _render_settings(self) -> None:
        p = self.palette
        self._title("Settings", "Network, pairing and appearance preferences")
        body = tk.Frame(self.content, bg=p["bg"])
        body.pack(fill="both", expand=True)
        nav = tk.Frame(body, bg=p["panel"], highlightbackground=p["line"], highlightthickness=1)
        nav.pack(side="left", fill="y", padx=(0, 12))
        for section in ("General", "Network", "Security", "Appearance"):
            self._button(nav, section, lambda value=section: self._set_settings_section(value),
                         primary=section == self.settings_section, fill="x", padx=8, pady=4)
        self.settings_content = tk.Frame(body, bg=p["panel"], highlightbackground=p["line"], highlightthickness=1)
        self.settings_content.pack(side="left", fill="both", expand=True)
        self._draw_settings_section()

    def _set_settings_section(self, section: str) -> None:
        self.settings_section = section
        self._show_page("settings")

    def _draw_settings_section(self) -> None:
        p = self.palette
        for child in self.settings_content.winfo_children():
            child.destroy()
        wrap = tk.Frame(self.settings_content, bg=p["panel"])
        wrap.pack(fill="both", expand=True, padx=22, pady=20)
        tk.Label(wrap, text=self.settings_section, bg=p["panel"], fg=p["text"],
                 font=("Arial", 15, "bold")).pack(anchor="w", pady=(0, 16))
        if self.settings_section == "General":
            tk.Label(wrap, text="Default port", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 10)).pack(anchor="w")
            self.settings_port_var = tk.StringVar(value=str(self._current_port()))
            self._entry(wrap, self.settings_port_var, width=12).pack(anchor="w", pady=(5, 12), ipady=7)
            self._button(wrap, "Save default port", self._save_general_settings,
                         primary=True, anchor="w")
            tk.Label(wrap, text="Mousehare's existing TCP protocol uses port 5555 by default.",
                     bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                     wraplength=500, justify="left").pack(anchor="w", pady=(14, 0))
        elif self.settings_section == "Network":
            tk.Label(wrap, text="Manual IP and port configuration stays available on every build.",
                     bg=p["panel"], fg=p["text"], font=("Arial", 10),
                     wraplength=520, justify="left").pack(anchor="w")
            if ROLE == "client":
                tk.Label(wrap, text="Mac position relative to Windows", bg=p["panel"],
                         fg=p["muted"], font=("Arial", 10)).pack(anchor="w", pady=(16, 0))
                self.network_mac_position_selector = ttk.Combobox(
                    wrap, textvariable=self.mac_position_var, values=MAC_POSITIONS,
                    state="readonly", style="Mousehare.TCombobox", width=18)
                self.network_mac_position_selector.pack(anchor="w", pady=(5, 0))
                self.network_mac_position_selector.bind(
                    "<<ComboboxSelected>>", lambda _event: self._save_mac_position())
                tk.Label(wrap,
                         text="This chooses the touching edges. For example, Mac left means Mac right → Windows left, and Windows left → Mac right. Changes are saved immediately.",
                         bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                         wraplength=520, justify="left").pack(anchor="w", pady=(6, 0))
            else:
                tk.Label(wrap, text="The Mac sets its position during secure pairing. The Windows cursor enters on the edge facing that Mac.",
                         bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                         wraplength=520, justify="left").pack(anchor="w", pady=(16, 0))
            tk.Label(wrap, text="Edge handoff threshold (pixels)", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 10)).pack(anchor="w", pady=(18, 0))
            self.threshold_var = tk.StringVar(value=str(self.settings.get("edge_threshold", 3)))
            self._entry(wrap, self.threshold_var, width=12).pack(anchor="w", pady=(5, 10), ipady=7)
            self._button(wrap, "Save network settings", self._save_network_settings,
                         primary=True, anchor="w")
            tk.Label(wrap, text="Automatic discovery is not implemented by the current protocol. Enter the peer's LAN IP directly.",
                     bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                     wraplength=520, justify="left").pack(anchor="w")
            tk.Label(wrap, text=f"Current LAN address: {self.local_ip}", bg=p["panel"],
                     fg=p["accent_hi"], font=("Courier New", 10)).pack(anchor="w", pady=(18, 0))
        elif self.settings_section == "Security":
            tk.Label(wrap, text="Pair only with a computer you trust on the same local network.",
                     bg=p["panel"], fg=p["text"], font=("Arial", 10),
                     wraplength=520, justify="left").pack(anchor="w")
            tk.Label(wrap, text="Mousehare accepts any non-empty pairing key. New keys are generated as 22 characters from 128 bits of randomness. A short, self-chosen key can be guessed from a recorded pairing exchange, so use the generated key whenever possible. Mouse and keyboard messages are encrypted and replay-protected. Keep the key private and do not forward the server port to the public internet.",
                     bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                     wraplength=520, justify="left").pack(anchor="w", pady=(10, 16))
            tk.Label(wrap, text="The key is remembered in this user's settings. Anyone with access to this user account can read it; regenerate the key if it may have been exposed.",
                     bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                     wraplength=520, justify="left").pack(anchor="w", pady=(0, 14))
            self.clipboard_sync_var = tk.BooleanVar(
                value=bool(self.settings.get("clipboard_sync", False)))
            clipboard_toggle = tk.Checkbutton(
                wrap, text="Share plain-text clipboard with the paired computer",
                variable=self.clipboard_sync_var, bg=p["panel"], fg=p["text"],
                selectcolor=p["field"], activebackground=p["panel"],
                activeforeground=p["text"], disabledforeground=p["disabled"],
                font=("Arial", 10), state="disabled" if self.backend_active else "normal",
            )
            clipboard_toggle.pack(anchor="w", pady=(0, 5))
            tk.Label(
                wrap,
                text="Optional and off by default. Only plain text up to 48 KiB is shared, and only when both computers enable it. Connecting does not send existing clipboard contents. Clipboard text may contain passwords or private information. macOS may show a clipboard access prompt. Disconnect before changing this setting, then reconnect both computers.",
                bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                wraplength=520, justify="left",
            ).pack(anchor="w", pady=(0, 9))
            self._button(wrap, "Save clipboard preference",
                         self._save_clipboard_preference,
                         primary=True, anchor="w", pady=(0, 16))
            if ROLE == "server":
                self.settings_token_var = tk.StringVar(value=self.settings["server_token"])
                self.settings_key_entry = self._entry(
                    wrap, self.settings_token_var, width=44, show="*", state="readonly")
                self.settings_key_entry.pack(anchor="w", ipady=7)
                self.settings_key_show = tk.BooleanVar(value=False)
                tk.Checkbutton(wrap, text="Show pairing key", variable=self.settings_key_show,
                               command=self._toggle_settings_key, bg=p["panel"], fg=p["muted"],
                               selectcolor=p["field"], activebackground=p["panel"],
                               activeforeground=p["text"], font=("Arial", 9)).pack(anchor="w", pady=(5, 0))
                self._button(wrap, "Regenerate pairing key", self._regenerate_token,
                             primary=True, anchor="w", pady=(12, 0))
            else:
                tk.Label(wrap, text="Enter the pairing key shown by the Windows server on the Connect screen.",
                         bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                         wraplength=520, justify="left").pack(anchor="w")
        else:
            tk.Label(wrap, text="Theme", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 10)).pack(anchor="w")
            self.theme_var = tk.StringVar(value=self.settings.get("theme", "TVA"))
            theme = ttk.Combobox(wrap, textvariable=self.theme_var,
                                 values=("TVA", "Dark", "Graphite"), state="readonly",
                                 style="Mousehare.TCombobox", width=20)
            theme.pack(anchor="w", pady=(6, 12))
            theme.bind("<<ComboboxSelected>>", lambda _event: self._save_theme())
            tk.Label(wrap, text="Warm amber, bronze and cream colors are tuned for the TVA control-panel style.",
                     bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                     wraplength=520, justify="left").pack(anchor="w")

    def _save_general_settings(self) -> None:
        try:
            port = int(self.settings_port_var.get())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid port", "Enter a port from 1 to 65535.", parent=self.root)
            return
        key = "client_port" if ROLE == "client" else "server_port"
        self.settings[key] = port
        self.port_var.set(str(port))
        if not self._save_settings():
            return
        self._set_status("disconnected", f"Default port saved: {port}.")

    def _save_network_settings(self) -> None:
        try:
            threshold = int(self.threshold_var.get())
            if not 1 <= threshold <= 20:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid threshold", "Enter a value from 1 to 20 pixels.", parent=self.root)
            return
        position = None
        if ROLE == "client":
            position = self.mac_position_var.get()
            if position not in MAC_POSITIONS:
                messagebox.showerror("Invalid position", "Choose left, right, top, or bottom.", parent=self.root)
                return
        self.settings["edge_threshold"] = threshold
        if position is not None:
            self.settings["mac_position"] = position
        if not self._save_settings():
            return
        note = " Disconnect and reconnect to apply them." if self.backend_active else ""
        state = self.connection_status if self.backend_active else "disconnected"
        self._set_status(state, f"Network settings saved; edge threshold {threshold} pixels.{note}")

    def _mac_position_summary(self) -> str:
        position = self.mac_position_var.get()
        if position not in MAC_POSITIONS:
            position = self.settings.get("mac_position", "left")
        relative_position = {"left": "left of", "right": "right of",
                             "top": "above", "bottom": "below"}[position]
        mac_exit_edge = {"left": "right", "right": "left",
                         "top": "bottom", "bottom": "top"}[position]
        return (f"Mac is {relative_position} Windows: Mac {mac_exit_edge} edge → "
                f"Windows {position} edge. Return through Windows {position} edge → "
                f"Mac {mac_exit_edge} edge.")

    def _save_mac_position(self) -> None:
        if ROLE != "client":
            return
        position = self.mac_position_var.get()
        if position not in MAC_POSITIONS:
            return
        previous = self.settings.get("mac_position", "left")
        self.settings["mac_position"] = position
        if not self._save_settings():
            self.settings["mac_position"] = previous
            self.mac_position_var.set(previous)
            return
        hint = self.mac_position_hint
        if hint is not None and hint.winfo_exists():
            hint.configure(text=self._mac_position_summary())
        self._set_status("disconnected", f"Position saved. {self._mac_position_summary()}")

    def _regenerate_token(self) -> None:
        if self.backend_active:
            messagebox.showwarning("Stop server first", "Disconnect before rotating the pairing key.", parent=self.root)
            return
        previous_token = self.settings["server_token"]
        self.settings["server_token"] = secrets.token_urlsafe(16)
        self.settings_token_var.set(self.settings["server_token"])
        self.token_var.set(self.settings["server_token"])
        if not self._save_settings():
            self.settings["server_token"] = previous_token
            self.settings_token_var.set(previous_token)
            self.token_var.set(previous_token)
            return
        self._set_status("disconnected", "A new pairing key was generated.")

    def _save_clipboard_preference(self) -> None:
        if self.backend_active:
            messagebox.showwarning(
                "Disconnect first",
                "Disconnect Mousehare before changing clipboard sharing.",
                parent=self.root,
            )
            return
        previous = bool(self.settings.get("clipboard_sync", False))
        self.settings["clipboard_sync"] = bool(self.clipboard_sync_var.get())
        if not self._save_settings():
            self.settings["clipboard_sync"] = previous
            self.clipboard_sync_var.set(previous)
            return
        enabled = self.settings["clipboard_sync"]
        message = ("Plain-text clipboard sharing is enabled locally; the peer must also enable it."
                   if enabled else "Plain-text clipboard sharing is disabled.")
        self._set_status("disconnected", message)

    def _save_theme(self) -> None:
        self.settings["theme"] = self.theme_var.get()
        if not self._save_settings():
            return
        self.palette = PALETTES[self.settings["theme"]]
        self._configure_ttk()
        self._show_page("settings")

    def _open_macos_privacy(self, section: str) -> None:
        if sys.platform != "darwin":
            return
        url = f"x-apple.systempreferences:com.apple.preference.security?{section}"
        try:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            subprocess.Popen(["open", "-b", "com.apple.systempreferences"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _render_about(self) -> None:
        p = self.palette
        self._title("About Mousehare", "A practical device-link instrument")
        card = self._panel(self.content, fill="x", pady=(0, 12))
        tk.Label(card, text="MOUSEHARE", bg=p["panel"], fg=p["accent_hi"],
                 font=("Arial", 18, "bold")).pack(anchor="w", padx=20, pady=(18, 5))
        tk.Label(card, text="Share keyboard and mouse input between a Mac and a Windows PC over a trusted local network.",
                 bg=p["panel"], fg=p["text"], font=("Arial", 10), wraplength=600,
                 justify="left").pack(anchor="w", padx=20, pady=(0, 18))
        info = "macOS needs Accessibility and Input Monitoring permission for global input capture. Windows may ask to allow Mousehare on Private networks in Windows Firewall."
        tk.Label(self.content, text=info, bg=p["bg"], fg=p["muted"], font=("Arial", 10),
                 wraplength=650, justify="left").pack(anchor="w", pady=6)
        tk.Label(self.content, text="Mousehare uses 4-byte framed TCP transport with encrypted, sequence-checked messages. Default port: 5555.",
                 bg=p["bg"], fg=p["muted"], font=("Arial", 9), wraplength=650,
                 justify="left").pack(anchor="w", pady=6)

    def _refresh_token_visibility(self) -> None:
        token_entry = getattr(self, "token_entry", None)
        if token_entry is not None:
            token_entry.configure(show="" if self.show_token.get() else "*")
        show_button = getattr(self, "show_token_button", None)
        if show_button is not None:
            show_button.configure(text="Hide" if self.show_token.get() else "Show")

    def _toggle_token_visibility(self) -> None:
        self.show_token.set(not self.show_token.get())
        self._refresh_token_visibility()

    def _toggle_settings_key(self) -> None:
        entry = getattr(self, "settings_key_entry", None)
        if entry is not None:
            entry.configure(show="" if self.settings_key_show.get() else "*")

    @staticmethod
    def _parse_connection_details(value: str) -> tuple[str, int, str] | None:
        """Parse the two-line value copied by the Windows server UI."""
        if not isinstance(value, str):
            return None
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if len(lines) != 2:
            return None
        endpoint, token = lines
        host, separator, port_text = endpoint.rpartition(":")
        if not separator:
            return None
        try:
            ipaddress.IPv4Address(host)
            port = int(port_text)
        except (ipaddress.AddressValueError, ValueError):
            return None
        if not 1 <= port <= 65535:
            return None
        return host, port, token

    def _paste_connection_details(self) -> None:
        """Paste a Windows-generated connection bundle without showing its key."""
        if ROLE != "client":
            return
        try:
            clipboard_text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showerror(
                "Clipboard unavailable",
                "Copy the connection details from the Windows Mousehare app, then try again.",
                parent=self.root,
            )
            return
        details = self._parse_connection_details(clipboard_text)
        if details is not None:
            host, port, token = details
            self.ip_var.set(host)
            self.port_var.set(str(port))
            self.token_var.set(token)
            self._set_status("disconnected", "Connection details pasted. Review them, then connect.")
            return

        token = clipboard_text.strip() if isinstance(clipboard_text, str) else ""
        if not token:
            messagebox.showerror(
                "No pairing key found",
                "Copy the connection details or pairing key from the Windows Mousehare app.",
                parent=self.root,
            )
            return
        self.token_var.set(token)
        self._set_status("disconnected", "Pairing key pasted. Enter the Windows IP address and port, then connect.")

    def _validate_and_config(self) -> dict | None:
        token = self.token_var.get().strip()
        if not token:
            messagebox.showerror(
                "Pairing key required",
                "On Windows, choose Copy connection details. Then use Paste connection details here, or paste the full pairing key.",
                parent=self.root,
            )
            return None
        try:
            port = int(self.port_var.get())
            if not 1 <= port <= 65535:
                raise ValueError("Port must be between 1 and 65535.")
        except ValueError:
            messagebox.showerror("Invalid port", "Enter a port from 1 to 65535.", parent=self.root)
            return None
        if ROLE == "client":
            host = self.ip_var.get().strip()
            try:
                ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError:
                messagebox.showerror("Invalid address", "Enter a valid IPv4 address for the Windows server.", parent=self.root)
                return None
            self.settings["client_ip"] = host
            self.settings["client_port"] = port
            self.settings["client_token"] = token
            return {"windows_host": host, "port": port, "token": token,
                    "edge_threshold": int(self.settings.get("edge_threshold", 3)),
                    "mac_position": self.settings.get("mac_position", "left"),
                    "clipboard_sync": bool(self.settings.get("clipboard_sync", False)),
                    "debug": False}
        self.settings["server_port"] = port
        self.settings["server_token"] = token
        return {"host": self.local_ip, "port": port, "token": token,
                "edge_threshold": int(self.settings.get("edge_threshold", 3)),
                "mac_position": self.settings.get("mac_position", "left"),
                "clipboard_sync": bool(self.settings.get("clipboard_sync", False)),
                "debug": False}

    def _copy_connection_details(self) -> None:
        details = f"{self.local_ip}:{self.port_var.get()}\n{self.token_var.get()}"
        self.root.clipboard_clear()
        self.root.clipboard_append(details)
        self._set_status("disconnected", "LAN address, port and pairing key copied.")

    def _start_or_retry(self) -> None:
        if self.backend_active:
            if self.connection_status == "error" and ROLE == "client":
                self._disconnect()
                self.root.after(180, self._start_or_retry)
            return
        config = self._validate_and_config()
        if config is None:
            return
        if not self._save_settings():
            return
        self.backend_active = True
        self.disconnect_button.configure(state="normal")
        self.primary_button.configure(state="disabled")
        self._set_status("connecting", "Starting Mousehare backend…")
        try:
            agent.start_backend(config, lambda status, message: self.events.put((status, message)))
        except Exception as exc:
            self.backend_active = False
            self.primary_button.configure(state="normal")
            self.disconnect_button.configure(state="disabled")
            self._set_status("error", f"Could not start backend: {exc}")

    def _disconnect(self) -> None:
        if self.backend_active:
            try:
                agent.stop_backend()
            except Exception as exc:
                self._set_status("error", f"Backend shutdown error: {exc}")
            self.backend_active = False
        if hasattr(self, "primary_button"):
            self.primary_button.configure(state="normal")
        if hasattr(self, "disconnect_button"):
            self.disconnect_button.configure(state="disabled")
        self._set_status("disconnected", "Disconnected.")

    def _poll_events(self) -> None:
        while True:
            try:
                status, message = self.events.get_nowait()
            except queue.Empty:
                break
            if status == "error" and ROLE == "server":
                self.backend_active = False
            self._set_status(status, message)
            if status == "connected" and ROLE == "server" \
                    and hasattr(self, "link_canvas") and self.link_canvas.winfo_exists():
                self._paint_link(
                    self.link_canvas, self.link_canvas.winfo_width(),
                    self.link_canvas.winfo_height())
        self.root.after(120, self._poll_events)

    def _set_status(self, status: str, message: str) -> None:
        self.connection_status = status
        title = {"connected": "Connected", "connecting": "Connecting",
                 "error": "Error", "disconnected": "Disconnected"}.get(status, "Disconnected")
        self.status_var.set(f"{title} · {message}")
        self._refresh_status_visual()
        self._sync_connection_controls()

    def _sync_connection_controls(self) -> None:
        primary = getattr(self, "primary_button", None)
        disconnect = getattr(self, "disconnect_button", None)
        position_state = "disabled" if self.backend_active else "readonly"
        for selector in (self.mac_position_selector,
                         self.network_mac_position_selector):
            if selector is not None and selector.winfo_exists():
                selector.configure(state=position_state)
        if primary is None or not primary.winfo_exists():
            return
        idle_label = "Connect" if ROLE == "client" else "Start Server"
        if not self.backend_active:
            primary.configure(state="normal", text=idle_label)
            if disconnect is not None and disconnect.winfo_exists():
                disconnect.configure(state="disabled")
        else:
            if disconnect is not None and disconnect.winfo_exists():
                disconnect.configure(state="normal")
            if ROLE == "client" and self.connection_status == "error":
                primary.configure(state="normal", text="Retry Connection")
            else:
                label = "Connected" if self.connection_status == "connected" else (
                    "Server Running" if ROLE == "server" else "Connecting…"
                )
                primary.configure(state="disabled", text=label)

    def _refresh_status_visual(self) -> None:
        if not hasattr(self, "status_dot") or not self.status_dot.winfo_exists():
            return
        p = self.palette
        color = {"connected": p["green"], "error": p["red"],
                 "connecting": p["accent_hi"], "disconnected": p["accent"]}.get(
                     self.connection_status, p["accent"])
        self.status_dot.delete("all")
        self.status_dot.create_oval(3, 3, 13, 13, fill=color, outline="")
        self.status_label.configure(fg=color if self.connection_status != "disconnected" else p["muted"])

    def close(self) -> None:
        self._disconnect()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MousehareApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
