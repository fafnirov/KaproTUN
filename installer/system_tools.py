"""Full paths of the Windows system tools this client starts.

`subprocess.run(["netsh", ...])` does not mean "the system's netsh". Windows
looks for a bare name in the folder of the running program first, then in
the current folder, and only then in System32. KaproTUN is installed per
user, in a folder that needs no rights to write to, and it runs with
administrator rights: a `netsh.exe` put next to `KaproTUN.exe` by any program
of the same user would have been started in the system tool's place, with
those rights. (The engine and the installer were closed the same way in
v4.1.1; this is the rest of that audit item.)

So a system tool is always named by its full path in the system folder, which
only an administrator can write to. Elsewhere than Windows the name is
returned as it is — the callers are Windows-only code paths, and the tests
run them on Linux.

This is the installer's copy of kapro_tun/core/win_tools.py: the installer is
a separate bundle that does not carry kapro_tun.core. A test keeps the two in
step.
"""
from __future__ import annotations

import os
import sys

# Not directly in System32.
_ELSEWHERE = {"powershell": ("WindowsPowerShell", "v1.0", "powershell.exe")}


def _system_dir() -> str:
    """The system folder, asked from the system (not from %SystemRoot%, which
    is an environment variable and so belongs to whoever started us)."""
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(buf, len(buf))
    if not 0 < size < len(buf):
        raise OSError("the system folder could not be determined")
    return buf.value


def tool(name: str) -> str:
    """`netsh` → `C:\\Windows\\System32\\netsh.exe`. On other systems, `name`."""
    if sys.platform != "win32":
        return name
    if any(sep in name for sep in ("/", "\\", ":")) or ".." in name:
        raise ValueError(f"not a system tool name: {name!r}")
    stem = name[:-4] if name.lower().endswith(".exe") else name
    parts = _ELSEWHERE.get(stem.lower(), (stem + ".exe",))
    return "\\".join((_system_dir().rstrip("\\"), *parts))


def powershell_env():
    """Environment for a PowerShell we start: modules from the system only.

    Starting powershell.exe by its full path is half of it. The cmdlets we
    call (Get-NetRoute, Get-NetFirewallRule, ...) live in modules that
    PowerShell loads on first use, searching $env:PSModulePath — and that
    path begins with the user's own Documents\\WindowsPowerShell\\Modules. A
    module of the same name put there would be loaded into our PowerShell,
    which runs with administrator rights.

    With PSModulePath pointing at the system's module folder, PowerShell
    searches only there and in Program Files (it adds that one itself) —
    both writable by administrators alone. Checked on Windows 11: the user
    folder is gone from the path and every cmdlet we use still resolves.

    Returns None outside Windows (subprocess then inherits the environment).
    """
    if sys.platform != "win32":
        return None
    modules = "\\".join((_system_dir().rstrip("\\"), "WindowsPowerShell", "v1.0", "Modules"))
    return dict(os.environ, PSModulePath=modules)
