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
    "Light": {
        "bg": "#EAE4D9", "panel": "#F6F1E8", "panel2": "#E8DECE",
        "line": "#BDA27D", "accent": "#A85A25", "accent_hi": "#C56D2D",
        "text": "#28231D", "muted": "#716251", "green": "#557F55",
        "red": "#A84335", "field": "#FCF9F2", "disabled": "#AA9D8C",
    },
}


def app_data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
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
        "server_token": secrets.token_urlsafe(32),
        "edge_threshold": 3,
        "theme": "TVA",
        "local_discovery": False,
        "debug": False,
    }


class MousehareApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.data_path = app_data_dir() / "settings.json"
        self.settings = self._load_settings()
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
        self.status_var = tk.StringVar(value="Disconnected · Ready to start")
        self.show_token = tk.BooleanVar(value=False)

        self.root.title("Mousehare — Device Link")
        self.root.geometry("1040x720")
        self.root.minsize(900, 650)
        self.root.configure(bg=self.palette["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_ttk()
        self._build_shell()
        self._show_page("connect")
        self.root.after(120, self._poll_events)

    def _load_settings(self) -> dict:
        values = default_settings()
        try:
            loaded = json.loads(self.data_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                values.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
        if not values.get("server_token") or values["server_token"] == "CHANGE_ME":
            values["server_token"] = secrets.token_urlsafe(32)
        return values

    def _save_settings(self) -> None:
        try:
            self.data_path.write_text(
                json.dumps(self.settings, indent=2), encoding="utf-8"
            )
            if sys.platform != "win32":
                os.chmod(self.data_path, 0o600)
        except OSError as exc:
            self._set_status("error", f"Could not save settings: {exc}")

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

        self.content = tk.Frame(shell, bg=p["bg"])
        self.content.pack(side="left", fill="both", expand=True, padx=22, pady=18)
        self._draw_status_bar()

    def _nav_button(self, page: str, label: str) -> None:
        p = self.palette
        active = page == self.current_page
        btn = tk.Button(self.sidebar, text=label, anchor="w", relief="flat", bd=0,
                        padx=16, pady=12, bg=p["accent"] if active else p["panel"],
                        fg="#17130F" if active else p["text"],
                        activebackground=p["accent_hi"],
                        activeforeground="#17130F", font=("Arial", 11, "bold" if active else "normal"),
                        command=lambda value=page: self._show_page(value))
        btn.pack(fill="x", padx=10, pady=3)

    def _draw_status_bar(self) -> None:
        p = self.palette
        self.status_bar = tk.Frame(self.content, bg=p["panel"],
                                   highlightbackground=p["line"], highlightthickness=1)
        self.status_bar.pack(side="bottom", fill="x", pady=(14, 0))
        self.status_dot = tk.Canvas(self.status_bar, width=16, height=16, bg=p["panel"],
                                    highlightthickness=0)
        self.status_dot.pack(side="left", padx=(14, 8), pady=12)
        self.status_dot.create_oval(3, 3, 13, 13, fill=p["accent"], outline="")
        self.status_label = tk.Label(self.status_bar, textvariable=self.status_var,
                                     bg=p["panel"], fg=p["muted"],
                                     font=("Arial", 9), wraplength=490,
                                     justify="left")
        self.status_label.pack(side="left", pady=11)
        tk.Label(self.status_bar, text="TCP / JSON FRAMED", bg=p["panel"], fg=p["muted"],
                 font=("Courier New", 8)).pack(side="right", padx=14)

    def _show_page(self, page: str) -> None:
        self.current_page = page
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
                 font=("Arial", 10)).pack(anchor="w", pady=(3, 14))

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
        p = self.palette
        bg = p["accent"] if primary else p["panel2"]
        fg = "#18130F" if primary else p["text"]
        button = tk.Button(parent, text=text, command=command, relief="flat", bd=0,
                           bg=bg, fg=fg, activebackground=p["accent_hi"],
                           activeforeground="#18130F", disabledforeground=p["disabled"],
                           padx=16, pady=10, cursor="hand2", font=("Arial", 10, "bold"))
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

    def _render_connect(self) -> None:
        p = self.palette
        role = "Mac → Windows" if ROLE == "client" else "Windows receiver"
        self._title("Device Link", f"{role} · Secure pairing on your local network")

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
                 font=("Arial", 9)).pack(anchor="w", padx=18, pady=(0, 10))

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
            self.token_entry = self._entry(fields, self.token_var, show="•")
            self.token_entry.grid(row=1, column=2, sticky="ew", pady=(5, 0), ipady=8)
        else:
            self.ip_var.set(self.local_ip)
            tk.Label(fields, text="Local IP", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 9)).grid(row=0, column=0, sticky="w", padx=(0, 8))
            tk.Label(fields, text="Port", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 9)).grid(row=0, column=1, sticky="w", padx=(0, 8))
            tk.Label(fields, text="Pairing Key", bg=p["panel"], fg=p["muted"],
                     font=("Arial", 9)).grid(row=0, column=2, sticky="w")
            self._entry(fields, self.ip_var, state="readonly").grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(5, 0), ipady=8)
            self._entry(fields, self.port_var, width=8).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(5, 0), ipady=8)
            self.token_entry = self._entry(fields, self.token_var, show="•")
            self.token_entry.grid(row=1, column=2, sticky="ew", pady=(5, 0), ipady=8)
            self.token_var.set(self.settings["server_token"])
        tk.Checkbutton(fields, text="Show pairing key", variable=self.show_token,
                       command=self._refresh_token_visibility, bg=p["panel"], fg=p["muted"],
                       selectcolor=p["field"], activebackground=p["panel"],
                       activeforeground=p["text"], font=("Arial", 8)).grid(
                           row=2, column=2, sticky="e", pady=(5, 0))

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
            tk.Label(details_inner,
                     text="Move the pointer to the right edge of this Mac to hand off control.",
                     bg=p["panel"], fg=p["muted"], font=("Arial", 9)).pack(anchor="w", pady=(10, 0))
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
                     text="Keep this panel open while sharing. Allow Mousehare through Windows Firewall on Private networks.",
                     bg=p["panel"], fg=p["muted"], font=("Arial", 9)).pack(anchor="w", pady=(10, 0))
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
        cy = height // 2
        left = (int(width * .20), cy)
        right = (int(width * .80), cy)
        hub = (int(width * .50), cy)
        canvas.create_line(left[0] + 34, cy, hub[0] - 25, cy, fill=p["accent"], width=2)
        canvas.create_line(hub[0] + 25, cy, right[0] - 34, cy, fill=p["accent"], width=2)
        canvas.create_oval(hub[0] - 30, cy - 30, hub[0] + 30, cy + 30,
                           outline=p["line"], width=1)
        canvas.create_oval(hub[0] - 20, cy - 20, hub[0] + 20, cy + 20,
                           fill=p["panel2"], outline=p["accent"], width=2)
        canvas.create_text(*hub, text="M", fill=p["accent_hi"], font=("Arial", 15, "bold"))
        for point, label, caption in ((left, "THIS COMPUTER", "Mac controller" if ROLE == "client" else "Windows host"),
                                      (right, "PEER COMPUTER", "Windows receiver" if ROLE == "client" else "Mac controller")):
            x, y = point
            canvas.create_rectangle(x - 36, y - 26, x + 36, y + 17, outline=p["accent"], width=2)
            canvas.create_line(x - 46, y + 23, x + 46, y + 23, fill=p["accent"], width=2)
            canvas.create_line(x, y + 17, x, y + 23, fill=p["accent"], width=2)
            canvas.create_text(x, y + 42, text=label, fill=p["text"], font=("Arial", 8, "bold"))
            canvas.create_text(x, y + 57, text=caption, fill=p["muted"], font=("Arial", 8))

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
            tk.Label(wrap, text="The server creates a random pairing key. Copy it to the Mac client. The current protocol authenticates with this key but does not encrypt traffic.",
                     bg=p["panel"], fg=p["muted"], font=("Arial", 9),
                     wraplength=520, justify="left").pack(anchor="w", pady=(10, 16))
            if ROLE == "server":
                self.settings_token_var = tk.StringVar(value=self.settings["server_token"])
                self._entry(wrap, self.settings_token_var, width=44).pack(anchor="w", ipady=7)
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
                                 values=("TVA", "Dark", "Light"), state="readonly",
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
        self._save_settings()
        self._set_status("disconnected", f"Default port saved: {port}.")

    def _save_network_settings(self) -> None:
        try:
            threshold = int(self.threshold_var.get())
            if not 1 <= threshold <= 20:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid threshold", "Enter a value from 1 to 20 pixels.", parent=self.root)
            return
        self.settings["edge_threshold"] = threshold
        self._save_settings()
        self._set_status("disconnected", f"Edge threshold saved: {threshold} pixels.")

    def _regenerate_token(self) -> None:
        if self.backend_active:
            messagebox.showwarning("Stop server first", "Disconnect before rotating the pairing key.", parent=self.root)
            return
        self.settings["server_token"] = secrets.token_urlsafe(32)
        self.settings_token_var.set(self.settings["server_token"])
        self.token_var.set(self.settings["server_token"])
        self._save_settings()
        self._set_status("disconnected", "A new pairing key was generated.")

    def _save_theme(self) -> None:
        self.settings["theme"] = self.theme_var.get()
        self._save_settings()
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
        tk.Label(self.content, text="Mousehare uses the existing 4-byte length-prefixed JSON protocol over TCP. Default port: 5555.",
                 bg=p["bg"], fg=p["muted"], font=("Arial", 9), wraplength=650,
                 justify="left").pack(anchor="w", pady=6)

    def _refresh_token_visibility(self) -> None:
        token_entry = getattr(self, "token_entry", None)
        if token_entry is not None:
            token_entry.configure(show="" if self.show_token.get() else "•")

    def _validate_and_config(self) -> dict | None:
        try:
            port = int(self.port_var.get())
            if not 1 <= port <= 65535:
                raise ValueError("Port must be between 1 and 65535.")
        except ValueError:
            messagebox.showerror("Invalid port", "Enter a port from 1 to 65535.", parent=self.root)
            return None
        token = self.token_var.get().strip()
        if ROLE == "client":
            host = self.ip_var.get().strip()
            try:
                ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError:
                messagebox.showerror("Invalid address", "Enter a valid IPv4 address for the Windows server.", parent=self.root)
                return None
            if not token:
                messagebox.showerror("Pairing key required", "Copy the pairing key from the Windows server.", parent=self.root)
                return None
            self.settings["client_ip"] = host
            self.settings["client_port"] = port
            self.settings["client_token"] = token
            return {"windows_host": host, "port": port, "token": token,
                    "edge_threshold": int(self.settings.get("edge_threshold", 3)), "debug": False}
        if not token:
            messagebox.showerror("Pairing key required", "Generate a pairing key in Settings → Security.", parent=self.root)
            return None
        self.settings["server_port"] = port
        self.settings["server_token"] = token
        return {"host": self.local_ip, "port": port, "token": token,
                "edge_threshold": int(self.settings.get("edge_threshold", 3)), "debug": False}

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
        self._save_settings()
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
