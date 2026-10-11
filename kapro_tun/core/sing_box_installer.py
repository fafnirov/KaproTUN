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


# SHA-256 of the files that actually get EXECUTED (or, for the driver, loaded
# into the network stack), per platform: what is inside the archives above.
#
# The archive digests prove what was downloaded. They say nothing about what
# is in the folder an hour later — and that folder is in the user's profile,
# writable by any program of theirs, while the engine is started from it with
# administrator rights. So before every start the files on disk are compared
# with these, and held against changes while the engine runs (hold_engine()).
#
# It also pins the engine to exactly one build: a sing-box of another version
# in that folder — including the 1.13.x line whose VLESS data-plane stalls on
# Windows — is simply not the pinned file, and is replaced.
#
# Computed from the pinned archives (each first checked against its digest
# above). Refresh together with SINGBOX_PINNED_VERSION / the WinTUN archive.
_PINNED_BINARY_SHA256: dict[str, dict[str, str]] = {
    "windows-amd64": {
        "sing-box.exe": "5c94a0a25b1b61b2f50c00815e72d6f3e5e5d234e6adf7d8a02526b5b173bc79",
        "wintun.dll": "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce",
    },
    "windows-arm64": {
        "sing-box.exe": "27f9b8f808496809b8a2f8e6a670d759c6054360688a625c23743e391f74ff2b",
        "wintun.dll": "f7ba89005544be9d85231a9e0d5f23b2d15b3311667e2dad0debd344918a3f80",
    },
    "darwin-amd64": {
        "sing-box": "1667b65743297d4dec53c491b472e3e2f62fac8052c857d55cbf8d21f8a34599",
    },
    "darwin-arm64": {
        "sing-box": "6ca4a7cc11a7b5ce126d3a7266238d3865692659e0d3f5388143f181249caa3d",
    },
    "linux-amd64": {
        "sing-box": "d883cf185b05564674485b64973014955c8060493e597ddb90369a6860bf5441",
    },
    "linux-arm64": {
        "sing-box": "849d1a59d7ad94483f70a6233d85c7c63042e06b1f0ad70ef1a04faecb2d7aae",
    },
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


def engine_files() -> dict:
    """{file name: path} of everything that must be on disk, and must be the
    pinned build, for the engine to be started on this platform."""
    files = {paths.sing_box_exe().name: paths.sing_box_exe()}
    if sys.platform == "win32":
        files[paths.wintun_dll().name] = paths.wintun_dll()     # the WinTUN driver
    return files


def expected_binary_sha256(name: str) -> str:
    """Pinned digest of the engine file `name` for this platform, or ""."""
    return _PINNED_BINARY_SHA256.get(_asset_marker(), {}).get(name, "")


# is_installed() is asked several times around every connect, and hashing the
# engine takes a moment. Remember the answer per file for as long as the file
# looks untouched (same size, same modification time). This is a shortcut for
# the question "is a download needed?" only — the check that guards the start
# is hold_engine(), which always reads the bytes.
_seen_good: dict[str, tuple] = {}


def _matches_pin(name: str, path) -> bool:
    want = expected_binary_sha256(name)
    if not want:
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    stamp = (want, st.st_size, st.st_mtime_ns)
    if _seen_good.get(str(path)) == stamp:
        return True
    try:
        net_download.verify_sha256(path, want)
    except (net_download.IntegrityError, OSError):
        _seen_good.pop(str(path), None)
        return False
    _seen_good[str(path)] = stamp
    return True


def is_installed() -> bool:
    """True when every engine file is present AND is the pinned build. A file
    that is there but different — another version, a damaged download, one
    that was tampered with — counts as not installed, so the caller downloads
    the pinned build over it."""
    return all(_matches_pin(name, path) for name, path in engine_files().items())


def get_installed_version() -> Optional[str]:
    """The engine's version, or None if the pinned build is not installed.

    Known from the digest: a file that matches the pin IS that version. The
    engine is not run to ask it — that would execute, with our rights, a file
    nobody has checked yet."""
    name = paths.sing_box_exe().name
    if not _matches_pin(name, paths.sing_box_exe()):
        return None
    return SINGBOX_PINNED_VERSION.lstrip("v")


class EngineGuard:
    """The engine's files, checked and held against changes. Keep it for as
    long as the engine process runs; close() lets the files go."""

    def __init__(self, held: list):
        self._held = held

    def real_path(self, name: str):
        """Where the held file `name` really is — the path to start it by.
        (Not the path we looked it up under: that one may lead through a
        junction, which can be re-pointed; see file_guard.)"""
        for item in self._held:
            if item.path.name == name:
                return item.real_path
        raise KeyError(name)

    def close(self) -> None:
        from . import file_guard
        file_guard.release_all(self._held)

    def __enter__(self) -> "EngineGuard":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def hold_engine() -> EngineGuard:
    """Check every engine file against its pinned digest and keep it from
    being changed, replaced or deleted until the returned guard is closed.

    Call right before starting the engine and keep the guard while it runs.
    Raises FileNotFoundError if a file is missing and
    net_download.IntegrityError if one is not the pinned build."""
    from . import file_guard
    held: list = []
    try:
        for name, path in engine_files().items():
            held.append(file_guard.hold_verified(path, expected_binary_sha256(name)))
    except file_guard.HoldError as e:
        # Nothing wrong with the bytes as far as we know — the file could not
        # be held. Downloading it again would change nothing; say what it is.
        file_guard.release_all(held)
        app_log.log(f"[integrity] engine file could not be held: {type(e.__cause__).__name__}")
        raise
    except net_download.IntegrityError as e:
        file_guard.release_all(held)
        # Forget the "looked fine" shortcut: a file can be swapped for one of
        # the same size and time stamp, and is_installed() must then stop
        # saying yes — or the download that would repair it never starts.
        _seen_good.clear()
        app_log.log(f"[integrity] engine file refused before start: {type(e).__name__}")
        raise net_download.IntegrityError(
            "Файл движка VPN на диске не совпадает с проверенной сборкой "
            f"sing-box {SINGBOX_PINNED_VERSION.lstrip('v')} и не будет запущен.\n"
            "Подключись ещё раз — KaproTUN скачает движок заново.\n\n" + str(e)) from e
    except Exception:
        file_guard.release_all(held)
        raise
    return EngineGuard(held)


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


def _write_verified(target, payload: bytes, name: str) -> None:
    """Put `payload` at `target` only if it is the pinned build of `name`.
    Written next to the target and moved into place, so a file that fails the
    check never exists where the engine is started from."""
    net_download.verify_sha256(payload, expected_binary_sha256(name))
    # A new file under a random name, created exclusively: a predictable
    # name could be planted as a link and make this (elevated) process
    # overwrite whatever it points to.
    import os
    import tempfile
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".new")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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
    # The archive carries one DLL per architecture; take this machine's.
    arch = "arm64" if _asset_marker().endswith("arm64") else "amd64"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        dll_member = next(
            (n for n in zf.namelist()
             if n.endswith("wintun.dll") and f"/{arch}/" in n),
            None,
        )
        if not dll_member:
            raise RuntimeError(f"wintun.dll ({arch}) not found in the archive")
        _write_verified(paths.wintun_dll(), zf.read(dll_member), paths.wintun_dll().name)


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
            _write_verified(target, zf.read(member), want)
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
        _write_verified(target, src.read(), want)
    try:
        st = target.stat()
        target.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def download_and_install(progress: ProgressCb = None) -> None:
    """Install sing-box (and the WinTUN driver on Windows).

    A file that is already there but is not the pinned build — another
    version, a damaged download, one somebody changed — is replaced. It is
    not deleted first: the new file is written next to it and moved over it
    only once it has passed its own check (_write_verified), so a download
    that fails leaves things as they were, and a file that cannot be replaced
    (still held by a running engine) is an error the caller sees — not a
    silent "nothing happened"."""
    exe = paths.sing_box_exe()
    if not _matches_pin(exe.name, exe):
        if exe.is_file():
            app_log.log("[integrity] sing-box on disk is not the pinned build — replacing it")
        release = _fetch_release()
        data = _download_with_fallback(release.filename, release.url, progress)
        _extract_binary(data, exe)
    if sys.platform == "win32":
        dll = paths.wintun_dll()
        if not _matches_pin(dll.name, dll):
            if dll.is_file():
                app_log.log("[integrity] wintun.dll on disk is not the pinned build — replacing it")
            _install_wintun(progress)


def ensure_installed(progress: ProgressCb = None) -> None:
    if not is_installed():
        download_and_install(progress)
