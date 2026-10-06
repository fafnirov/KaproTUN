#!/bin/bash
# Build KaproTUN.app with PyInstaller and wrap it in a .dmg.
#
#   packaging/build-macos-dmg.sh <output.dmg> <expected-arch>
#
# Shared by both macOS jobs in .github/workflows/release.yml: Apple Silicon
# (arm64) and Intel (x86_64). PyInstaller builds for the architecture of the
# Python it runs under, so which Mac you get is decided by the runner, not by
# a flag here — hence the check at the end. A DMG labelled "Intel" that
# contains an arm64 binary does not launch on an Intel Mac at all, and the
# only symptom the user sees is "the application can't be opened".

set -euo pipefail

OUT="${1:?usage: build-macos-dmg.sh <output.dmg> <expected-arch>}"
WANT_ARCH="${2:?usage: build-macos-dmg.sh <output.dmg> <expected-arch>}"

# PyInstaller's BUNDLE() rejects .png — it needs a real .icns. Generate one
# from our PNG with the macOS-native tools (Apple's iconset wants square
# sizes 16..512, each with an @2x variant).
mkdir -p icon.iconset
for SZ in 16 32 128 256 512; do
  sips -z "$SZ" "$SZ" kapro_tun/data/icon.png \
    --out "icon.iconset/icon_${SZ}x${SZ}.png" > /dev/null
  DBL=$((SZ * 2))
  sips -z "$DBL" "$DBL" kapro_tun/data/icon.png \
    --out "icon.iconset/icon_${SZ}x${SZ}@2x.png" > /dev/null
done
iconutil -c icns icon.iconset -o kapro_tun/data/icon.icns
ls -lh kapro_tun/data/icon.icns

python -m PyInstaller KaproTUN.spec --noconfirm --log-level WARN
ls -la dist/

# Refuse to package the wrong architecture.
BIN="dist/KaproTUN.app/Contents/MacOS/KaproTUN"
GOT_ARCH="$(lipo -archs "$BIN")"
echo "runner: $(uname -m), binary: $GOT_ARCH, expected: $WANT_ARCH"
if [ "$GOT_ARCH" != "$WANT_ARCH" ]; then
  echo "ERROR: built '$GOT_ARCH' but this job is supposed to ship '$WANT_ARCH'" >&2
  exit 1
fi

# Package the .app into a .dmg users drag into /Applications.
mkdir -p dist/dmg_root
cp -R dist/KaproTUN.app dist/dmg_root/
ln -s /Applications dist/dmg_root/Applications
# The app is unsigned, so Gatekeeper will stop most first launches. Put the
# way through it where the user is already looking: next to the app.
cp packaging/macos-first-run.txt "dist/dmg_root/Не открывается? Прочти.txt"
hdiutil create -volname "KaproTUN" -srcfolder dist/dmg_root \
  -ov -format UDZO "dist/${OUT}"
rm -rf dist/dmg_root
ls -lh "dist/${OUT}"
