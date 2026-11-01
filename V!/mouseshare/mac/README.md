# Mousehare — macOS Client Agent

For end-user installation, use the macOS DMG described in the project [README](../../README.md). The terminal workflow below is retained for contributors who need to debug the original agent directly.

## Overview
Mac Agent captures keyboard and mouse via macOS CGEventTap. Its edge is selected
by the saved Mac position relative to Windows. Pairing uses an authenticated
ephemeral key exchange, then encrypts and sequence-checks control messages.
Clipboard sharing is optional, plain-text only, and off by default. If enabled
on both computers, Mousehare reads and writes the macOS general pasteboard while
the secure connection is active. macOS may show its clipboard privacy prompt.

## Requirements
- macOS 12+ (Monterey or later recommended)
- Python 3.10+
- Two macOS privacy permissions (see below)

## Installation

```bash
cd mac
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Configuration (config.json)

```json
{
    "windows_host": "192.168.0.101",   // IP of your Windows PC
    "port": 5555,
    "token": "<server-generated pairing key>",
    "mac_position": "left",           // left, right, top, or bottom of Windows
    "edge_threshold": 3,               // pixels from the selected edge
    "debug": true
}
```

Change `windows_host` to your actual Windows PC's local IP.
Copy the random pairing key from the Windows Mousehare app. The current app
accepts any non-empty key and remains compatible with established protocol-v3
Windows servers. Newly generated 22-character keys contain 128 bits of
randomness; use them instead of a short self-chosen phrase, which may be guessed
from a recorded pairing exchange. A short key requires the current Windows build.

## Running

```bash
source .venv/bin/activate
python mac_app.py
```

A circle icon appears in the menu bar:
- **Gray**  — not connected to Windows
- **Green** — connected, Mac is active
- **Blue**  — connected, Windows is active (your input goes to Windows)

To quit: click the menu bar icon → Quit.

---

## macOS Permissions (REQUIRED)

The app intercepts keyboard and mouse at the system level.
macOS requires explicit permission for this.

### 1. Accessibility
`System Settings → Privacy & Security → Accessibility`
Add **Terminal** (or your Python app) and toggle it **ON**.

### 2. Input Monitoring
`System Settings → Privacy & Security → Input Monitoring`
Add **Terminal** (or your Python app) and toggle it **ON**.

> **If you run from a virtual environment**, the app process may appear as
> `/usr/bin/python3` or the venv Python binary.  Add whichever binary appears
> in the prompt when you type `which python` inside the venv.
>
> After granting permissions, **restart** mac_app.py.

If the event tap fails (no keyboard interception), you will see in the terminal:
```
[FATAL] CGEventTapCreate returned None.
```
This always means a missing permission.

---

## State Machine

```
DISCONNECTED
     ↓  (TCP connect + handshake OK)
CONNECTED + MAC_ACTIVE
     ↓  (cursor reaches the edge facing Windows)
CONNECTED + WINDOWS_ACTIVE
     ↓  (Windows detects the edge facing Mac and sends its side)
CONNECTED + MAC_ACTIVE
     ...
```
On any disconnect: immediately resets to MAC_ACTIVE, restores cursor.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `CGEventTapCreate returned None` | Grant Accessibility + Input Monitoring |
| Cursor stuck invisible after crash | Run: `python -c "import Quartz; Quartz.CGAssociateMouseAndMouseCursorPosition(True); Quartz.CGDisplayShowCursor(Quartz.CGMainDisplayID())"` |
| Cannot connect to Windows | Check `windows_host` IP, Windows firewall, and that windows_app.py is running |
| Typing appears in both Mac and Windows | Event tap not active — check permissions |
| Pairing key rejected | Copy the current key from the Windows app; regenerate it there if it may have been exposed |
