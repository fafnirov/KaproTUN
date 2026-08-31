#!/bin/bash
# Verify the KaproTUN mirror actually serves what the client asks for.
#
#   ./verify-mirror.sh                 # check https://kaprovpn.pro/files
#   MIRROR=https://host/files ./verify-mirror.sh
#
# Run it from anywhere — a dev box, the VPS, CI. Exits non-zero on any
# problem so it can gate a cron/CI job.
#
# Why this exists: the client tries the mirror first and falls back to
# GitHub on ANY failure, silently. That is good for users and terrible
# for us — a mirror can be dead for months and nothing surfaces it. In
# Aug 2026 the kaprovpn.pro vhost served a certificate issued for an
# unrelated domain; every client had been quietly falling back to GitHub,
# which is exactly the path RU users can't take. This script turns that
# class of failure into a loud, checkable signal.

set -uo pipefail

MIRROR="${MIRROR:-https://kaprovpn.pro/files}"
REPO="fafnirov/KaproTUN"
RAW_SRC="https://raw.githubusercontent.com/${REPO}/main/kapro_tun/core/sing_box_installer.py"

host="$(printf '%s' "$MIRROR" | sed -E 's#^https?://##; s#/.*##')"
problems=0

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; problems=$((problems + 1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; }

# --- 1. TLS ---------------------------------------------------------------

echo "=== TLS: $host ==="
tls_err="$(curl -sS -o /dev/null --connect-timeout 15 --max-time 30 "https://${host}/" 2>&1)"
if [ -n "$tls_err" ]; then
    bad "handshake/verify failed — $tls_err"
    # Show what the server actually presents; a hostname mismatch here
    # means nginx fell through to a default vhost with another site's cert.
    subject="$(echo | openssl s_client -connect "${host}:443" -servername "$host" 2>/dev/null \
               | openssl x509 -noout -subject -dates 2>/dev/null)"
    [ -n "$subject" ] && printf '        server presents: %s\n' "$(printf '%s' "$subject" | tr '\n' ' ')"
else
    expiry="$(echo | openssl s_client -connect "${host}:443" -servername "$host" 2>/dev/null \
              | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)"
    if [ -n "$expiry" ]; then
        secs_left=$(( $(date -d "$expiry" +%s 2>/dev/null || echo 0) - $(date +%s) ))
        days_left=$(( secs_left / 86400 ))
        if   [ "$days_left" -lt 0 ];  then bad  "certificate EXPIRED ($expiry)"
        elif [ "$days_left" -lt 14 ]; then warn "certificate expires in ${days_left}d ($expiry) — renewal is overdue"
        else ok "certificate valid, ${days_left}d remaining"
        fi
    else
        ok "certificate verifies"
    fi
fi

# --- 2. the assets the client will actually request ------------------------

src="$(curl -fsSL --connect-timeout 15 --max-time 60 "$RAW_SRC" 2>/dev/null || true)"
SINGBOX_VERSION="$(printf '%s' "$src" | sed -n 's/^SINGBOX_PINNED_VERSION *= *"\([^"]*\)".*/\1/p' | head -1)"
WINTUN_FILE="$(printf '%s' "$src"     | sed -n 's/^WINTUN_FILENAME *= *"\([^"]*\)".*/\1/p'          | head -1)"
[ -n "$SINGBOX_VERSION" ] || { SINGBOX_VERSION="v1.12.9"; warn "using fallback sing-box pin $SINGBOX_VERSION"; }
[ -n "$WINTUN_FILE" ]     || WINTUN_FILE="wintun-0.14.1.zip"
SB="${SINGBOX_VERSION#v}"

LATEST_TAG="$(curl -fsSL --connect-timeout 15 --max-time 60 \
              "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
              | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"

assets=(
    "sing-box-${SB}-windows-amd64.zip"
    "sing-box-${SB}-windows-arm64.zip"
    "sing-box-${SB}-darwin-amd64.tar.gz"
    "sing-box-${SB}-darwin-arm64.tar.gz"
    "sing-box-${SB}-linux-amd64.tar.gz"
    "sing-box-${SB}-linux-arm64.tar.gz"
    "$WINTUN_FILE"
)
[ -n "${LATEST_TAG:-}" ] && assets+=("KaproTUN-Setup-v${LATEST_TAG#v}.exe")

echo
echo "=== assets on $MIRROR ==="
for a in "${assets[@]}"; do
    read -r code size < <(curl -sS -o /dev/null --connect-timeout 15 --max-time 45 \
                          -w '%{http_code} %{size_download}\n' -r 0-0 "${MIRROR}/${a}" 2>/dev/null \
                          || echo "000 0")
    case "$code" in
        200|206) ok   "$a" ;;
        404)     bad  "$a — 404, sync has never placed this file" ;;
        000)     bad  "$a — no response (TLS or connectivity)" ;;
        *)       bad  "$a — HTTP $code" ;;
    esac
done

echo
if [ "$problems" -eq 0 ]; then
    echo "mirror healthy"
    exit 0
fi
echo "$problems problem(s) — clients are silently falling back to GitHub,"
echo "which is the path RU users cannot take. Fix before shipping a release."
exit 1
