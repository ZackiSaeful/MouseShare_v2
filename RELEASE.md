# Release build and verification

## macOS Apple Silicon

Requirements: macOS on Apple Silicon, Python 3.13 with Tcl/Tk, Xcode command-line tools (`iconutil`), and network access to install the pinned packages.

```sh
bash scripts/build-macos.sh
```

The script builds `dist/Mousehare.app`, embeds the permission usage strings, stages an Applications shortcut, and creates `release/Mousehare-macos-arm64.dmg` on Apple Silicon or `release/Mousehare-macos-x86_64.dmg` on Intel. It uses PyInstaller's windowed macOS application bundle mode, so normal launch does not open Terminal. The script names the DMG from the build host architecture; it does not produce a universal binary.

The generated package is not Developer ID signed or notarized. Public distribution requires the publisher's Apple Developer ID certificate and notarization credentials; those are not bundled in this repository. Without signing/notarization, macOS may require the user to approve the app in Privacy & Security after moving it to Applications.

For a stable signing identity, install the publisher's **Developer ID Application** certificate in the build machine's Keychain, then build with:

```sh
MACOS_CODESIGN_IDENTITY="Developer ID Application: Publisher Name (TEAMID)" bash scripts/build-macos.sh
```

If `MACOS_CODESIGN_IDENTITY` is omitted, the script uses an ad-hoc signature for local testing. macOS can treat each rebuilt ad-hoc app as a different code identity, so Accessibility and Input Monitoring approvals may need to be removed and granted again for the current copy in `/Applications`. A stable Developer ID signature keeps the app identity consistent across rebuilds; public distribution still needs notarization and stapling.

When permissions appear enabled but Mousehare still reports them denied, use the exact bundle path shown in the app's error message. Quit other Mousehare copies, remove stale Mousehare entries from Accessibility and Input Monitoring, then add the current bundle from that path. Launch only the installed copy from `/Applications/Mousehare.app`; running another copy from `dist/` can use a different macOS privacy identity.

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
4. Set Mac position to each supported side in turn. Verify Mac exits on the facing edge, Windows places the cursor on its matching entry edge, and returning through the Windows edge brings it back to the matching Mac edge.
5. Verify the generated 22-character key connects, a short non-empty key connects when it matches on both peers, and both peers show the secure pairing state. Capture traffic on a controlled test LAN to confirm control payloads are ciphertext.
6. Enable clipboard text sharing on both computers, reconnect, then copy plain text in each direction. Confirm it arrives once without an echo loop. Confirm it remains off if either side opts out, and that large clipboard contents are not sent.
7. Disconnect, close the app, and reconnect; confirm the Mac cursor is visible and usable after every exit path.
8. Try a wrong key, invalid IP/port, occupied server port, refused connection, and firewall denial; confirm a readable status/error.
9. Install both deliverables on separate computers and verify the same protocol works cross-platform.
10. Verify the app permissions and firewall prompt on clean user accounts.

This host is macOS Apple Silicon. A Windows installer and two-computer end-to-end input test must be performed on Windows and a separate Mac/Windows pair before a release is described as fully verified.
