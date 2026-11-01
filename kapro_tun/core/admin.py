"""Privilege escalation, cross-platform.

TUN mode requires root/admin on every OS:
  - Windows  → UAC prompt via ShellExecuteW("runas")
  - macOS    → AuthorizationServices via osascript
               "do shell script ... with administrator privileges"
  - Linux    → pkexec (polkit GUI prompt) if available,
               otherwise instruct the user to `sudo`

We don't ship a system service, so each TUN-connect cycle either runs
us as root from the start, or the relaunch helper kicks in.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
from typing import Sequence


def is_admin() -> bool:
    """True if the current process has root / UAC-elevated privileges."""
    if sys.platform == "win32":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    # Unix-likes (macOS + Linux + BSD)
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def relaunch_as_admin(argv: Sequence[str] = None) -> int:
    """Spawn an elevated copy of this app. Returns an opaque success code
    (>0 = launching, <=0 = user declined / unsupported). Caller should
    exit immediately on success.
    """
    if argv is None:
        argv = list(sys.argv)
    else:
        argv = list(argv)

    if sys.platform == "win32":
        return _win_relaunch(argv)
    if sys.platform == "darwin":
        return _mac_relaunch(argv)
    if sys.platform == "linux":
        return _linux_relaunch(argv)
    return 0


# ==========================================================================
# Windows — UAC via ShellExecuteW
# ==========================================================================

def _win_relaunch(argv: list[str]) -> int:
    import subprocess

    # A frozen executable already IS argv[0]; passing it again makes it an
    # application argument. In development Python still needs the script.
    args = argv[1:] if getattr(sys, "frozen", False) else argv
    executable = sys.executable
    if not getattr(sys, "frozen", False):
        # Elevating python.exe creates a console before our GUI can open.
        windowless = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.isfile(windowless):
            executable = windowless
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.restype = ctypes.c_ssize_t
    rc = shell_execute(
        None, "runas", executable, subprocess.list2cmdline(args), None, 1,
    )
    return int(rc)


def require_windows_admin() -> int | None:
    """Before normal startup: None = continue, otherwise exit with this code.

    A declined UAC prompt must never leave an unprivileged client running.
    Keep this before the singleton guard and all startup network cleanup.
    """
    if sys.platform != "win32" or is_admin():
        return None
    try:
        if relaunch_as_admin() > 32:
            return 0
    except OSError:
        pass
    from .i18n import tr
    ctypes.windll.user32.MessageBoxW(
        None,
        tr("err.admin_required_win"),
        "KaproTUN", 0x10,
    )
    return 1


# ==========================================================================
# macOS — osascript with "do shell script ... with administrator privileges"
# ==========================================================================

def _mac_relaunch(argv: list[str]) -> int:
    """Re-launch via osascript so the user gets the native admin GUI prompt.

    osascript's `do shell script ... with administrator privileges` is the
    standard pattern for unprivileged apps that need one elevated child.
    """
    # AppleScript quoting is its own little headache. Each argv item ends
    # up inside POSIX single-quotes, which we double-up to escape any '
    # inside.
    quoted = " ".join(_posix_single_quote(a) for a in argv)
    script = f'do shell script "{_applescript_escape(quoted)}" with administrator privileges'
    import subprocess
    try:
        proc = subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Caller will exit immediately; osascript stays alive on its own.
        return 1 if proc.poll() in (None, 0) else 0
    except (OSError, subprocess.SubprocessError):
        return 0


def _posix_single_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _applescript_escape(s: str) -> str:
    """Escape for an AppleScript string literal (the double-quoted kind)."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ==========================================================================
# Linux — pkexec (polkit GUI) preferred, fall back to "use sudo" message
# ==========================================================================

def _linux_relaunch(argv: list[str]) -> int:
    """Re-launch via pkexec if installed. pkexec pops a polkit dialog
    that asks for the user's password and runs the child as root.

    pkexec preserves DISPLAY/XAUTHORITY/WAYLAND_DISPLAY by default on
    modern distros, so the Qt UI shows up correctly. If pkexec isn't
    on PATH (Alpine, minimal Debian, etc.), return 0 — the controller
    will surface a "sudo from terminal" hint.
    """
    if not shutil.which("pkexec"):
        return 0
    import subprocess
    try:
        # pkexec usage: `pkexec [--user USER] PROGRAM [ARGS...]`
        # We re-launch sys.executable with our argv, same as Windows.
        cmd = ["pkexec", sys.executable] + argv
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return 1 if proc.poll() in (None, 0) else 0
    except (OSError, subprocess.SubprocessError):
        return 0


# ==========================================================================
# Capability hints for the UI — does this platform have a clean GUI
# elevation flow, or should we instruct the user to relaunch by hand?
# ==========================================================================

def has_gui_elevation() -> bool:
    """True if relaunch_as_admin() will pop a native GUI prompt rather
    than silently failing or requiring a terminal. The UI uses this to
    decide between "click here to elevate" and "open a terminal and
    run `sudo ./KaproTUN`".
    """
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        return True
    if sys.platform == "linux":
        return shutil.which("pkexec") is not None
    return False
