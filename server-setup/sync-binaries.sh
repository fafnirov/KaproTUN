#!/bin/bash
# Sync upstream xray-core, tun2socks, wintun binaries into the
# KaproVPN mirror directory. Run weekly via cron.
#
# Usage:
#   /usr/local/bin/kaprovpn-sync
#
# Side effects:
#   Downloads from GitHub / wintun.net into a temp dir, atomically
#   moves into /var/www/files.kaprovpn.pro/ on success. If a single
#   download fails the old version stays in place (no half-broken
#   mirror state).
#
# Failure handling: exits non-zero on any download failure so a
# cron log shows "FAILED" in the email/log. Already-downloaded
# files are kept — only failed ones need re-running.

set -euo pipefail

MIRROR_DIR="/var/www/files.kaprovpn.pro"
TMP_DIR="$(mktemp -d -t kaprovpn-sync-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Pinned upstream versions. Bump these when a new release comes out
# (or rewrite to fetch latest via GitHub API — left as a manual
# step so an upstream breaking change can't auto-poison the mirror).
XRAY_VERSION="v26.3.27"
TUN2SOCKS_VERSION="v2.6.0"
WINTUN_VERSION="0.14.1"
# Hysteria2 client binary (apernet/hysteria). Release tags are prefixed
# `app/` — see the download URL below.
HYSTERIA_VERSION="v2.9.2"

XRAY_ASSETS=(
    "Xray-windows-64.zip"
    "Xray-windows-arm64-v8a.zip"
    "Xray-macos-64.zip"
    "Xray-macos-arm64-v8a.zip"
    "Xray-linux-64.zip"
    "Xray-linux-arm64-v8a.zip"
)

TUN2SOCKS_ASSETS=(
    "tun2socks-windows-amd64.zip"
    "tun2socks-darwin-amd64.zip"
    "tun2socks-darwin-arm64.zip"
    "tun2socks-linux-amd64.zip"
    "tun2socks-linux-arm64.zip"
)

HYSTERIA_ASSETS=(
    "hysteria-windows-amd64.exe"
    "hysteria-windows-arm64.exe"
    "hysteria-darwin-amd64"
    "hysteria-darwin-arm64"
    "hysteria-linux-amd64"
    "hysteria-linux-arm64"
)

WINTUN_FILE="wintun-${WINTUN_VERSION}.zip"

fetch() {
    local url="$1" dest="$2"
    echo "  [fetch] $url"
    if ! curl -fsSL --retry 3 --retry-delay 5 -o "$dest" "$url"; then
        echo "  [FAIL]  $url" >&2
        return 1
    fi
    # Sanity: must be at least 100 KB (smallest tun2socks ~1 MB)
    if [ "$(stat -c%s "$dest")" -lt 102400 ]; then
        echo "  [FAIL]  $url returned suspiciously small file" >&2
        rm -f "$dest"
        return 1
    fi
}

echo "=== Xray-core $XRAY_VERSION ==="
for asset in "${XRAY_ASSETS[@]}"; do
    fetch "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/${asset}" \
          "$TMP_DIR/$asset"
done

echo "=== xjasonlyu/tun2socks $TUN2SOCKS_VERSION ==="
for asset in "${TUN2SOCKS_ASSETS[@]}"; do
    fetch "https://github.com/xjasonlyu/tun2socks/releases/download/${TUN2SOCKS_VERSION}/${asset}" \
          "$TMP_DIR/$asset"
done

echo "=== apernet/hysteria $HYSTERIA_VERSION ==="
# NB: hysteria release tags are prefixed `app/`, so the path is
# .../releases/download/app/vX.Y.Z/<asset>. Assets are single binaries.
for asset in "${HYSTERIA_ASSETS[@]}"; do
    fetch "https://github.com/apernet/hysteria/releases/download/app/${HYSTERIA_VERSION}/${asset}" \
          "$TMP_DIR/$asset"
done

echo "=== wintun.net $WINTUN_VERSION ==="
fetch "https://www.wintun.net/builds/${WINTUN_FILE}" "$TMP_DIR/$WINTUN_FILE"

echo "=== KaproVPN release installer (latest) ==="
# Mirror the latest Windows installer so the in-app auto-updater can fall
# back here when github.com is DNS-blocked / throttled (the common RU
# failure). Flat versioned name `KaproVPN-Setup-v<ver>.exe` matches what
# updater_dialog._mirror_setup_url() requests. Old versions are left in
# place (clients may still be updating from them); prune by hand if disk
# gets tight.
LATEST_TAG="$(curl -fsSL https://api.github.com/repos/fafnirov/KaproVPN/releases/latest \
              | grep -oP '"tag_name":\s*"\K[^"]+' | head -1 || true)"
if [ -n "${LATEST_TAG:-}" ]; then
    VER="${LATEST_TAG#v}"
    if fetch "https://github.com/fafnirov/KaproVPN/releases/download/${LATEST_TAG}/KaproVPN-Setup.exe" \
             "$TMP_DIR/KaproVPN-Setup-v${VER}.exe"; then
        echo "  mirrored installer v${VER}"
    else
        echo "  [skip] no KaproVPN-Setup.exe asset for ${LATEST_TAG}"
    fi
else
    echo "  [skip] couldn't resolve latest KaproVPN release tag"
fi

echo "=== Promoting to $MIRROR_DIR ==="
mkdir -p "$MIRROR_DIR"
# Atomic-ish: move each file into place. If we crash mid-loop the
# already-moved files are the new version, the rest are still the
# old version — never inconsistent within a single file.
for f in "$TMP_DIR"/*; do
    mv -f "$f" "$MIRROR_DIR/"
done
chown -R www-data:www-data "$MIRROR_DIR"
chmod -R a+r "$MIRROR_DIR"

echo "=== Done. Mirror contents: ==="
ls -lh "$MIRROR_DIR"
