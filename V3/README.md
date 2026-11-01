# Mousehare V3

V3 is a clean, isolated rewrite. It does not import or modify the V2 package.
The first milestone is one Mac input provider paired to one Windows receiver on
the same private network. It shares pointer, keyboard, button, and scroll input.
Pairing is authenticated and all session messages are encrypted. Clipboard and
multi-monitor geometry negotiation are later milestones.

## Run from source

macOS (Python 3.13, Tcl/Tk):

```sh
cd V3
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-macos.txt
.venv/bin/python -m mousehare_v3
```

Windows 10/11 (Python 3.12):

```powershell
cd V3
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
.venv\Scripts\python.exe -m mousehare_v3
```

Windows listens on TCP 5555 and shows a newly generated pairing key. Enter the
Windows LAN address and key on the Mac. Keep both computers on a trusted
private network and permit Mousehare V3 through Windows Firewall for Private
networks.

## Build artifacts

Build the macOS DMG on macOS:

```sh
bash scripts/build-macos.sh
```

Build the Windows installer on Windows with Inno Setup 6:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1
```

The Mac build creates `release/MousehareV3-macos-<architecture>.dmg` with a
stable bundle identifier (`com.mousehare.v3`) and designated code requirement,
so local Accessibility and Input Monitoring approvals can persist across local
rebuilds. The default build is ad-hoc signed and not notarized; public Mac
distribution needs Developer ID signing and notarization credentials.

The Windows build creates both `dist/MousehareV3/MousehareV3.exe` and
`release/MousehareV3-Setup-3.0.0.exe`. It is unsigned unless the Windows build
machine has a publisher certificate with its private key in
`Cert:\CurrentUser\My`; pass its thumbprint with
`-CertificateThumbprint`. An unsigned build may still be blocked by Smart App
Control. A self-signed certificate does not establish publisher reputation on
another computer. Native Windows build and Smart App Control validation must
run on Windows.

## Pairing

1. Put both computers on the same trusted private LAN.
2. On Windows, install and open Mousehare V3, then choose **Start receiving**.
   Copy the displayed LAN IP and pairing key. Allow the app through Windows
   Firewall for Private networks if prompted.
3. On Mac, install the DMG to Applications, open Mousehare V3, enter the
   Windows IP and pairing key, select where Windows is relative to the Mac, and
   choose **Connect**.
4. Approve Accessibility and Input Monitoring for the installed V3 app when
   macOS asks. If permission was granted while the app was open, quit and reopen
   the same `/Applications/MousehareV3.app` before connecting again.

Both ends must run V3. A V2 client cannot pair to a V3 receiver.

## V3 transport contract

TCP frames use a four-byte network-order length and UTF-8 JSON. A v1 handshake
authenticates ephemeral X25519 keys with the user pairing key, derives separate
AES-GCM send/receive keys with HKDF-SHA256, then requires an encrypted finish
message before accepting input. Each encrypted frame has a strict sequence
number and direction-specific nonce prefix. No V2 wire compatibility is
claimed; both computers must run V3.
