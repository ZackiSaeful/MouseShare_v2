# Mousehare — macOS Client Agent

For end-user installation, use the macOS DMG described in the project [README](../../README.md). The terminal workflow below is retained for contributors who need to debug the original agent directly.

## Overview
Mac Agent captures keyboard and mouse via macOS CGEventTap.
When cursor hits the right screen edge, all input is forwarded to Windows.
When Windows signals "edge left", Mac resumes normal operation.

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
    "token": "CHANGE_ME",              // must match windows/config.json
    "edge_threshold": 3,               // pixels from right edge to trigger switch
    "debug": true
}
```

Change `windows_host` to your actual Windows PC's local IP.
Change `token` to a shared secret string — pick anything unique.

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
     ↓  (cursor reaches right edge)
CONNECTED + WINDOWS_ACTIVE
     ↓  (Windows detects cursor at left edge, sends edge=left)
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
| Token error in logs | Ensure both config.json files have the same `token` value |
