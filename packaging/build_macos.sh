#!/bin/bash
# Bouwt "Open FujiTransfer.app" en een .dmg in dist/. Alleen op een Mac.
#   python3 -m pip install pyinstaller
#   bash packaging/build_macos.sh
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(python3 -c "import open_fujitransfer as o; print(o.__version__)")
ARCH=$(uname -m)
APP="Open FujiTransfer"

echo "==> Icoon maken"
rm -rf build/icon.iconset && mkdir -p build/icon.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s assets/icon.png --out build/icon.iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) assets/icon.png --out build/icon.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns build/icon.iconset -o build/icon.icns

echo "==> App bouwen"
python3 -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller \
  packaging/OpenFujiTransfer.spec

# Ad-hoc ondertekenen (geen Apple Developer-account nodig)
codesign --force --deep --sign - "dist/$APP.app"

echo "==> DMG maken"
rm -rf build/dmg && mkdir -p build/dmg
cp -R "dist/$APP.app" build/dmg/
ln -s /Applications build/dmg/Applications
DMG="dist/OpenFujiTransfer-$VERSION-macos-$ARCH.dmg"
hdiutil create -volname "$APP" -srcfolder build/dmg -ov -format UDZO "$DMG"
echo "==> Klaar: $DMG"
