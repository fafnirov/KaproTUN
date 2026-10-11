"""Filesystem paths used during install/uninstall."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


APP_NAME = "KaproTUN"
APP_EXE_NAME = "KaproTUN.exe"
PUBLISHER = "KaproTUN"
HOMEPAGE = "https://github.com/fafnirov/KaproTUN"


def install_dir() -> Path:
    """Per-user install location — no admin required."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_NAME


def installed_exe_path() -> Path:
    return install_dir() / APP_EXE_NAME


def installed_uninstaller_path() -> Path:
    return install_dir() / f"{APP_NAME}-Uninstall.exe"


def start_menu_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME


def desktop_dir() -> Path:
    return Path.home() / "Desktop"


def bundled_main_exe() -> Optional[Path]:
    """If a KaproTUN.exe is embedded with the installer, return its path.

    We dropped embedding in v0.1.4 — the installer downloads the matching
    KaproTUN.exe from the GitHub release at install time instead — but
    keep this lookup around in case anyone re-enables embed via the spec
    file (or runs the dev/unfrozen flow with a local dist/KaproTUN.exe).
    Returns None when no local payload is available.
    """
    if getattr(sys, "frozen", False):
        candidate = Path(sys._MEIPASS) / "payload" / APP_EXE_NAME
    else:
        candidate = Path(__file__).resolve().parent.parent / "dist" / APP_EXE_NAME
    return candidate if candidate.is_file() else None


def github_release_exe_url(version: str) -> str:
    """Direct download URL for KaproTUN.exe attached to a tagged release."""
    return f"https://github.com/fafnirov/KaproTUN/releases/download/v{version}/{APP_EXE_NAME}"


def mirror_exe_url(version: str) -> str:
    """The same file on our mirror (server-setup/sync-binaries.sh puts it
    there under a flat, versioned name)."""
    return f"https://kaprovpn.pro/files/KaproTUN-v{version}.exe"


def release_page_url(version: str) -> str:
    return f"https://github.com/fafnirov/KaproTUN/releases/tag/v{version}"
