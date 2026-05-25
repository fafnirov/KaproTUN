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

# Our own mirror — server we control, files manually synced via
# server-setup/sync-binaries.sh. Tried FIRST: faster (no API hop, no
# CDN-redirect, often less blocked in RU than github.com), and lets
# us keep working when GitHub itself has a bad day. Falls back to
# upstream GitHub URL on any failure (404/timeout/etc).
KAPROVPN_MIRROR_BASE = "https://files.kaprovpn.pro"

# Bypass system proxy — we're fetching our own deps, not user traffic.
# Without this, a stale 127.0.0.1:2080 system proxy (left over from a
# crashed HTTP-mode session where xray died without restoring the
# registry) makes every GitHub download fail with WinError 10061
# "Connection refused" because nothing's listening on that port.
# Empty-string values explicitly override env vars AND Windows
# registry proxy detection.
_NO_PROXY = {"http": "", "https": ""}


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
    """True only if ALL three required files are present.

    Used to be just `xray.exe is_file()`, which created a silent
    failure mode: if a wipe-and-reinstall left xray.exe locked by a
    running process but successfully deleted geoip.dat (which isn't
    locked), the next launch saw "xray.exe exists, install OK" and
    skipped re-download — then xray crashed at startup because
    `geoip:private` rules in our routing config can't load without
    geoip.dat. Now we require all three.
    """
    geo_dir = paths.xray_dir()
    return (
        paths.xray_exe().is_file()
        and (geo_dir / "geoip.dat").is_file()
        and (geo_dir / "geosite.dat").is_file()
    )


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
        r = requests.get(GITHUB_LATEST, timeout=10, proxies=_NO_PROXY)
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


def _mirror_url(filename: str) -> str:
    """URL on our own server for the given asset filename.

    Filename matches what GitHub releases use (e.g. `Xray-windows-64.zip`)
    so the sync script just `wget`s upstream and drops the file in nginx
    docroot — no renaming.
    """
    return f"{KAPROVPN_MIRROR_BASE}/{filename}"


def _download_to_memory(url: str, progress: ProgressCb,
                        attempts: int = 2) -> bytes:
    """One source, N attempts. Raises on final failure."""
    sink = io.BytesIO()
    last_err: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            sink.seek(0)
            sink.truncate(0)
            downloaded = 0
            with requests.get(url, stream=True, timeout=(10, 20),
                              proxies=_NO_PROXY) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    sink.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
            return sink.getvalue()
        except (requests.exceptions.RequestException, OSError) as e:
            last_err = e
    raise RuntimeError(f"download from {url} failed: {last_err}")


def download_and_install(progress: ProgressCb = None) -> None:
    """Download latest Xray-core zip, extract xray binary + geo data files.

    Try OUR mirror first (fast, we control), fall back to whatever URL
    the GitHub Releases API hands us (could be the latest tag or our
    pinned fallback). If both fail with 6 attempts total, surface a
    clear error so the installer dialog can suggest a manual download.
    """
    release = fetch_latest_release()
    sources = [
        _mirror_url(f"Xray-{_asset_marker()}.zip"),  # try ours first
        release.url,                                  # then upstream
    ]
    raw: Optional[bytes] = None
    errors: list[str] = []
    for src in sources:
        try:
            raw = _download_to_memory(src, progress, attempts=2)
            break
        except RuntimeError as e:
            errors.append(str(e))
    if raw is None:
        raise RuntimeError(
            "Не удалось скачать Xray-core ни с зеркала, ни с GitHub:\n  - "
            + "\n  - ".join(errors)
        )
    buf = io.BytesIO(raw)

    install_dir = paths.xray_dir()
    # The binary name we ship as varies per OS, but inside the zip it's
    # always "xray" (unix) or "xray.exe" (windows). Pull whichever shows
    # up plus the geo data, and write it under the right name for our OS.
    target_bin = paths.xray_exe()
    extracted = {"bin": False, "geoip": False, "geosite": False}
    with zipfile.ZipFile(buf) as zf:
        for member in zf.namelist():
            base = member.rsplit("/", 1)[-1]
            if not base or base.endswith("/"):
                continue
            base_lower = base.lower()
            if base_lower in ("xray", "xray.exe"):
                target_bin.write_bytes(zf.read(member))
                extracted["bin"] = True
            elif base_lower == "geoip.dat":
                (install_dir / "geoip.dat").write_bytes(zf.read(member))
                extracted["geoip"] = True
            elif base_lower == "geosite.dat":
                (install_dir / "geosite.dat").write_bytes(zf.read(member))
                extracted["geosite"] = True

    # All three are required — xray's routing config references
    # `geoip:private` (and bypass rules can reference `geoip:ru` /
    # `geosite:*`), so a missing .dat breaks startup with a cryptic
    # "failed to load GeoIP" error. Better to fail loudly here so the
    # installer dialog can offer a retry.
    missing = [k for k, ok in extracted.items() if not ok]
    if missing:
        raise RuntimeError(
            f"Xray archive is incomplete — missing: {', '.join(missing)}. "
            f"Try again or download manually from GitHub."
        )

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
