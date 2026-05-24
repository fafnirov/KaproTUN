"""Auto-start with Windows via HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.

We register *this* exact Python interpreter + run.py path (or sys.argv[0]
if frozen via PyInstaller), so a user who's moved the project folder
gets the registry entry updated on the next toggle.

The launched process picks up `--minimized` and boots straight into the
tray without showing the main window — appropriate for "boot with the
OS, sit quietly until I open it".
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "KaproVPN"
MINIMIZED_FLAG = "--minimized"


def _winreg():
    import winreg
    return winreg


def _is_frozen() -> bool:
    """True if running from a PyInstaller-built .exe (sys.frozen attr)."""
    return getattr(sys, "frozen", False)


def _autostart_command(minimized: bool = True) -> str:
    """Build the command string to register in the Run key.

    Quoted so spaces in paths don't break parsing. Includes
    --minimized so the boot launch goes to tray, not foreground.
    """
    if _is_frozen():
        # PyInstaller .exe — sys.executable IS the bundled app
        exe = sys.executable
        cmd = f'"{exe}"'
    else:
        # Dev mode: python.exe + path to run.py
        exe = sys.executable
        script = str(Path(sys.argv[0]).resolve()) if sys.argv[0] else ""
        if not script:
            return ""
        cmd = f'"{exe}" "{script}"'
    if minimized:
        cmd += f" {MINIMIZED_FLAG}"
    return cmd


def is_enabled() -> bool:
    """True if the Run-key value for KaproVPN exists."""
    if sys.platform != "win32":
        return False
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except (FileNotFoundError, OSError):
        return False


def enable(minimized: bool = True) -> bool:
    """Add (or update) the Run-key entry. Returns True on success."""
    if sys.platform != "win32":
        return False
    cmd = _autostart_command(minimized)
    if not cmd:
        return False
    winreg = _winreg()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
        return True
    except OSError:
        return False


def disable() -> bool:
    """Remove the Run-key entry. Returns True on success (or already gone)."""
    if sys.platform != "win32":
        return True
    winreg = _winreg()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True  # already gone
    except OSError:
        return False


def configured_command() -> Optional[str]:
    """Return the currently-registered command string, or None if not set."""
    if sys.platform != "win32":
        return None
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except (FileNotFoundError, OSError):
        return None
