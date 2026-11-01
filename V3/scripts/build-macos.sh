#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SIGN_IDENTITY="${MACOS_CODESIGN_IDENTITY:--}"
NOTARY_PROFILE="${MACOS_NOTARY_PROFILE:-}"
ARCH="$(uname -m)"
case "$ARCH" in arm64|x86_64) ;; *) echo "Unsupported Mac architecture: $ARCH" >&2; exit 1 ;; esac
BUILD="$ROOT/.build"
ENV="$BUILD/venv-macos"
mkdir -p "$BUILD/spec" "$ROOT/release"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$BUILD/pyinstaller-cache}"
if [[ ! -x "$ENV/bin/python" ]]; then "$PYTHON_BIN" -m venv "$ENV"; fi
"$ENV/bin/python" -m pip install --upgrade pip
"$ENV/bin/python" -m pip install -r requirements-macos.txt
"$ENV/bin/python" -m PyInstaller --noconfirm --clean --windowed --onedir \
  --name MousehareV3 --osx-bundle-identifier com.mousehare.v3 \
  --specpath "$BUILD/spec" --distpath "$ROOT/dist" --workpath "$BUILD/pyinstaller" \
  --paths "$ROOT" --collect-all cryptography \
  --hidden-import Quartz --hidden-import CoreFoundation "$ROOT/launch.py"

APP="$ROOT/dist/MousehareV3.app"
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :NSAccessibilityUsageDescription string Mousehare V3 needs Accessibility permission to share mouse and keyboard input.' "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c 'Set :NSAccessibilityUsageDescription Mousehare V3 needs Accessibility permission to share mouse and keyboard input.' "$PLIST"
/usr/libexec/PlistBuddy -c 'Add :NSInputMonitoringUsageDescription string Mousehare V3 needs Input Monitoring permission to capture keyboard and mouse input.' "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c 'Set :NSInputMonitoringUsageDescription Mousehare V3 needs Input Monitoring permission to capture keyboard and mouse input.' "$PLIST"

if [[ "$SIGN_IDENTITY" == "-" ]]; then
  codesign --force --deep --sign - --identifier com.mousehare.v3 \
    --requirements '=designated => identifier "com.mousehare.v3"' "$APP"
else
  codesign --force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY" \
    --identifier com.mousehare.v3 "$APP"
fi
codesign --verify --deep --strict "$APP"

STAGE="$BUILD/dmg-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/MousehareV3.app"
ln -s /Applications "$STAGE/Applications"
DMG="$ROOT/release/MousehareV3-macos-$ARCH.dmg"
hdiutil create -volname "Mousehare V3" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
if [[ -n "$NOTARY_PROFILE" ]]; then
  if [[ "$SIGN_IDENTITY" == "-" ]]; then echo "Notarization requires a Developer ID signature." >&2; exit 1; fi
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
fi
echo "Built $DMG"
if [[ "$SIGN_IDENTITY" == "-" ]]; then
  echo "Local ad-hoc build. Public distribution requires Developer ID signing and notarization."
fi
