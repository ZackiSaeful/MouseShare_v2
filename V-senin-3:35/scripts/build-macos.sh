#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MACOS_CODESIGN_IDENTITY="${MACOS_CODESIGN_IDENTITY:--}"
BUILD_ENV="$ROOT/.build/venv-macos"
ARCH="$(uname -m)"
case "$ARCH" in
  arm64|x86_64) ;;
  *) echo "Unsupported macOS architecture: $ARCH" >&2; exit 1 ;;
esac
mkdir -p "$ROOT/.build" "$ROOT/release"
mkdir -p "$ROOT/.build/spec"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$ROOT/.build/pyinstaller-cache}"
if [[ -z "${DEVELOPER_DIR:-}" && -d /Library/Developer/CommandLineTools ]]; then
  export DEVELOPER_DIR=/Library/Developer/CommandLineTools
fi
if [[ ! -x "$BUILD_ENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$BUILD_ENV"
fi
"$BUILD_ENV/bin/python" -m pip install -r requirements-macos.txt
"$BUILD_ENV/bin/python" scripts/make_icons.py --platform macos

"$BUILD_ENV/bin/python" -m PyInstaller \
  --noconfirm --clean --windowed --onedir \
  --name Mousehare \
  --osx-bundle-identifier com.mousehare.desktop \
  --specpath "$ROOT/.build/spec" \
  --icon "$ROOT/.build/icons/mouseshare.icns" \
  --paths "$ROOT" \
  --collect-all pystray \
  --collect-all cryptography \
  --hidden-import Quartz --hidden-import CoreFoundation --hidden-import AppKit \
  mouseshare_app.py

APP="$ROOT/dist/Mousehare.app"
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :NSAccessibilityUsageDescription string Mousehare needs Accessibility permission to share keyboard and mouse input.' "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c 'Set :NSAccessibilityUsageDescription Mousehare needs Accessibility permission to share keyboard and mouse input.' "$PLIST"
/usr/libexec/PlistBuddy -c 'Add :NSInputMonitoringUsageDescription string Mousehare needs Input Monitoring permission to capture keyboard and mouse input.' "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c 'Set :NSInputMonitoringUsageDescription Mousehare needs Input Monitoring permission to capture keyboard and mouse input.' "$PLIST"
if [[ "$MACOS_CODESIGN_IDENTITY" == "-" ]]; then
  echo "Signing ad hoc for local use. macOS privacy approvals may need to be renewed after a rebuild."
  codesign --force --deep --sign - --identifier com.mousehare.desktop "$APP"
else
  echo "Signing with: $MACOS_CODESIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp \
    --sign "$MACOS_CODESIGN_IDENTITY" --identifier com.mousehare.desktop "$APP"
fi
codesign --verify --deep --strict "$APP"

STAGE="$ROOT/.build/dmg-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/Mousehare.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname Mousehare -srcfolder "$STAGE" -ov -format UDZO \
  "$ROOT/release/Mousehare-macos-$ARCH.dmg"
echo "Built $ROOT/release/Mousehare-macos-$ARCH.dmg"
