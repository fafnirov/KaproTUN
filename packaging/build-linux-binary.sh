#!/usr/bin/env bash
# Build the Linux binary (dist/KaproTUN) inside an old-glibc container.
#
# Why a container: a binary built on a new distribution needs that
# distribution's glibc. Built on ubuntu-latest, the AppImage asked for glibc
# 2.38 and would not start on Ubuntu 22.04 or Debian 12, while the README
# promises glibc 2.31+. Ubuntu 20.04 has exactly glibc 2.31, so what is built
# here runs there and on everything newer. (Debian 11 has it too, but its
# package repositories were retired with its end of life and apt fails.)
#
# Run from the repository root, as root, inside ubuntu:20.04:
#   docker run --rm -v "$PWD":/src -w /src -e HOST_UID=$(id -u) -e HOST_GID=$(id -g) \
#     ubuntu:20.04 bash packaging/build-linux-binary.sh
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# curl + certificates for the Python download; binutils for PyInstaller
# (objdump/strip); the rest are the system libraries Qt's platform plugins
# link against — PyInstaller copies them into the bundle from here, so they
# are the old, widely compatible builds.
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl binutils file \
  libglib2.0-0 libdbus-1-3 libfontconfig1 libfreetype6 libegl1 libgl1 \
  libxkbcommon0 libxkbcommon-x11-0 libx11-xcb1 libxcb1 libxcb-cursor0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
  libxcb-render0 libxcb-render-util0 libxcb-shape0 libxcb-shm0 \
  libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1

echo "glibc in the build container: $(ldd --version | head -n1)"

# Python 3.12 from python-build-standalone (via uv): it is built against
# glibc 2.17 and ships a shared libpython, which is what PyInstaller needs.
# Ubuntu 20.04's own Python is 3.8.
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/opt/uv sh
export PATH="/opt/uv:$PATH"
uv python install 3.12
uv venv --python 3.12 /opt/venv
# shellcheck disable=SC1091
. /opt/venv/bin/activate
uv pip install -r requirements-build.txt

python -m PyInstaller KaproTUN.spec --noconfirm --log-level WARN

test -x dist/KaproTUN
file dist/KaproTUN

# The bundle must start here, on glibc 2.31. A loader that needs a newer
# glibc fails at once with "version `GLIBC_2.xx' not found"; an app that got
# as far as its event loop keeps running until the timeout stops it.
set +e
out="$(QT_QPA_PLATFORM=offscreen timeout 25 ./dist/KaproTUN 2>&1)"
rc=$?
set -e
echo "start check: exit code ${rc}"
echo "${out}" | tail -n 20
if echo "${out}" | grep -q "GLIBC_[0-9.]*' not found"; then
  echo "ERROR: the binary needs a newer glibc than this container has" >&2
  exit 1
fi

# Hand the outputs back to the runner's user (we ran as root).
if [ -n "${HOST_UID:-}" ]; then
  chown -R "${HOST_UID}:${HOST_GID:-$HOST_UID}" dist build
fi
