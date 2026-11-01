# Mousehare — Windows Server Agent

For end-user installation, use the Windows installer described in the project [README](../../README.md). For contributors, build from the repository root on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1
```

For a self-signed local test build, pass `-DevelopmentCertificate`. For public
distribution, pass `-CertificateThumbprint` with a certificate from a trusted
code-signing provider. The installer is written to `release/`.

## Overview
Windows Agent is the TCP server.  It receives keyboard and mouse events from
the Mac agent and injects them into Windows using pynput.
It also monitors the edge facing the Mac and returns the pointer to the matching
Mac edge. The Mac's configured placement is exchanged during secure pairing.

## Handoff messages and position mapping

The authenticated control stream uses protocol v3: encrypted JSON messages are
carried in the existing sequence-checked TCP frames. A Mac-to-Windows
`activate_windows` message supplies `position` as a normalized fraction from
0.0 to 1.0 along the Mac edge. Windows maps that fraction to the configured
entry edge (`mac_position`) using its virtual-screen dimensions: vertical
positions for left/right, horizontal positions for top/bottom. If a
multi-monitor layout leaves a gap on that outer edge, Windows chooses the
nearest real monitor on the edge and reports the resulting fraction.
Protocol v3 carries no monitor identifier or display geometry, so it cannot
guarantee exact monitor-to-monitor alignment when either computer has multiple
displays; that would require a coordinated protocol extension.

After updating its active state and attempting cursor placement, Windows now
sends this encrypted server-to-client message:

```json
{
  "type": "activation_ack",
  "active": true,
  "cursor_positioned": true,
  "edge": "left",
  "position": 0.9
}
```

`cursor_positioned` is false if Windows could not position the pointer;
`position` is the effective normalized location (or the requested location if
placement failed). This is an additive protocol-v3 application message: the
handshake, encryption, frame layout, and existing message fields are unchanged.
Older Mac clients ignore unknown message types. The current Mac client has an
`activation_ack` branch in `mouseshare/mac/mac_app.py::_handle_incoming` and
uses `CGDisplayBounds` coordinates consistently for edge fractions and cursor
warps. The message remains additive to protocol v3.

On return, Windows derives the fraction from the cursor's actual exit
coordinate on the virtual desktop; it does not reuse a saved entry position.
`edge` remains the only Windows message that asks the Mac to resume control;
`activation_ack` only reports Windows' state. Injected Windows mouse events
are filtered from the physical edge detector, preventing remote motion from
generating a false return-to-Mac event.

## Requirements
- Windows 10 / 11
- Python 3.10+ (install from python.org or Microsoft Store)

## Installation

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
```

> **Note**: Do NOT use `.venv\Scripts\activate` in PowerShell if execution
> policy blocks scripts.  Use the `.exe` path directly instead.

## Configuration (config.json)

```json
{
    "host": "192.168.0.102", // bind only to the Windows LAN address
    "port": 5555,
    "token": "<random generated pairing key>",
    "edge_threshold": 3,    // pixels from the edge facing Mac
    "debug": true
}
```

Use the random pairing key generated in the Windows Mousehare app and copy it to
the Mac. The current app accepts any non-empty key while retaining protocol-v3
compatibility. New app-generated 22-character keys contain 128 bits of
randomness; use them instead of a short self-chosen phrase, which may be guessed
from a recorded pairing exchange. Existing longer keys remain valid.

Plain-text clipboard sharing is optional and off by default. Enable it under
Settings → Security on both computers and reconnect both sides. Only text up to
48 KiB is shared over the encrypted pairing; images and files are not shared.

## Running

```powershell
.venv\Scripts\python.exe mouseshare_app.py
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
netsh advfirewall firewall add rule name="MouseShare" dir=in action=allow protocol=TCP localport=5555 profile=private
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
