"""Filesystem paths used during install/uninstall."""
from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "KaproVPN"
APP_EXE_NAME = "KaproVPN.exe"
PUBLISHER = "KaproVPN"
HOMEPAGE = "https://github.com/fafnirov/KaproVPN"


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


def bundled_main_exe() -> Path:
    """Where the to-be-installed KaproVPN.exe lives inside *this* installer.

    PyInstaller places `--add-data` files under `sys._MEIPASS` at runtime,
    under the original sub-path. In dev (not frozen), we look in the
    repo's `dist/` so a local `pyinstaller KaproVPN.spec` run produces a
    real file to install from.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "payload"
    else:
        base = Path(__file__).resolve().parent.parent / "dist"
    return base / APP_EXE_NAME
