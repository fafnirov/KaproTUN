"""Downloads and extracts the Xray-core Windows binary from GitHub releases."""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from . import paths

GITHUB_LATEST = "https://api.github.com/repos/XTLS/Xray-core/releases/latest"
PINNED_FALLBACK_URL = (
    "https://github.com/XTLS/Xray-core/releases/download/"
    "v26.3.27/Xray-windows-64.zip"
)
ASSET_MARKER = "windows-64"

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
    try:
        r = requests.get(GITHUB_LATEST, timeout=10)
        r.raise_for_status()
        data = r.json()
        version = data.get("tag_name", "unknown")
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            # Xray-core asset is "Xray-windows-64.zip" — case-insensitive contains check
            if ASSET_MARKER in name.lower() and name.endswith(".zip"):
                return ReleaseInfo(
                    version=version,
                    url=asset["browser_download_url"],
                    filename=name,
                )
    except Exception:
        pass
    return ReleaseInfo(
        version="v26.3.27",
        url=PINNED_FALLBACK_URL,
        filename="Xray-windows-64.zip",
    )


def download_and_install(progress: ProgressCb = None) -> None:
    """Download latest Xray-core zip, extract xray.exe + geo data files."""
    release = fetch_latest_release()
    with requests.get(release.url, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        buf = io.BytesIO()
        downloaded = 0
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            buf.write(chunk)
            downloaded += len(chunk)
            if progress:
                progress(downloaded, total)
        buf.seek(0)

    install_dir = paths.xray_dir()
    with zipfile.ZipFile(buf) as zf:
        # Xray zip contains xray.exe at root plus geoip.dat / geosite.dat
        for member in zf.namelist():
            base = member.rsplit("/", 1)[-1]
            if not base or base.endswith("/"):
                continue
            # Only extract files we care about — skip docs/readmes
            if base.lower() in ("xray.exe", "geoip.dat", "geosite.dat"):
                with zf.open(member) as src:
                    (install_dir / base).write_bytes(src.read())

    if not paths.xray_exe().is_file():
        raise RuntimeError("xray.exe not found in the downloaded archive")


def ensure_installed(progress: ProgressCb = None) -> None:
    if not is_installed():
        download_and_install(progress)
