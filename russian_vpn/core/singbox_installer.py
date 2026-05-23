"""Downloads and extracts the sing-box Windows binary from GitHub releases."""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from . import paths

GITHUB_LATEST = "https://api.github.com/repos/SagerNet/sing-box/releases/latest"
PINNED_FALLBACK_URL = (
    "https://github.com/SagerNet/sing-box/releases/download/"
    "v1.13.12/sing-box-1.13.12-windows-amd64.zip"
)
ASSET_MARKER = "windows-amd64"

ProgressCb = Optional[Callable[[int, int], None]]  # (bytes_done, bytes_total)


@dataclass
class ReleaseInfo:
    version: str
    url: str
    filename: str


def is_installed() -> bool:
    return paths.singbox_exe().is_file()


def get_installed_version() -> Optional[str]:
    """Returns version string by running `sing-box version`, or None on error."""
    if not is_installed():
        return None
    import subprocess
    try:
        proc = subprocess.run(
            [str(paths.singbox_exe()), "version"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        return first.strip() or None
    except Exception:
        return None


def fetch_latest_release() -> ReleaseInfo:
    """Asks GitHub API for the latest release. Falls back to pinned URL on failure."""
    try:
        r = requests.get(GITHUB_LATEST, timeout=10)
        r.raise_for_status()
        data = r.json()
        version = data.get("tag_name", "unknown")
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if ASSET_MARKER in name and name.endswith(".zip"):
                return ReleaseInfo(
                    version=version,
                    url=asset["browser_download_url"],
                    filename=name,
                )
    except Exception:
        pass
    return ReleaseInfo(
        version="v1.13.12",
        url=PINNED_FALLBACK_URL,
        filename="sing-box-1.13.12-windows-amd64.zip",
    )


def download_and_install(progress: ProgressCb = None) -> None:
    """Download latest sing-box zip, extract sing-box.exe to the install dir."""
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

    with zipfile.ZipFile(buf) as zf:
        exe_member = next(
            (n for n in zf.namelist() if n.endswith("sing-box.exe")),
            None,
        )
        if not exe_member:
            raise RuntimeError("sing-box.exe not found in the downloaded archive")
        with zf.open(exe_member) as src, open(paths.singbox_exe(), "wb") as dst:
            dst.write(src.read())


def ensure_installed(progress: ProgressCb = None) -> None:
    if not is_installed():
        download_and_install(progress)
