"""Downloads and extracts the Xray-core binary from GitHub releases.

Per-platform asset selection. Xray-core release names follow the
pattern Xray-<os>-<arch>.zip — we map the current sys.platform +
machine() to the right one:

  Windows x64        → Xray-windows-64.zip
  Windows ARM64      → Xray-windows-arm64-v8a.zip
  macOS Intel        → Xray-macos-64.zip
  macOS Apple Silicon→ Xray-macos-arm64-v8a.zip
  Linux x64          → Xray-linux-64.zip
  Linux ARM64        → Xray-linux-arm64-v8a.zip

On Unix-likes we chmod 755 the extracted binary so it can actually run.
"""
from __future__ import annotations

import io
import os
import platform
import stat
import sys
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from . import paths

GITHUB_LATEST = "https://api.github.com/repos/XTLS/Xray-core/releases/latest"
# Pinned fallback used if the live API is unreachable (rate-limit,
# DNS blocked, etc.). Mirrors the asset matrix above.
PINNED_VERSION = "v26.3.27"


def _asset_marker() -> str:
    """Return the lowercase substring that identifies our platform's asset
    in a Xray-core release.

    The naming convention is `Xray-<os>-<arch>` — we match on the
    second-half so 'windows-64' vs 'windows-arm64-v8a' don't collide.
    """
    machine = platform.machine().lower()
    is_arm64 = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "windows-arm64-v8a" if is_arm64 else "windows-64"
    if sys.platform == "darwin":
        return "macos-arm64-v8a" if is_arm64 else "macos-64"
    # Treat everything else as Linux. Xray ships glibc builds; users on
    # musl distros (Alpine) need to install xray manually for now.
    return "linux-arm64-v8a" if is_arm64 else "linux-64"


def _pinned_fallback_url() -> str:
    return (
        f"https://github.com/XTLS/Xray-core/releases/download/"
        f"{PINNED_VERSION}/Xray-{_asset_marker()}.zip"
    )


ProgressCb = Optional[Callable[[int, int], None]]


@dataclass
class ReleaseInfo:
    version: str
    url: str
    filename: str


def is_installed() -> bool:
    return paths.xray_exe().is_file()


def get_installed_version() -> Optional[str]:
    if not is_installed():
        return None
    import subprocess
    try:
        proc = subprocess.run(
            [str(paths.xray_exe()), "version"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        return first.strip() or None
    except Exception:
        return None


def fetch_latest_release() -> ReleaseInfo:
    marker = _asset_marker()
    try:
        r = requests.get(GITHUB_LATEST, timeout=10)
        r.raise_for_status()
        data = r.json()
        version = data.get("tag_name", "unknown")
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            # Match on the platform marker AND .zip — Xray ships both
            # .zip and .dgst variants; we want the archive.
            if marker in name.lower() and name.endswith(".zip"):
                return ReleaseInfo(
                    version=version,
                    url=asset["browser_download_url"],
                    filename=name,
                )
    except Exception:
        pass
    return ReleaseInfo(
        version=PINNED_VERSION,
        url=_pinned_fallback_url(),
        filename=f"Xray-{marker}.zip",
    )


def download_and_install(progress: ProgressCb = None) -> None:
    """Download latest Xray-core zip, extract xray binary + geo data files.

    Per-chunk read timeout (20 s) + 3 retries — survives the case where a
    throttled CDN starts a download but stops sending bytes mid-stream.
    """
    release = fetch_latest_release()
    raw: bytes = b""
    for attempt in range(3):
        try:
            sink = io.BytesIO()
            downloaded = 0
            with requests.get(release.url, stream=True, timeout=(10, 20)) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    sink.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
            raw = sink.getvalue()
            break
        except (requests.exceptions.RequestException, OSError) as e:
            if attempt == 2:
                raise RuntimeError(
                    f"Не удалось скачать Xray-core после 3 попыток: {e}"
                ) from e
    buf = io.BytesIO(raw)

    install_dir = paths.xray_dir()
    # The binary name we ship as varies per OS, but inside the zip it's
    # always "xray" (unix) or "xray.exe" (windows). Pull whichever shows
    # up plus the geo data, and write it under the right name for our OS.
    target_bin = paths.xray_exe()
    extracted_bin = False
    with zipfile.ZipFile(buf) as zf:
        for member in zf.namelist():
            base = member.rsplit("/", 1)[-1]
            if not base or base.endswith("/"):
                continue
            base_lower = base.lower()
            if base_lower in ("xray", "xray.exe"):
                data = zf.read(member)
                target_bin.write_bytes(data)
                extracted_bin = True
            elif base_lower in ("geoip.dat", "geosite.dat"):
                (install_dir / base).write_bytes(zf.read(member))

    if not extracted_bin or not target_bin.is_file():
        raise RuntimeError("xray binary not found in the downloaded archive")

    # Unix-likes need the exec bit. Belt-and-braces: set it for owner+group+
    # other so a sudo install for one user, then non-sudo run by another,
    # doesn't break.
    if sys.platform != "win32":
        try:
            st = target_bin.stat()
            target_bin.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass


def ensure_installed(progress: ProgressCb = None) -> None:
    if not is_installed():
        download_and_install(progress)
