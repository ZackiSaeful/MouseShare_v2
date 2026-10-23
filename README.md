# Mousehare

Mousehare links a Mac keyboard and mouse to a Windows computer on the same trusted local network. The application has a native desktop window, remembers its settings, and starts the existing input-sharing backend when you connect.

The current wire protocol assigns the **Mac build the Client role** and the **Windows build the Server role**. This reflects the source implementation: macOS uses Quartz event taps to suppress and forward local input, while Windows receives and injects input. The role cards in the app show the supported role for that build; the opposite role is not presented as a working option.

## End-user installation

### macOS

1. Open `Mousehare-macos-arm64.dmg`.
2. Drag `Mousehare.app` to Applications.
3. Open Mousehare and enter the Windows server's LAN IP, port, and pairing key.
4. If macOS requests permission, enable Mousehare in **System Settings → Privacy & Security → Accessibility** and **Input Monitoring**, then quit and reopen the app.

### Windows 10/11

1. Run `Mousehare-Setup-1.0.0.exe` and follow the installer.
2. Start Mousehare from the Start menu or optional desktop shortcut.
3. Start the server. Copy the displayed LAN address, port, and pairing key into the Mac client.
4. If Windows Firewall asks, allow Mousehare on **Private networks**.

The apps do not require a terminal, Python installation, or administrator access for normal operation. Windows input injection cannot control secure desktop prompts or elevated applications when Mousehare itself is not elevated.

## Pairing and network security

- Both computers should be on a trusted local network. The Windows server binds to the active local IPv4 address and the Mac client connects to that address.
- The default port is **5555**. Change it in Settings on the server and use the same value on the client.
- New Windows server keys are random 22-character values with 128 bits of randomness. Share the key only with the intended Mac and use Settings → Security to rotate it. The current app accepts any non-empty pairing key. Use generated keys: a short, self-chosen key may be guessed from a recorded pairing exchange. A short key also requires the current Mousehare build on Windows.
- The key is retained in the per-user settings file so the connection can be remembered. On macOS the file is owner-readable only; Windows uses the current user's profile permissions. A process with access to that user account can read the key, so rotate it if exposed.
- Pairing uses a challenge-response over ephemeral X25519 keys. Control messages are encrypted with AES-GCM, authenticated, and sequence-checked to reject replay or tampering. The pairing key is not sent over the network.
- A paired computer can still control the other computer's mouse and keyboard by design. Pair only with a computer you trust; do not forward the server port to the public internet. The app does not protect against a compromised endpoint or someone who has the pairing key.
- Plain-text clipboard synchronization is optional and off by default. Turn it on under Settings → Security on both computers, then disconnect and reconnect both sides. It shares only plain text up to 48 KiB over the encrypted channel; images, files, and existing clipboard contents are not sent when a connection starts. Clipboard text can contain passwords or private data, so enable it only on computers and networks you trust.
- The macOS app remains compatible with Windows servers using the established protocol-v3 format. To use an unrestricted pairing-key length, update the Windows app too.
- On the Mac, choose the Mac's position relative to Windows on Connect or under Settings → Network. The Mac exits on the edge facing Windows; Windows enters and returns through its matching edge.
- Discovery is unavailable in this protocol, so manual IP and port configuration remains the connection path.
- Closing the app stops its backend. Disconnecting the Mac also restores its local cursor and mouse association.

Settings are stored per user in `~/Library/Application Support/Mousehare/settings.json` on macOS and `%APPDATA%\Mousehare\settings.json` on Windows.

## Development

The app uses Python's built-in Tkinter UI. Platform input handling stays in the existing agents:

- `mouseshare/mac/mac_app.py`: Quartz event capture, position-aware handoff, TCP client and reconnect loop.
- `mouseshare/windows/windows_app.py`: TCP server, pynput injection and position-aware return-edge monitoring.
- `mouseshare/secure_transport.py`: pairing handshake, ephemeral key exchange, AES-GCM encryption, and replay protection.
- `mouseshare/clipboard_sync.py`: opt-in plain-text clipboard polling, platform clipboard access, and size validation.
- `mouseshare/mac/protocol.py` and `mouseshare/windows/protocol.py`: 4-byte big-endian length-prefixed transport frames.

Use Python 3.13 on macOS and Python 3.12 on Windows for the pinned release toolchains. Install the matching platform requirements file, then run `python mouseshare_app.py` for development. Platform-specific input features require real macOS Accessibility/Input Monitoring permissions or a Windows desktop session.

## Release builds

Build each deliverable on its target operating system. The build scripts install pinned dependencies into `.build/` and write distributables to `release/`.

- macOS Apple Silicon `.app` and `.dmg`: `bash scripts/build-macos.sh`
- Windows installer `.exe`: `powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1` on Windows 10/11 with Python 3.12 and Inno Setup 6 installed.

See [RELEASE.md](RELEASE.md) for signing, architecture, and verification notes. The Windows installer cannot be built or exercised from macOS.
