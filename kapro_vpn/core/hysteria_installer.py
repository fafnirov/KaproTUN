"""Download the standalone Hysteria2 client binary.

Xray-core can't speak Hysteria2, so hy2 configs route through the
`hysteria` client run as a local SOCKS5 proxy (see hysteria_process). This
module fetches the right per-OS binary on first use — same mirror-first,
then-GitHub strategy as xray_installer.

Hysteria release assets are single binaries (NOT archives):
  Windows x64    -> hysteria-windows-amd64.exe
  Windows ARM64  -> hysteria-windows-arm64.exe
  macOS Intel    -> hysteria-darwin-amd64
  macOS ARM      -> hysteria-darwin-arm64
  Linux x64      -> hysteria-linux-amd64
  Linux ARM64    -> hysteria-linux-arm64
The GitHub release tag is prefixed `app/` (e.g. app/v2.9.2).
"""
from __future__ import annotations

import io
import stat
import sys
import platform
from typing import Callable, Optional

import requests

from . import paths

GITHUB_LATEST = "https://api.github.com/repos/apernet/hysteria/releases/latest"
# Pinned fallback if the live API is unreachable. Bump alongside the
# server-setup mirror's HYSTERIA_VERSION.
PINNED_TAG = "app/v2.9.2"
KAPROVPN_MIRROR_BASE = "https://files.kaprovpn.pro"
# Bypass any system proxy — we're fetching our own deps, not user traffic
# (a stale 127.0.0.1 proxy from a crashed session would otherwise break this).
_NO_PROXY = {"http": "", "https": ""}

ProgressCb = Optional[Callable[[int, int], None]]


def _asset_name() -> str:
    """The single-binary asset filename for this platform."""
    is_arm64 = platform.machine().lower() in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "hysteria-windows-arm64.exe" if is_arm64 else "hysteria-windows-amd64.exe"
    if sys.platform == "darwin":
        return "hysteria-darwin-arm64" if is_arm64 else "hysteria-darwin-amd64"
    return "hysteria-linux-arm64" if is_arm64 else "hysteria-linux-amd64"


def is_installed() -> bool:
    f = paths.hysteria_exe()
    return f.is_file() and f.stat().st_size > 0


def get_installed_version() -> Optional[str]:
    if not is_installed():
        return None
    import subprocess
    try:
        proc = subprocess.run(
            [str(paths.hysteria_exe()), "version"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in (proc.stdout or "").splitlines():
            if line.lower().startswith("version:"):
                return line.split(":", 1)[1].strip()
        lines = (proc.stdout or "").strip().splitlines()
        return lines[0] if lines else None
    except Exception:
        return None


def _github_url() -> str:
    """Live latest-release asset URL, or the pinned-tag URL on API failure."""
    name = _asset_name()
    try:
        r = requests.get(GITHUB_LATEST, timeout=10, proxies=_NO_PROXY)
        r.raise_for_status()
        for asset in r.json().get("assets", []):
            if asset.get("name") == name:
                return asset["browser_download_url"]
    except Exception:
        pass
    return f"https://github.com/apernet/hysteria/releases/download/{PINNED_TAG}/{name}"


def _download(url: str, progress: ProgressCb, attempts: int = 2) -> bytes:
    """One source, N attempts. Raises RuntimeError on final failure."""
    sink = io.BytesIO()
    last: Optional[Exception] = None
    for _ in range(attempts):
        try:
            sink.seek(0)
            sink.truncate(0)
            done = 0
            with requests.get(url, stream=True, timeout=(10, 30),
                              proxies=_NO_PROXY) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    sink.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            data = sink.getvalue()
            # hysteria binaries are ~10-30 MB; anything under 1 MB is an
            # error page / mirror 404 masquerading as success.
            if len(data) < 1024 * 1024:
                raise RuntimeError(f"file too small ({len(data)} bytes) — not the binary")
            return data
        except (requests.exceptions.RequestException, OSError, RuntimeError) as e:
            last = e
    raise RuntimeError(f"download from {url} failed: {last}")


def _write_with_kill_retry(target, data: bytes) -> None:
    """Write data; if a running hysteria holds the file handle, kill + retry."""
    import subprocess as _sp
    import time
    no_window = getattr(_sp, "CREATE_NO_WINDOW", 0)
    try:
        target.write_bytes(data)
        return
    except PermissionError:
        pass
    if sys.platform == "win32":
        try:
            _sp.run(["taskkill", "/F", "/IM", "hysteria.exe"],
                    capture_output=True, timeout=3, creationflags=no_window)
        except (OSError, _sp.SubprocessError):
            pass
    else:
        try:
            _sp.run(["pkill", "-9", "-x", "hysteria"], capture_output=True, timeout=3)
        except (OSError, _sp.SubprocessError):
            pass
    time.sleep(0.5)
    target.write_bytes(data)  # re-raise on second failure


def download_and_install(progress: ProgressCb = None) -> None:
    """Download the hysteria binary (mirror first, GitHub fallback) and
    install it executable into the per-OS hysteria dir."""
    name = _asset_name()
    sources = [f"{KAPROVPN_MIRROR_BASE}/{name}", _github_url()]
    raw: Optional[bytes] = None
    errors: list[str] = []
    for src in sources:
        try:
            raw = _download(src, progress, attempts=2)
            break
        except RuntimeError as e:
            errors.append(str(e))
    if raw is None:
        raise RuntimeError(
            "Не удалось скачать hysteria ни с зеркала, ни с GitHub:\n  - "
            + "\n  - ".join(errors)
        )
    target = paths.hysteria_exe()
    _write_with_kill_retry(target, raw)
    if sys.platform != "win32":
        try:
            st = target.stat()
            target.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass


def ensure_installed(progress: ProgressCb = None) -> None:
    if not is_installed():
        download_and_install(progress)
