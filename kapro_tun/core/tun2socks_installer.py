"""Downloads tun2socks (xjasonlyu/tun2socks) per-OS binary.

tun2socks creates the TUN device and forwards all OS-level TCP/UDP into
xray's SOCKS5 inbound — the foundation of TUN mode on all three OSes.

Per-OS assets in xjasonlyu/tun2socks releases:
  Windows  → tun2socks-windows-amd64.zip   (.exe inside, needs WinTUN driver alongside)
  macOS    → tun2socks-darwin-amd64.zip    Intel
             tun2socks-darwin-arm64.zip    Apple Silicon
  Linux    → tun2socks-linux-amd64.zip
             tun2socks-linux-arm64.zip

On Windows we additionally fetch WinTUN.dll from wintun.net — on
macOS/Linux the kernel already provides the TUN device (utun / /dev/net/tun)
so no extra driver is needed.
"""
from __future__ import annotations

import io
import platform
import stat
import sys
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from . import net_download, paths

TUN2SOCKS_LATEST = "https://api.github.com/repos/xjasonlyu/tun2socks/releases/latest"
TUN2SOCKS_PINNED_VERSION = "v2.6.0"

# Windows-only: WinTUN driver from wintun.net
WINTUN_URL = "https://www.wintun.net/builds/wintun-0.14.1.zip"
WINTUN_FILENAME = "wintun-0.14.1.zip"  # used for the mirror URL
WINTUN_DLL_IN_ZIP = "wintun/bin/amd64/wintun.dll"

# Our own mirror — same setup as xray_installer. See server-setup/
# for nginx + sync-binaries.sh. Mirror is tried FIRST, upstream is
# the fallback when our server is down or doesn't have a file yet.
KAPROTUN_MIRROR_BASE = "https://kaprovpn.pro/files"

# Bypass system proxy on our own downloads — see xray_installer for full
# story. TL;DR: a stale 127.0.0.1:2080 registry entry from a crashed
# HTTP-mode session kills every GitHub fetch with WinError 10061.
_NO_PROXY = {"http": "", "https": ""}


def _asset_marker() -> str:
    """Return the OS-arch substring that identifies our tun2socks asset.

    Maps Python's sys.platform + platform.machine() onto the names
    xjasonlyu/tun2socks ships.
    """
    machine = platform.machine().lower()
    is_arm64 = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "windows-arm64" if is_arm64 else "windows-amd64"
    if sys.platform == "darwin":
        return "darwin-arm64" if is_arm64 else "darwin-amd64"
    return "linux-arm64" if is_arm64 else "linux-amd64"


def _pinned_fallback_url() -> str:
    return (
        f"https://github.com/xjasonlyu/tun2socks/releases/download/"
        f"{TUN2SOCKS_PINNED_VERSION}/tun2socks-{_asset_marker()}.zip"
    )


ProgressCb = Optional[Callable[[int, int], None]]


@dataclass
class ReleaseInfo:
    version: str
    url: str
    filename: str


def is_installed() -> bool:
    """tun2socks present? On Windows we also need WinTUN; on Unix the
    kernel provides the TUN device, so no driver check needed.
    """
    if not paths.tun2socks_exe().is_file():
        return False
    if sys.platform == "win32":
        return paths.wintun_dll().is_file()
    return True


def get_installed_version() -> Optional[str]:
    if not paths.tun2socks_exe().is_file():
        return None
    import subprocess
    try:
        proc = subprocess.run(
            [str(paths.tun2socks_exe()), "-version"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first = (proc.stdout or proc.stderr or "").splitlines()[0].strip() if (proc.stdout or proc.stderr) else ""
        return first or None
    except Exception:
        return None


def _fetch_tun2socks_release() -> ReleaseInfo:
    marker = _asset_marker()
    try:
        r = requests.get(TUN2SOCKS_LATEST, timeout=10, proxies=_NO_PROXY)
        r.raise_for_status()
        data = r.json()
        version = data.get("tag_name", "unknown")
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if marker in name and name.endswith(".zip"):
                return ReleaseInfo(
                    version=version,
                    url=asset["browser_download_url"],
                    filename=name,
                )
    except Exception:
        pass
    return ReleaseInfo(
        version=TUN2SOCKS_PINNED_VERSION,
        url=_pinned_fallback_url(),
        filename=f"tun2socks-{marker}.zip",
    )


def _download(url: str, progress: ProgressCb, total_offset: int = 0,
              attempts: int = 3,
              max_bytes: int = net_download.MAX_TUN2SOCKS_ZIP) -> bytes:
    """Download to memory, size-capped, with per-chunk timeout + retries.
    `total_offset` lets the wintun fetch continue the same progress bar."""
    last_err: Optional[Exception] = None

    def _prog(done: int, total: int) -> None:
        if progress:
            progress(total_offset + done, total_offset + total)

    for attempt in range(attempts):
        try:
            return net_download.download_to_memory(
                url, max_bytes, _prog if progress else None)
        except net_download.DownloadTooLarge:
            raise  # over-cap is not transient — surface immediately
        except (requests.exceptions.RequestException, OSError) as e:
            last_err = e
            if attempt < attempts - 1:
                continue
    raise RuntimeError(f"Не удалось скачать после {attempts} попыток: {last_err}")


def _mirror_url(filename: str) -> str:
    return f"{KAPROTUN_MIRROR_BASE}/{filename}"


def _download_with_fallback(filename: str, upstream_url: str,
                            progress: ProgressCb,
                            max_bytes: int = net_download.MAX_TUN2SOCKS_ZIP) -> bytes:
    """Try the mirror first, fall back to upstream on any failure.

    The mirror serves files by the same filename GitHub uses (so the
    sync script doesn't have to rename anything). On 404 / DNS-fail /
    timeout we drop down to the upstream URL transparently.
    """
    mirror = _mirror_url(filename)
    try:
        return _download(mirror, progress, attempts=2, max_bytes=max_bytes)
    except RuntimeError:
        # mirror miss (incl. over-cap) — fall through to upstream
        pass
    return _download(upstream_url, progress, attempts=2, max_bytes=max_bytes)


def _install_tun2socks(progress: ProgressCb) -> None:
    """Pull the right tun2socks-<os>-<arch> binary out of its release zip.

    The binary inside is suffixed identically to the zip (e.g. inside
    tun2socks-darwin-arm64.zip you get a file literally named
    "tun2socks-darwin-arm64"). We extract whichever member looks like
    our tun2socks binary and write it under the right name for our OS
    (paths.tun2socks_exe() handles the .exe suffix on Windows).
    """
    release = _fetch_tun2socks_release()
    data = _download_with_fallback(release.filename, release.url, progress)
    target = paths.tun2socks_exe()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # Look for any non-directory member whose basename contains
        # "tun2socks". Skip README/LICENSE/etc.
        member = next(
            (n for n in zf.namelist()
             if not n.endswith("/")
             and "tun2socks" in n.rsplit("/", 1)[-1].lower()
             and not n.lower().endswith((".md", ".txt", ".license"))),
            None,
        )
        if not member:
            raise RuntimeError("tun2socks binary not found in the archive")
        target.write_bytes(zf.read(member))
    # chmod +x on Unix — without it the user gets a cryptic EACCES later.
    if sys.platform != "win32":
        try:
            st = target.stat()
            target.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass


def _install_wintun(progress: ProgressCb) -> None:
    """Windows-only: WinTUN driver DLL.

    Source order: our mirror → wintun.net upstream. wintun.net is
    slow-ish from RU and occasionally times out, so the mirror is a
    real win for first-launch latency.
    """
    data = _download_with_fallback(WINTUN_FILENAME, WINTUN_URL, progress,
                                   max_bytes=net_download.MAX_WINTUN_ZIP)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        dll_member = next(
            (n for n in zf.namelist()
             if n.endswith("wintun.dll") and "amd64" in n),
            None,
        )
        if not dll_member:
            raise RuntimeError("wintun.dll (amd64) not found in the archive")
        with zf.open(dll_member) as src:
            paths.wintun_dll().write_bytes(src.read())


def download_and_install(progress: ProgressCb = None) -> None:
    """Install tun2socks (and WinTUN driver on Windows)."""
    if not paths.tun2socks_exe().is_file():
        _install_tun2socks(progress)
    if sys.platform == "win32" and not paths.wintun_dll().is_file():
        _install_wintun(progress)


def ensure_installed(progress: ProgressCb = None) -> None:
    if not is_installed():
        download_and_install(progress)
