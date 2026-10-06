#!/bin/bash
# KaproTUN installer for macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/fafnirov/KaproTUN/main/packaging/install-macos.sh | bash
#
# Why this exists. KaproTUN is not signed with an Apple Developer ID and not
# notarized (that needs a paid Apple Developer account). A DMG downloaded with
# a browser is therefore stopped by Gatekeeper: "Apple could not verify that
# KaproTUN is free of malware". Gatekeeper acts on the quarantine attribute,
# and that attribute is set by the downloading app — a browser sets it, curl
# does not. Installing from the terminal means there is nothing for Gatekeeper
# to act on, so the app simply opens.
#
# Be clear about what that trades away: macOS is NOT checking this app for
# you. What stands in for that check here is (a) HTTPS to github.com and
# (b) the SHA-256 that GitHub itself reports for the release file, which this
# script verifies before installing anything. Read the script first if you
# like — that is the point of it being a plain shell file.
#
# It needs no administrator password: it installs into /Applications when
# that is writable (administrator accounts) and into ~/Applications otherwise.

set -euo pipefail

REPO="fafnirov/KaproTUN"
APP="KaproTUN.app"

say()  { printf '%s\n' "$*"; }
fail() { printf 'Ошибка / Error: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "этот установщик только для macOS / this installer is for macOS only"

# Architecture. `uname -m` reports x86_64 inside a Rosetta-translated shell on
# Apple Silicon, so ask the hardware instead.
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ]; then
  ASSET="KaproTUN-macOS-arm64.dmg"
else
  ASSET="KaproTUN-macOS-x64.dmg"
fi

OS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[ "$OS_MAJOR" -ge 13 ] || fail "нужна macOS 13 или новее (у тебя $(sw_vers -productVersion)) / macOS 13+ required"

WORK="$(mktemp -d -t kaprotun-install)"
MNT="$WORK/mnt"
cleanup() {
  hdiutil detach "$MNT" -quiet 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

say "==> Ищу последний релиз / Looking up the latest release"
curl -fsSL --connect-timeout 15 --max-time 60 \
  "https://api.github.com/repos/${REPO}/releases/latest" -o "$WORK/release.json" \
  || fail "не удалось получить данные о релизе с GitHub / could not reach the GitHub API"

TAG="$(sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' "$WORK/release.json" | head -1)"
[ -n "$TAG" ] || fail "не удалось определить версию / could not determine the release tag"

# The asset's own "digest" field follows its "name" inside the same JSON object.
WANT_SHA="$(awk -v a="$ASSET" '
  index($0, "\"name\": \"" a "\"") { found = 1 }
  found && /"digest":/ { sub(/.*sha256:/, ""); sub(/".*/, ""); print; exit }
' "$WORK/release.json")"

say "==> Скачиваю ${ASSET} (${TAG}) / Downloading"
curl -fL --connect-timeout 15 --retry 3 --retry-delay 3 --progress-bar \
  "https://github.com/${REPO}/releases/download/${TAG}/${ASSET}" -o "$WORK/$ASSET" \
  || fail "не удалось скачать ${ASSET} / download failed"

if [ -n "$WANT_SHA" ]; then
  GOT_SHA="$(shasum -a 256 "$WORK/$ASSET" | cut -d' ' -f1)"
  if [ "$GOT_SHA" != "$WANT_SHA" ]; then
    fail "контрольная сумма не совпала, файл не установлен / checksum mismatch, nothing installed
  ожидалась / expected: $WANT_SHA
  получена  / got:      $GOT_SHA"
  fi
  say "==> Контрольная сумма совпала / Checksum verified"
else
  # GitHub did not report a digest. The file came from github.com over the
  # same TLS as the API answer, so there is no stronger source to compare
  # with — say so rather than pretend it was checked.
  say "==> Внимание: GitHub не сообщил контрольную сумму, файл не сверен / Warning: no digest reported, file not verified"
fi

mkdir -p "$MNT"
hdiutil attach "$WORK/$ASSET" -nobrowse -readonly -mountpoint "$MNT" -quiet \
  || fail "не удалось открыть образ / could not mount the disk image"
[ -d "$MNT/$APP" ] || fail "в образе нет ${APP} / ${APP} not found in the image"

DEST="/Applications"
if [ ! -w "$DEST" ]; then
  DEST="$HOME/Applications"
  mkdir -p "$DEST"
fi

# Quit a running copy only now, after the download: if it is the VPN that
# makes github.com reachable, it had to stay up until this point. A normal
# quit lets it disconnect and restore the system proxy itself.
if pgrep -x KaproTUN >/dev/null 2>&1; then
  say "==> Закрываю запущенный KaproTUN / Quitting the running KaproTUN"
  osascript -e 'tell application "KaproTUN" to quit' >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -x KaproTUN >/dev/null 2>&1 || break
    sleep 1
  done
  # Still running (macOS may have declined to let the terminal quit it, or it
  # runs as root in TUN mode). Killing it would skip its own disconnect and
  # could leave the system proxy pointing at a dead port, and replacing the
  # bundle under a live process helps nobody. Stop and say what to do.
  if pgrep -x KaproTUN >/dev/null 2>&1; then
    fail "KaproTUN всё ещё запущен: закрой его (значок в строке меню → Выход) и запусти команду ещё раз / KaproTUN is still running: quit it from the menu bar and run this command again"
  fi
fi

say "==> Устанавливаю в ${DEST} / Installing"
rm -rf "${DEST:?}/${APP}"
ditto "$MNT/$APP" "$DEST/$APP"
# curl does not quarantine what it downloads, so this is normally a no-op.
# It covers the case of a copy that was first downloaded with a browser.
xattr -dr com.apple.quarantine "$DEST/$APP" 2>/dev/null || true

say "==> Готово / Done: ${DEST}/${APP} (${TAG})"
open "$DEST/$APP"
