#!/bin/bash
# Sync the KaproTUN file mirror from upstream releases.
#
#   install:  sudo install -m 755 sync-binaries.sh /usr/local/bin/kaprotun-sync
#   run:      sudo /usr/local/bin/kaprotun-sync
#   schedule: see kaprotun-mirror.timer (systemd) or cron in README.md
#
# What it mirrors — exactly the three things the client asks us for:
#
#   sing-box-<pin>-<platform>.<zip|tar.gz>   engine        (SagerNet/sing-box)
#   wintun-<ver>.zip                         Windows TUN   (wintun.net)
#   KaproTUN-Setup-v<ver>.exe                installer     (our own release)
#
# Nothing else. The geoip zone file is NOT mirrored — geoip_ru.py fetches
# ipdeny.com directly, and ipdeny is not blocked in RU.
#
# Versions are read from the client source on GitHub rather than pinned
# here, so bumping SINGBOX_PINNED_VERSION in a commit is all it takes for
# the mirror to follow on its next run. There is exactly one source of
# truth and it lives in the repo. Hardcoded values below are only a
# fallback for when raw.githubusercontent is unreachable.
#
# Failure model: a failed download leaves the PREVIOUS file in place —
# a stale mirror still serves clients, a half-written one does not. The
# script exits non-zero if any *required* asset failed, so the systemd
# unit / cron mail shows it.

set -uo pipefail

MIRROR_DIR="${MIRROR_DIR:-/var/www/kaprovpn.pro/files}"
REPO="fafnirov/KaproTUN"
RAW_SRC="https://raw.githubusercontent.com/${REPO}/main/kapro_tun/core/sing_box_installer.py"

# Fallbacks, used only if we can't read the pins out of the repo.
SINGBOX_FALLBACK="v1.12.9"
WINTUN_FALLBACK="wintun-0.14.1.zip"
KEEP_INSTALLERS=3          # how many past KaproTUN-Setup-v*.exe to retain

TMP_DIR="$(mktemp -d -t kaprotun-sync-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

failed=0
note() { printf '%s\n' "$*"; }
fail() { printf '%s\n' "$*" >&2; failed=1; }

# --- resolve the pinned versions from the client source -------------------

note "=== resolving pins from ${REPO} ==="
src="$(curl -fsSL --connect-timeout 15 --max-time 60 "$RAW_SRC" 2>/dev/null || true)"

SINGBOX_VERSION="$(printf '%s' "$src" | sed -n 's/^SINGBOX_PINNED_VERSION *= *"\([^"]*\)".*/\1/p' | head -1)"
WINTUN_FILE="$(printf '%s' "$src"     | sed -n 's/^WINTUN_FILENAME *= *"\([^"]*\)".*/\1/p'          | head -1)"

if [ -z "$SINGBOX_VERSION" ]; then
    SINGBOX_VERSION="$SINGBOX_FALLBACK"
    note "  ! could not read the pin from source — falling back to $SINGBOX_VERSION"
fi
[ -n "$WINTUN_FILE" ] || WINTUN_FILE="$WINTUN_FALLBACK"

SINGBOX_VER_BARE="${SINGBOX_VERSION#v}"
WINTUN_VERSION="$(printf '%s' "$WINTUN_FILE" | sed -n 's/^wintun-\(.*\)\.zip$/\1/p')"
note "  sing-box $SINGBOX_VERSION"
note "  wintun   ${WINTUN_VERSION:-?}"

# Refuse to mirror an engine from a release line we've blacklisted. If a
# bad pin ever lands in a commit, the mirror should not amplify it.
sb_minor="$(printf '%s' "$SINGBOX_VER_BARE" | cut -d. -f1-2)"
if [ "$sb_minor" = "1.13" ]; then
    fail "  ABORT: sing-box 1.13.x breaks the VLESS data-path on Windows; refusing to mirror"
    exit 1
fi

# --- fetch helper ---------------------------------------------------------

# fetch <url> <dest> <min-bytes>
fetch() {
    local url="$1" dest="$2" min="${3:-102400}"
    note "  [fetch] ${url##*/}"
    # Timeouts are not optional: a host that accepts the connection and
    # then never responds would otherwise hang the whole sync forever.
    if ! curl -fsSL --connect-timeout 15 --max-time 900 --retry 3 --retry-delay 5 \
              -o "$dest" "$url"; then
        rm -f "$dest"
        return 1
    fi
    local size
    size="$(stat -c%s "$dest" 2>/dev/null || echo 0)"
    if [ "$size" -lt "$min" ]; then
        note "          rejected: ${size}B is below the ${min}B floor (error page?)"
        rm -f "$dest"
        return 1
    fi
    return 0
}

# --- sing-box engine ------------------------------------------------------

note "=== sing-box ${SINGBOX_VERSION} ==="
for platform in windows-amd64 windows-arm64 darwin-amd64 darwin-arm64 linux-amd64 linux-arm64; do
    case "$platform" in
        windows-*) ext="zip"  ;;
        *)         ext="tar.gz" ;;
    esac
    asset="sing-box-${SINGBOX_VER_BARE}-${platform}.${ext}"
    fetch "https://github.com/SagerNet/sing-box/releases/download/${SINGBOX_VERSION}/${asset}" \
          "$TMP_DIR/$asset" \
        || fail "  [FAIL] $asset"
done

# --- WinTUN driver --------------------------------------------------------

note "=== wintun ${WINTUN_VERSION} ==="
# Not required: wintun.net is not blocked in RU, so a client falls back to
# it cleanly. Worth mirroring anyway for the offline/slow case.
fetch "https://www.wintun.net/builds/${WINTUN_FILE}" "$TMP_DIR/$WINTUN_FILE" \
    || note "  [skip] $WINTUN_FILE — clients fall back to wintun.net directly"

# --- our own installer ----------------------------------------------------

note "=== KaproTUN installer (latest release) ==="
# This is the whole point of the mirror for the auto-updater: when
# github.com is DNS-blocked from RU, this is the only path that works.
# The flat versioned name matches updater_dialog._mirror_setup_url().
LATEST_TAG="$(curl -fsSL --connect-timeout 15 --max-time 60 \
              "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
              | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"

if [ -n "${LATEST_TAG:-}" ]; then
    VER="${LATEST_TAG#v}"
    if fetch "https://github.com/${REPO}/releases/download/${LATEST_TAG}/KaproTUN-Setup.exe" \
             "$TMP_DIR/KaproTUN-Setup-v${VER}.exe" 5242880; then
        note "  mirrored installer v${VER}"
    else
        fail "  [FAIL] no usable KaproTUN-Setup.exe on ${LATEST_TAG}"
    fi
else
    fail "  [FAIL] couldn't resolve the latest release tag"
fi

# --- promote --------------------------------------------------------------

note "=== promoting into $MIRROR_DIR ==="
mkdir -p "$MIRROR_DIR" || { fail "cannot create $MIRROR_DIR"; exit 1; }

promoted=0
for f in "$TMP_DIR"/*; do
    [ -e "$f" ] || continue
    # Same-filesystem rename would be atomic; across filesystems mv falls
    # back to copy+unlink, so stage next to the target and rename to keep
    # nginx from ever serving a partially-written file.
    base="$(basename "$f")"
    cp -f "$f" "$MIRROR_DIR/.${base}.tmp" && mv -f "$MIRROR_DIR/.${base}.tmp" "$MIRROR_DIR/$base" \
        && promoted=$((promoted + 1)) \
        || fail "  [FAIL] could not promote $base"
done
note "  $promoted file(s) promoted"

# Retain a few past installers — a client mid-update may still be pulling
# the version it started with. Everything older goes.
mapfile -t old_installers < <(ls -1t "$MIRROR_DIR"/KaproTUN-Setup-v*.exe 2>/dev/null | tail -n +$((KEEP_INSTALLERS + 1)))
for old in "${old_installers[@]:-}"; do
    [ -n "$old" ] || continue
    note "  pruning $(basename "$old")"
    rm -f "$old"
done

chown -R www-data:www-data "$MIRROR_DIR" 2>/dev/null || true
chmod -R a+r "$MIRROR_DIR" 2>/dev/null || true

note "=== mirror contents ==="
ls -lh "$MIRROR_DIR"

if [ "$failed" -ne 0 ]; then
    note ""
    fail "sync finished WITH ERRORS — previous copies of any failed asset are still in place"
    exit 1
fi
note ""
note "sync OK"
