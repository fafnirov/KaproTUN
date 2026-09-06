"""Downloads the sing-box binary (SagerNet/sing-box) per-OS.

v3.0.0 makes sing-box the primary TUN dataplane: a single process that owns the
TUN device natively and routes/proxies itself — no separate tun2socks.exe and
no local SOCKS bridge (127.0.0.1:2081), which is what exhausted loopback
ephemeral ports under load in the classic engine.

Release assets (SagerNet/sing-box):
  Windows  → sing-box-<ver>-windows-amd64.zip     (sing-box.exe inside a folder)
  macOS    → sing-box-<ver>-darwin-amd64.tar.gz / -arm64
  Linux    → sing-box-<ver>-linux-amd64.tar.gz   / -arm64

On Windows sing-box uses the same WinTUN driver as tun2socks (wintun.dll lives
in tun_dir() and sing-box finds it via PATH/cwd). Mirror-first download with a
strict size cap and streaming to disk — same pattern as xray/tun2socks.
"""
from __future__ import annotations

import io
import platform
import re
import stat
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from . import app_log, net_download, paths

SINGBOX_LATEST = "https://api.github.com/repos/SagerNet/sing-box/releases/latest"
# The sing-box release we ship. We DELIBERATELY pin to the 1.12.x line and do
# NOT track GitHub "latest" any more (see _fetch_release).
#
# Why not 1.13.x: the 1.13 line regressed the VLESS data-plane on Windows — the
# tunnel ESTABLISHES (the proxy CONNECT returns "200", REALITY handshake + auth
# succeed) but PAYLOAD STALLS: HTTP/TLS bytes never flow, so every real request
# times out. `sing-box check` can't catch it (it's a runtime data-path bug, not
# a config-grammar error), which is exactly how v3.1.0/v3.1.1 shipped a broken
# default engine. v3.1.2 rolls back to a proven 1.12.x release where the same
# generated config carries real traffic end-to-end.
#
# Floor: MUST be >= 1.12.0 — sing_box_config emits the modern config grammar
# (typed DNS servers, `hijack-dns`/`sniff`/`route` rule actions,
# route.default_domain_resolver) that 1.11 would reject.
SINGBOX_PINNED_VERSION = "v1.12.9"

# Release lines known to break the VLESS data-plane on Windows. A binary at or
# above this (major, minor) is treated as "not installed" so we replace it with
# the pinned 1.12.x. Bump this once upstream fixes the regression.
_BLOCKED_MINOR = (1, 13)

KAPROTUN_MIRROR_BASE = "https://kaprovpn.pro/files"

# Expected SHA-256 for every asset we fetch. These are pinned in code on
# purpose: the versions above are pinned too, so the digests are constants,
# and a constant needs no network to consult. That matters — the mirror
# exists precisely for users who cannot reach github.com, so an integrity
# check that had to ask GitHub for the expected value would be unavailable
# exactly when it is most needed.
#
# sing-box values come from the GitHub release API's own `digest` field for
# tag v1.12.9; the WinTUN one was computed from wintun.net's published
# archive and cross-checked against an independently fetched copy.
#
# When bumping SINGBOX_PINNED_VERSION, replace all six — a stale digest here
# fails closed (nothing installs), which is the correct direction to fail but
# will look like a broken download until the values are refreshed.
_PINNED_SHA256: dict[str, str] = {
    "sing-box-1.12.9-windows-amd64.zip":
        "f9f9b55d394fe08a8afe1fd3bb1c037e687a12fbe701f16481be6941d1766840",
    "sing-box-1.12.9-windows-arm64.zip":
        "4a490b0d114e0ae4c7e154232860cad7b9897bfcd9324d40b07a48dae90eb1d0",
    "sing-box-1.12.9-darwin-amd64.tar.gz":
        "1657fb9fd356bc17d4b657052db93a0741547348070e605ed2553a067281fd8b",
    "sing-box-1.12.9-darwin-arm64.tar.gz":
        "d37141302f0c9e1ea5a2f071e78146961a7b4f9045feaafee51d8b3535c6ff0b",
    "sing-box-1.12.9-linux-amd64.tar.gz":
        "519bc521e6b25f779b37738c5fca0fa3f68175b3d8e434fcacd8ea42da9e70ef",
    "sing-box-1.12.9-linux-arm64.tar.gz":
        "0d571bf961c651cc5a4eaffe9715d7759edb484b90473f3fa25aaab87fa11961",
    "wintun-0.14.1.zip":
        "07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51",
}


def expected_sha256(filename: str) -> str:
    """Pinned digest for `filename`, or "" when we have none on file.

    Returning "" rather than raising keeps a future asset from bricking the
    installer, but every current caller passes a filename that IS pinned —
    see the smoke check that asserts the table covers all six platforms plus
    WinTUN.
    """
    return _PINNED_SHA256.get(filename, "")

# Windows-only WinTUN driver (sing-box uses it for the native TUN device). Was
# previously fetched via tun2socks_installer; inlined here in v3.1.0 when the
# classic tun2socks engine was removed so sing-box owns its only dependency.
WINTUN_URL = "https://www.wintun.net/builds/wintun-0.14.1.zip"
WINTUN_FILENAME = "wintun-0.14.1.zip"  # used for the mirror URL

# Bypass system proxy on our own downloads (a stale 127.0.0.1:2080 registry
# entry from a crashed HTTP-mode session otherwise kills every fetch).
_NO_PROXY = {"http": "", "https": ""}

ProgressCb = Optional[Callable[[int, int], None]]


@dataclass
class ReleaseInfo:
    version: str
    url: str
    filename: str


def _asset_marker() -> str:
    machine = platform.machine().lower()
    is_arm64 = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "windows-arm64" if is_arm64 else "windows-amd64"
    if sys.platform == "darwin":
        return "darwin-arm64" if is_arm64 else "darwin-amd64"
    return "linux-arm64" if is_arm64 else "linux-amd64"


def _asset_ext() -> str:
    # Windows ships .zip; the other platforms ship .tar.gz.
    return ".zip" if sys.platform == "win32" else ".tar.gz"


def _pinned_filename() -> str:
    ver = SINGBOX_PINNED_VERSION.lstrip("v")
    return f"sing-box-{ver}-{_asset_marker()}{_asset_ext()}"


def _pinned_fallback_url() -> str:
    return (f"https://github.com/SagerNet/sing-box/releases/download/"
            f"{SINGBOX_PINNED_VERSION}/{_pinned_filename()}")


def _parse_minor(version_str: str) -> Optional[tuple[int, int]]:
    """Extract (major, minor) from a `sing-box version 1.12.9` string, or None
    if it can't be parsed."""
    m = re.search(r"(\d+)\.(\d+)\.\d+", version_str or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def is_blocked_version() -> bool:
    """True if the on-disk sing-box is from a release line we've blacklisted for
    breaking the VLESS data-plane on Windows (>= 1.13). Returns False if there's
    no binary or its version can't be parsed (don't churn a possibly-fine one)."""
    if not paths.sing_box_exe().is_file():
        return False
    mm = _parse_minor(get_installed_version() or "")
    return mm is not None and mm >= _BLOCKED_MINOR


def is_installed() -> bool:
    if not paths.sing_box_exe().is_file():
        return False
    # A previously-downloaded 1.13.x stalls VLESS payloads — treat it as "not
    # installed" so callers re-download the pinned 1.12.x over it (v3.1.2).
    if is_blocked_version():
        return False
    # On Windows sing-box (like tun2socks) needs the WinTUN driver.
    if sys.platform == "win32":
        return paths.wintun_dll().is_file()
    return True


def get_installed_version() -> Optional[str]:
    if not paths.sing_box_exe().is_file():
        return None
    import subprocess
    try:
        proc = subprocess.run(
            [str(paths.sing_box_exe()), "version"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or proc.stderr or "").strip().splitlines()
        return out[0].strip() if out else None
    except Exception:
        return None


def _fetch_release() -> ReleaseInfo:
    """Resolve the sing-box asset to download.

    v3.1.2: we no longer follow GitHub "latest". Tracking latest is what pulled
    in the 1.13.x line whose VLESS data-plane stalls on Windows (tunnel up,
    payload never flows). For a VPN client, a proven-stable engine beats a fresh
    one, so we fetch EXACTLY the pinned 1.12.x release (mirror-first, GitHub
    fallback). Bump SINGBOX_PINNED_VERSION to move it."""
    return ReleaseInfo(SINGBOX_PINNED_VERSION, _pinned_fallback_url(),
                       _pinned_filename())


def _download(url: str, progress: ProgressCb, attempts: int = 3,
              expect_sha256: str = "") -> bytes:
    last_err: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return net_download.download_to_memory(
                url, net_download.MAX_SINGBOX_ARCHIVE, progress,
                expect_sha256=expect_sha256 or None)
        except net_download.DownloadTooLarge:
            raise
        except (requests.exceptions.RequestException, OSError) as e:
            last_err = e
            if attempt < attempts - 1:
                continue
    raise RuntimeError(f"Не удалось скачать sing-box после {attempts} попыток: {last_err}")


def _download_with_fallback(filename: str, upstream_url: str,
                            progress: ProgressCb) -> bytes:
    """Mirror first, upstream fallback — same as the other installers.

    Both paths are checked against the same pinned digest, so the fallback is
    not a way around the check: a mirror serving the wrong bytes loses to
    upstream instead of winning by being first.
    """
    want = expected_sha256(filename)
    try:
        return _download(f"{KAPROTUN_MIRROR_BASE}/{filename}", progress,
                         attempts=2, expect_sha256=want)
    except net_download.IntegrityError as e:
        # Not a routine fallback. The mirror answered, was not truncated, and
        # still produced different bytes than we published — say so loudly
        # rather than quietly succeeding from upstream a moment later.
        app_log.log(f"[integrity] mirror rejected for {filename}: {e}")
    except RuntimeError:
        pass
    return _download(upstream_url, progress, attempts=2, expect_sha256=want)


def _download_wintun(url: str, progress: ProgressCb, attempts: int = 2,
                     expect_sha256: str = "") -> bytes:
    last_err: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return net_download.download_to_memory(
                url, net_download.MAX_WINTUN_ZIP, progress,
                expect_sha256=expect_sha256 or None)
        except net_download.DownloadTooLarge:
            raise
        except (requests.exceptions.RequestException, OSError) as e:
            last_err = e
            if attempt < attempts - 1:
                continue
    raise RuntimeError(f"Не удалось скачать WinTUN: {last_err}")


def _install_wintun(progress: ProgressCb) -> None:
    """Windows-only: fetch + extract the WinTUN driver DLL (mirror → upstream)."""
    want = expected_sha256(WINTUN_FILENAME)
    try:
        data = _download_wintun(f"{KAPROTUN_MIRROR_BASE}/{WINTUN_FILENAME}",
                                progress, expect_sha256=want)
    except net_download.IntegrityError as e:
        app_log.log(f"[integrity] mirror rejected for {WINTUN_FILENAME}: {e}")
        data = _download_wintun(WINTUN_URL, progress, expect_sha256=want)
    except RuntimeError:
        data = _download_wintun(WINTUN_URL, progress, expect_sha256=want)
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


def _extract_binary(data: bytes, target) -> None:
    """Pull the sing-box[.exe] out of the release archive (zip or tar.gz)."""
    want = "sing-box.exe" if sys.platform == "win32" else "sing-box"
    if sys.platform == "win32":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next(
                (n for n in zf.namelist()
                 if not n.endswith("/") and n.rsplit("/", 1)[-1].lower() == want),
                None,
            )
            if not member:
                raise RuntimeError("sing-box.exe not found in the archive")
            target.write_bytes(zf.read(member))
        return
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        member = next(
            (m for m in tf.getmembers()
             if m.isfile() and m.name.rsplit("/", 1)[-1] == want),
            None,
        )
        if not member:
            raise RuntimeError("sing-box binary not found in the archive")
        src = tf.extractfile(member)
        if src is None:
            raise RuntimeError("sing-box binary unreadable in the archive")
        target.write_bytes(src.read())
    try:
        st = target.stat()
        target.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def download_and_install(progress: ProgressCb = None) -> None:
    """Install sing-box (and the WinTUN driver on Windows).

    v3.1.2: if a blacklisted 1.13.x binary is already on disk, delete it first
    so we replace it with the pinned 1.12.x (otherwise the is_file() guard would
    keep the broken engine forever). Best-effort unlink — a held file (sing-box
    still running) is reaped by main._kill_orphan_helpers before we get here."""
    exe = paths.sing_box_exe()
    if exe.is_file() and is_blocked_version():
        try:
            exe.unlink()
        except OSError:
            pass
    if not exe.is_file():
        release = _fetch_release()
        data = _download_with_fallback(release.filename, release.url, progress)
        _extract_binary(data, exe)
    if sys.platform == "win32" and not paths.wintun_dll().is_file():
        _install_wintun(progress)


def ensure_installed(progress: ProgressCb = None) -> None:
    if not is_installed():
        download_and_install(progress)
