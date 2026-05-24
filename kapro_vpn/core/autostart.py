"""Auto-start with the OS, cross-platform.

Per-OS storage:

  Windows
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run, value "KaproVPN"

  macOS
    ~/Library/LaunchAgents/com.kaprovpn.autostart.plist
    (launchd loads it at user login when RunAtLoad=true)

  Linux
    ~/.config/autostart/kaprovpn.desktop
    (XDG Autostart spec — honored by GNOME, KDE, XFCE, Cinnamon, etc.)

In every case we pass `--minimized` so the boot launch goes to tray
without showing the main window — appropriate for "boot with the OS,
sit quietly until I open it".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "KaproVPN"
MINIMIZED_FLAG = "--minimized"

# macOS
MAC_PLIST_LABEL = "com.kaprovpn.autostart"
MAC_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{MAC_PLIST_LABEL}.plist"

# Linux
LINUX_DESKTOP_PATH = Path.home() / ".config" / "autostart" / "kaprovpn.desktop"


def _winreg():
    import winreg
    return winreg


def _is_frozen() -> bool:
    """True if running from a PyInstaller-built binary (sys.frozen attr)."""
    return getattr(sys, "frozen", False)


def _autostart_command(minimized: bool = True) -> str:
    """Build the command string to register.

    Frozen → just the bundled binary path; dev → python interpreter + script.
    Always wrapped in quotes so spaces in paths don't break parsing.
    """
    if _is_frozen():
        exe = sys.executable
        cmd = f'"{exe}"'
    else:
        exe = sys.executable
        script = str(Path(sys.argv[0]).resolve()) if sys.argv[0] else ""
        if not script:
            return ""
        cmd = f'"{exe}" "{script}"'
    if minimized:
        cmd += f" {MINIMIZED_FLAG}"
    return cmd


# ---------------------------------------------------------------- public API

def is_enabled() -> bool:
    if sys.platform == "win32":
        return _win_is_enabled()
    if sys.platform == "darwin":
        return MAC_PLIST_PATH.is_file()
    if sys.platform == "linux":
        return LINUX_DESKTOP_PATH.is_file()
    return False


def enable(minimized: bool = True) -> bool:
    if sys.platform == "win32":
        return _win_enable(minimized)
    if sys.platform == "darwin":
        return _mac_enable(minimized)
    if sys.platform == "linux":
        return _linux_enable(minimized)
    return False


def disable() -> bool:
    if sys.platform == "win32":
        return _win_disable()
    if sys.platform == "darwin":
        return _mac_disable()
    if sys.platform == "linux":
        return _linux_disable()
    return True  # nothing to remove on unsupported OSes


def configured_command() -> Optional[str]:
    """Return the currently-registered command, or None if not set.

    Useful for debugging "why doesn't it auto-start?" — caller can show
    this in the Settings page so the user sees exactly what's registered.
    """
    if sys.platform == "win32":
        return _win_configured()
    if sys.platform == "darwin":
        if MAC_PLIST_PATH.is_file():
            return MAC_PLIST_PATH.read_text(encoding="utf-8")
        return None
    if sys.platform == "linux":
        if LINUX_DESKTOP_PATH.is_file():
            return LINUX_DESKTOP_PATH.read_text(encoding="utf-8")
        return None
    return None


# ===========================================================================
# Windows — HKCU Run key
# ===========================================================================

def _win_is_enabled() -> bool:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except (FileNotFoundError, OSError):
        return False


def _win_enable(minimized: bool) -> bool:
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


def _win_disable() -> bool:
    winreg = _winreg()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _win_configured() -> Optional[str]:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except (FileNotFoundError, OSError):
        return None


# ===========================================================================
# macOS — LaunchAgent plist in ~/Library/LaunchAgents
# ===========================================================================

def _mac_program_argv(minimized: bool) -> list[str]:
    """Resolve the absolute path to our binary + flags as a launchd ProgramArguments list."""
    if _is_frozen():
        argv = [sys.executable]
    else:
        script = str(Path(sys.argv[0]).resolve()) if sys.argv[0] else ""
        if not script:
            return []
        argv = [sys.executable, script]
    if minimized:
        argv.append(MINIMIZED_FLAG)
    return argv


def _mac_enable(minimized: bool) -> bool:
    argv = _mac_program_argv(minimized)
    if not argv:
        return False
    args_xml = "\n        ".join(
        f"<string>{_xml_escape(a)}</string>" for a in argv
    )
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{MAC_PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        {args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
"""
    try:
        MAC_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MAC_PLIST_PATH.write_text(plist, encoding="utf-8")
        # Best-effort launchctl load — if it fails the plist will still
        # auto-load on next login, so don't propagate the error.
        try:
            import subprocess
            subprocess.run(["launchctl", "load", "-w", str(MAC_PLIST_PATH)],
                           check=False, capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        return True
    except OSError:
        return False


def _mac_disable() -> bool:
    try:
        if MAC_PLIST_PATH.is_file():
            try:
                import subprocess
                subprocess.run(["launchctl", "unload", "-w", str(MAC_PLIST_PATH)],
                               check=False, capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            MAC_PLIST_PATH.unlink()
        return True
    except OSError:
        return False


# ===========================================================================
# Linux — XDG Autostart .desktop entry
# ===========================================================================

def _linux_exec_command(minimized: bool) -> str:
    """Build the Exec= value for the .desktop entry.

    .desktop spec mandates single-string commands with %f/%U placeholders;
    we have none of those, so just shell-quote our argv components.
    """
    if _is_frozen():
        exe = sys.executable
        parts = [_sh_quote(exe)]
    else:
        exe = sys.executable
        script = str(Path(sys.argv[0]).resolve()) if sys.argv[0] else ""
        if not script:
            return ""
        parts = [_sh_quote(exe), _sh_quote(script)]
    if minimized:
        parts.append(MINIMIZED_FLAG)
    return " ".join(parts)


def _linux_enable(minimized: bool) -> bool:
    exec_cmd = _linux_exec_command(minimized)
    if not exec_cmd:
        return False
    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=KaproVPN\n"
        "Comment=VPN client with split routing for Russian sites\n"
        f"Exec={exec_cmd}\n"
        "Icon=kaprovpn\n"
        "Terminal=false\n"
        "Categories=Network;\n"
        "X-GNOME-Autostart-enabled=true\n"
        "StartupNotify=false\n"
    )
    try:
        LINUX_DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        LINUX_DESKTOP_PATH.write_text(contents, encoding="utf-8")
        # XDG autostart doesn't require the executable bit, but some old
        # versions of Cinnamon refused entries without it — belt+braces.
        try:
            mode = LINUX_DESKTOP_PATH.stat().st_mode
            LINUX_DESKTOP_PATH.chmod(mode | 0o100)
        except OSError:
            pass
        return True
    except OSError:
        return False


def _linux_disable() -> bool:
    try:
        if LINUX_DESKTOP_PATH.is_file():
            LINUX_DESKTOP_PATH.unlink()
        return True
    except OSError:
        return False


# ===========================================================================
# Helpers
# ===========================================================================

def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def _sh_quote(s: str) -> str:
    """Bare-minimum POSIX shell quoting for .desktop Exec= field."""
    if not s:
        return "''"
    if all(c.isalnum() or c in "_-./:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"
