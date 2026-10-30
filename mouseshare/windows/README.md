# Mousehare — Windows Server Agent

For end-user installation, use the Windows installer described in the project [README](../../README.md). The terminal workflow below is retained for contributors who need to debug the original agent directly.

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
An older Mac client ignores unknown message types, so it will not display this
confirmation until its receiver is updated.

For the Mac client to use the confirmation, add an `activation_ack` branch to
`mouseshare/mac/mac_app.py::_handle_incoming`. Treat `active: true` and
`cursor_positioned: true` as the server's confirmation; surface the failure
fields if cursor placement failed. The current Mac switches to
`WINDOWS_ACTIVE` optimistically in `switch_to_windows` and currently ignores
this message, so that client-side status behavior still needs to be aligned.

The Mac client also needs to use one coordinate unit consistently when it
computes and warps edge positions. Its current handoff path combines
`CGEventGetLocation` / `CGWarpMouseCursorPosition` global display coordinates
with `CGDisplayPixelsWide` / `CGDisplayPixelsHigh` pixel dimensions. Use
`CGDisplayBounds` dimensions for those global coordinates (or convert both
positions and dimensions to pixels consistently) in the edge detection,
fraction calculation, and return warp. The fraction message itself remains
dimensionless, so this correction does not require a protocol-version bump.

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
