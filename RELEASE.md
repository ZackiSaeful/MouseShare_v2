# Release build and verification

## macOS Apple Silicon

Requirements: macOS on Apple Silicon, Python 3.13 with Tcl/Tk, Xcode command-line tools (`iconutil`), and network access to install the pinned packages.

```sh
bash scripts/build-macos.sh
```

The script builds `dist/Mousehare.app`, embeds the permission usage strings, stages an Applications shortcut, and creates `release/Mousehare-macos-arm64.dmg` on Apple Silicon or `release/Mousehare-macos-x86_64.dmg` on Intel. It uses PyInstaller's windowed macOS application bundle mode, so normal launch does not open Terminal. The script names the DMG from the build host architecture; it does not produce a universal binary.

The generated package is not Developer ID signed or notarized. Public distribution requires the publisher's Apple Developer ID certificate and notarization credentials; those are not bundled in this repository. Without signing/notarization, macOS may require the user to approve the app in Privacy & Security after moving it to Applications.

## Windows 10/11 x64

Requirements: Windows 10/11 x64, Python 3.12, and Inno Setup 6. The Windows binary must be produced on Windows so PyInstaller can bundle the Windows runtime and native dependencies correctly.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1
```

The script creates a windowed `dist/Mousehare/` application and compiles `release/Mousehare-Setup-1.0.0.exe`. The installer is per-user (`PrivilegesRequired=lowest`) and creates a Start menu shortcut plus an optional desktop shortcut. Runtime Python and libraries are included in the PyInstaller output.

## Verification checklist

On each target OS, test the built app from the installer or DMG copy rather than the source tree:

1. Launch from Applications/Start menu; confirm no terminal window appears.
2. Start the Windows server and verify its displayed local IP and listening port.
3. Enter that IP, port, and pairing key in the Mac client; verify Connected.
4. Test right-edge handoff and return across the Windows left edge with keyboard, mouse buttons, movement, and scroll.
5. Disconnect, close the app, and reconnect; confirm the Mac cursor is visible and usable after every exit path.
6. Try a wrong key, invalid IP/port, occupied server port, refused connection, and firewall denial; confirm a readable status/error.
7. Install both deliverables on separate computers and verify the same protocol works cross-platform.
8. Verify the app permissions and firewall prompt on clean user accounts.

This host is macOS Apple Silicon. A Windows installer and two-computer end-to-end input test must be performed on Windows and a separate Mac/Windows pair before a release is described as fully verified.
