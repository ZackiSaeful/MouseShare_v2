# Mousehare — Windows Server Agent

For end-user installation, use the Windows installer described in the project [README](../../README.md). The terminal workflow below is retained for contributors who need to debug the original agent directly.

## Overview
Windows Agent is the TCP server.  It receives keyboard and mouse events from
the Mac agent and injects them into Windows using pynput.
It also monitors the cursor: when it reaches the left edge, it signals Mac.

## Requirements
- Windows 10 / 11
- Python 3.10+ (install from python.org or Microsoft Store)

## Installation

```cmd
cd windows
py -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> **Note**: Do NOT use `.venv\Scripts\activate` in PowerShell if execution
> policy blocks scripts.  Use the `.exe` path directly instead.

## Configuration (config.json)

```json
{
    "host": "0.0.0.0",      // listen on all interfaces
    "port": 5555,
    "token": "CHANGE_ME",   // must match mac/config.json
    "edge_threshold": 3,    // pixels from left edge to trigger switch back
    "debug": true
}
```

Change `token` to match the same value in the Mac config.

## Running

```cmd
.venv\Scripts\python.exe windows_app.py
```

A circle icon appears in the system tray:
- **Gray**  — no Mac connected
- **Green** — Mac connected, Mac is active (Windows receives no remote input)
- **Blue**  — Mac connected, Windows is active (remote input being injected)

To quit: right-click tray icon → Quit.

---

## Firewall

Windows Firewall may block port 5555.
Allow it with (run as Administrator):

```cmd
netsh advfirewall firewall add rule name="MouseShare" dir=in action=allow protocol=TCP localport=5555
```

Or: Windows Security → Firewall → Allow an app → Add python.exe.

---

## Command Mapping

| Mac key | Windows key |
|---|---|
| Command | Ctrl |
| Option/Alt | Alt |
| Control | Ctrl |
| Shift | Shift |
| Cmd+C | Ctrl+C |
| Cmd+V | Ctrl+V |
| Cmd+Z | Ctrl+Z |
| Cmd+A | Ctrl+A |

Mac Command is transparently mapped to Windows Ctrl.
The translation happens in the Windows key handler via `SPECIAL_KEY_MAP`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Port already in use | Change `port` in both config files |
| Mac cannot connect | Check Windows Firewall, ensure agent is running |
| Keys injected but wrong characters | Check keyboard layout settings in Windows |
| pynput injection not working | Try running as Administrator |
| Tray icon doesn't appear | Make sure Pillow is installed correctly |
