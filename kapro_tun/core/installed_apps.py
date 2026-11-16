"""Read Windows application registrations and Start-menu shortcuts, never run them."""
from __future__ import annotations

import json
import ntpath
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import win_tools


@dataclass(frozen=True)
class InstalledApp:
    name: str
    path: str

    @property
    def executable(self) -> str:
        return ntpath.basename(self.path).lower()


def local_executable(raw: str) -> str:
    path = os.path.expandvars(str(raw or "").strip().strip('"'))
    drive, _ = ntpath.splitdrive(path)
    # Do not ask the shell/icon provider to access network shares.
    if len(drive) != 2 or drive[1] != ":" or not ntpath.isabs(path):
        return ""
    if ntpath.splitext(path)[1].lower() != ".exe":
        return ""
    if ntpath.basename(path).lower().startswith(("unins", "uninstall")):
        return ""
    return path


def discover() -> list[InstalledApp]:
    if sys.platform != "win32":
        return []
    import winreg
    found = {}

    def add(name, raw):
        path = local_executable(raw)
        if path and Path(path).is_file():
            found.setdefault(path.lower(), InstalledApp(str(name), path))

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
                                    0, winreg.KEY_READ | view) as root:
                    for i in range(winreg.QueryInfoKey(root)[0]):
                        name = winreg.EnumKey(root, i)
                        try:
                            with winreg.OpenKey(root, name) as key:
                                add(ntpath.splitext(name)[0], winreg.QueryValue(key, None))
                        except OSError:
                            continue
            except OSError:
                continue
    script = r"""$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$shell = New-Object -ComObject WScript.Shell
$roots = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('CommonPrograms'))
$items = @(Get-ChildItem -LiteralPath $roots -Filter '*.lnk' -Recurse |
    Select-Object -First 600 | ForEach-Object {
        $shortcut = $shell.CreateShortcut($_.FullName)
        [pscustomobject]@{name=$_.BaseName; path=$shortcut.TargetPath}
    })
ConvertTo-Json -InputObject $items -Compress
"""
    try:
        proc = subprocess.run([win_tools.tool("powershell"), "-NoProfile", "-NonInteractive",
                               "-WindowStyle", "Hidden", "-Command", script],
                              capture_output=True, encoding="utf-8", errors="replace", timeout=20, check=False,
                              creationflags=subprocess.CREATE_NO_WINDOW,
                              startupinfo=win_tools.hidden_startupinfo(), env=win_tools.powershell_env())
        for item in json.loads(proc.stdout or "[]"):
            add(item.get("name", ""), item.get("path", ""))
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
        pass  # App Paths and the manual file picker remain available.
    return sorted(found.values(), key=lambda app: (app.name.casefold(), app.path.casefold()))
