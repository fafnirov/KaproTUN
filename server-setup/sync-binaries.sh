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

echo "=== wintun.net $WINTUN_VERSION ==="
fetch "https://www.wintun.net/builds/${WINTUN_FILE}" "$TMP_DIR/$WINTUN_FILE"

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
