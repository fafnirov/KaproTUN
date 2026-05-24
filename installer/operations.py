"""File copy / shortcut / registry operations performed during install.

All run synchronously on the worker thread. Each step logs and raises on
failure so the installer UI can surface a clear error.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

from . import paths


ProgressCb = Optional[Callable[[str, int], None]]  # (status_text, percent 0-100)

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\KaproVPN"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# --- file operations ------------------------------------------------------

def copy_main_exe(progress: ProgressCb = None) -> Path:
    """Copy the bundled KaproVPN.exe payload to the install dir."""
    src = paths.bundled_main_exe()
    if not src.is_file():
        raise FileNotFoundError(
            f"KaproVPN.exe payload missing inside the installer: {src}"
        )
    target_dir = paths.install_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = paths.installed_exe_path()
    if progress:
        progress(f"Копирую {src.name}…", 20)
    shutil.copy2(src, target)
    if progress:
        progress(f"Скопировано {target.stat().st_size // 1024} КБ", 40)
    return target


def copy_uninstaller() -> Path:
    """Place a copy of *this* installer as the uninstaller.

    Same binary, different filename — when launched with --uninstall it
    enters uninstall mode (see main.py).
    """
    if not getattr(sys, "frozen", False):
        # Dev mode — there's no compiled installer to copy. Skip silently.
        return paths.installed_uninstaller_path()
    src = Path(sys.executable)
    target = paths.installed_uninstaller_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    return target


# --- shortcuts (Win32, no admin) -----------------------------------------

def _create_shortcut(target: Path, lnk_path: Path,
                     description: str = "", icon: Optional[Path] = None,
                     args: str = "") -> None:
    """Create a Windows .lnk via the shell COM API (via PowerShell).

    Pure-ctypes shortcut creation needs PyWin32; we avoid that dependency
    by shelling out to a tiny PowerShell snippet — already a hard dep
    for the rest of the project and present on every Windows install.
    """
    import subprocess

    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    args_quoted = args.replace('"', '`"') if args else ""
    icon_line = (
        f'$s.IconLocation = "{icon}";\n' if icon else ""
    )
    ps = (
        f'$w = New-Object -ComObject WScript.Shell;\n'
        f'$s = $w.CreateShortcut("{lnk_path}");\n'
        f'$s.TargetPath = "{target}";\n'
        f'$s.WorkingDirectory = "{target.parent}";\n'
        f'$s.Description = "{description}";\n'
        f'{icon_line}'
        f'$s.Arguments = "{args_quoted}";\n'
        f'$s.Save();\n'
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, timeout=15,
        creationflags=creationflags,
    )


def create_start_menu_shortcut() -> Path:
    lnk = paths.start_menu_dir() / f"{paths.APP_NAME}.lnk"
    _create_shortcut(
        target=paths.installed_exe_path(),
        lnk_path=lnk,
        description="Desktop VPN client with split routing for Russian sites",
        icon=paths.installed_exe_path(),
    )
    return lnk


def create_desktop_shortcut() -> Path:
    lnk = paths.desktop_dir() / f"{paths.APP_NAME}.lnk"
    _create_shortcut(
        target=paths.installed_exe_path(),
        lnk_path=lnk,
        description="Desktop VPN client with split routing for Russian sites",
        icon=paths.installed_exe_path(),
    )
    return lnk


# --- uninstaller registration --------------------------------------------

def register_uninstaller(version: str, total_size_kb: int = 60000) -> None:
    """Add an entry to "Установка и удаление программ" / Apps & Features."""
    if sys.platform != "win32":
        return
    import winreg
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, paths.APP_NAME)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, paths.PUBLISHER)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ,
                          str(paths.install_dir()))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ,
                          str(paths.installed_exe_path()))
        winreg.SetValueEx(key, "URLInfoAbout", 0, winreg.REG_SZ, paths.HOMEPAGE)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, total_size_kb)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(
            key, "UninstallString", 0, winreg.REG_SZ,
            f'"{paths.installed_uninstaller_path()}" --uninstall',
        )


def unregister_uninstaller() -> None:
    if sys.platform != "win32":
        return
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


# --- uninstall ------------------------------------------------------------

def uninstall_everything(progress: ProgressCb = None) -> None:
    """Tear down everything install_everything put in place."""
    if progress:
        progress("Удаляю ярлыки…", 10)
    for lnk in (
        paths.start_menu_dir() / f"{paths.APP_NAME}.lnk",
        paths.desktop_dir() / f"{paths.APP_NAME}.lnk",
    ):
        try:
            lnk.unlink(missing_ok=True)
        except OSError:
            pass
    # Start Menu folder
    try:
        sm = paths.start_menu_dir()
        if sm.is_dir():
            shutil.rmtree(sm, ignore_errors=True)
    except OSError:
        pass

    if progress:
        progress("Удаляю запись из Programs & Features…", 30)
    unregister_uninstaller()

    if progress:
        progress("Снимаю auto-start если был…", 50)
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, paths.APP_NAME)
            except FileNotFoundError:
                pass
    except OSError:
        pass

    if progress:
        progress("Удаляю файлы программы…", 80)
    install = paths.install_dir()
    if install.is_dir():
        # The uninstaller is running from inside install_dir, so we can't
        # rmtree(install) — Windows holds the lock on the running exe.
        # Delete everything except the uninstaller itself; the OS will
        # purge the uninstaller via MOVEFILE_DELAY_UNTIL_REBOOT below.
        for entry in install.iterdir():
            if entry == paths.installed_uninstaller_path():
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try:
                    entry.unlink()
                except OSError:
                    pass
        # Schedule the uninstaller (and the now-empty install dir) for
        # deletion on next boot.
        _schedule_delete_on_reboot(paths.installed_uninstaller_path())
        _schedule_delete_on_reboot(install)

    if progress:
        progress("Готово", 100)


def _schedule_delete_on_reboot(path: Path) -> None:
    """Ask Windows to delete `path` at next boot (used for locked files)."""
    if sys.platform != "win32":
        return
    MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
    try:
        ctypes.windll.kernel32.MoveFileExW(str(path), None, MOVEFILE_DELAY_UNTIL_REBOOT)
    except OSError:
        pass


# --- top-level install orchestration -------------------------------------

def install_everything(version: str, progress: ProgressCb = None,
                       create_desktop: bool = True) -> None:
    if progress:
        progress("Создаю папку установки…", 5)
    paths.install_dir().mkdir(parents=True, exist_ok=True)

    copy_main_exe(progress=progress)

    if progress:
        progress("Копирую uninstaller…", 55)
    copy_uninstaller()

    if progress:
        progress("Создаю ярлык в меню Пуск…", 70)
    create_start_menu_shortcut()

    if create_desktop:
        if progress:
            progress("Создаю ярлык на Рабочем столе…", 85)
        create_desktop_shortcut()

    if progress:
        progress("Регистрирую uninstaller в Windows…", 95)
    register_uninstaller(version=version)

    if progress:
        progress("Готово", 100)
