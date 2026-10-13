"""File copy / shortcut / registry operations performed during install.

All run synchronously on the worker thread. Each step logs and raises on
failure so the installer UI can surface a clear error.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from . import paths, payload_digest, system_tools


ProgressCb = Optional[Callable[[str, int], None]]  # (status_text, percent 0-100)

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\KaproTUN"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- stop the running app before touching its files ----------------------
#
# Windows refuses to overwrite or delete a *running* executable — the open
# handle makes it un-writable and un-deletable (PermissionError [Errno 13] /
# ERROR_SHARING_VIOLATION). Reinstall (overwrite KaproTUN.exe) and uninstall
# (delete it) must therefore ensure no KaproTUN.exe is running first.

def _exe_is_locked(path: Path) -> bool:
    """True if `path` exists but can't be opened for writing.

    A running .exe denies write-sharing on Windows, so opening it in append
    mode raises. Append (not truncate) + immediate close leaves the file
    byte-for-byte unchanged, so this is a safe, non-destructive probe.
    """
    if not path.exists():
        return False
    try:
        with open(path, "ab"):
            return False
    except OSError:
        return True


def _wait_until_unlocked(path: Path, timeout: float) -> bool:
    """Poll until `path` is writable again, or `timeout` seconds elapse."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _exe_is_locked(path):
            return True
        time.sleep(0.25)
    return not _exe_is_locked(path)


def _request_graceful_quit() -> None:
    """Best-effort: ask a running KaproTUN to quit cleanly via its
    single-instance pipe, so it disconnects (restoring the system proxy +
    firewall rules) before we touch its files.

    No-op if nothing is listening, or if the running version predates
    CMD_QUIT support (it'll just ignore the message) — the caller falls
    back to a force-kill in that case.
    """
    try:
        from PySide6.QtNetwork import QLocalSocket

        from kapro_tun.gui.singleton import CMD_QUIT, SERVER_NAME
    except Exception:
        return
    try:
        sock = QLocalSocket()
        sock.connectToServer(SERVER_NAME)
        if sock.waitForConnected(700):
            sock.write(CMD_QUIT)
            sock.flush()
            sock.waitForBytesWritten(700)
            sock.disconnectFromServer()
    except Exception:
        pass


def _taskkill_force(image: str) -> None:
    """taskkill /F /T the named image (and its child xray/tun2socks/hysteria)."""
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            [system_tools.tool("taskkill"), "/F", "/T", "/IM", image],
            capture_output=True, timeout=10, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def stop_running_app(progress: ProgressCb = None) -> None:
    """Make sure no running KaproTUN.exe holds a lock on the install dir.

    Escalates politely:
      1. If the exe isn't even locked, return immediately (fresh install,
         or the app is already closed).
      2. Ask the app to quit cleanly via its pipe, wait a few seconds.
      3. Still locked? Force-kill it (and its helper children), wait again.
      4. Still locked? Raise a clear, actionable error rather than letting
         the caller hit a cryptic PermissionError mid-write.
    """
    exe = paths.installed_exe_path()
    if not _exe_is_locked(exe):
        return

    if progress:
        progress("Закрываю запущенный KaproTUN…", 2)
    _request_graceful_quit()
    if _wait_until_unlocked(exe, timeout=6.0):
        return

    if progress:
        progress("Завершаю процесс KaproTUN…", 3)
    _taskkill_force(paths.APP_EXE_NAME)
    if _wait_until_unlocked(exe, timeout=6.0):
        return

    raise RuntimeError(
        "KaproTUN запущен и не закрывается автоматически.\n\n"
        "Закрой его вручную (правый клик по иконке в трее → «Выход») "
        "и запусти установщик заново."
    )


def _cleanup_network_state() -> None:
    """Undo network changes a force-killed app couldn't restore itself.

    Only runs as a safety net for the force-kill path of uninstall — when
    the app quits cleanly via the pipe it restores these itself. Everything
    here is best-effort and conservative: we only clear a system proxy that
    points at *our own* local port (never a real corporate/personal one),
    and only remove firewall rules we know are ours.

    Without this, uninstalling while connected in HTTP-proxy mode (or with
    the kill-switch armed) would leave the machine with no internet and no
    app left to heal it on next launch.
    """
    _clear_our_system_proxy()
    for mod_name in ("killswitch", "ipv6_block", "webrtc_block"):
        try:
            mod = __import__(f"kapro_tun.core.{mod_name}", fromlist=[mod_name])
            if mod.is_active():
                mod.remove()
        except Exception:
            pass


def _clear_our_system_proxy() -> None:
    try:
        from kapro_tun.core import system_proxy
    except Exception:
        return
    try:
        state = system_proxy.get_state()
    except Exception:
        return
    if not state or not state.get("enable"):
        return
    host, _, port_s = str(state.get("server", "")).rpartition(":")
    if host not in ("127.0.0.1", "localhost", "::1"):
        return  # a real proxy, not ours — leave it alone
    listen_port = 2080
    try:
        from kapro_tun.core import storage
        listen_port = int(storage.load_settings().get("listen_port", 2080))
    except Exception:
        pass
    try:
        if int(port_s) != listen_port:
            return
    except ValueError:
        return
    # Points at our local port, and we just killed everything listening
    # there — it's dead. Clear it so the user keeps their internet.
    try:
        system_proxy.disable_proxy()
    except Exception:
        pass


# --- file operations ------------------------------------------------------

def acquire_main_exe(version: str, progress: ProgressCb = None) -> Path:
    """Get KaproTUN.exe into the install dir.

    Order of preference:
      1. Locally embedded payload (if anyone re-enables the embed in the
         spec — keeps offline-installer scenarios working).
      2. Download of the matching version: our mirror, then GitHub Releases.

    Range 5..50 of the overall progress is reserved for this step.
    """
    target_dir = paths.install_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = paths.installed_exe_path()

    embedded = paths.bundled_main_exe()
    if embedded is not None:
        if progress:
            progress(f"Копирую {embedded.name}…", 20)
        shutil.copy2(embedded, target)
        if progress:
            progress(f"Скопировано {target.stat().st_size // (1024 * 1024)} МБ", 50)
        return target

    return _download_main_exe(version, target, progress)


# Hard ceiling for the portable exe download. The real binary is ~40-60 MB;
# this rejects a hostile/broken mirror that would otherwise stream unbounded
# data onto the user's disk. (Inline — the installer is a standalone bundle
# and can't import kapro_tun.core.net_download.)
_MAX_MAIN_EXE = 150 * 1024 * 1024


def _release_digest(version: str) -> str:
    """SHA-256 of KaproTUN.exe as GitHub reports it for the release of
    `version`, or "" if it cannot be had. Used only by an installer that was
    built without its own digest (payload_digest)."""
    api = f"https://api.github.com/repos/fafnirov/KaproTUN/releases/tags/v{version}"
    try:
        r = requests.get(api, timeout=(10, 15), proxies={"http": "", "https": ""})
        r.raise_for_status()
        for asset in r.json().get("assets") or []:
            if str(asset.get("name") or "") == paths.APP_EXE_NAME:
                return str(asset.get("digest") or "").removeprefix("sha256:").strip().lower()
    except (requests.RequestException, ValueError, AttributeError):
        pass
    return ""


def _sha256_of(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


_download_host = ""


def download_source() -> str:
    """The host KaproTUN.exe is being fetched from right now ("kaprovpn.pro",
    "github.com"), for the installer to show next to the megabytes."""
    return _download_host


def _main_exe_sources(version: str) -> list:
    """Where KaproTUN.exe is fetched from, in order: (host, url).

    Our mirror first. Release downloads from github.com stall for many
    people in Russia — the page opens, the file starts, and then the read
    times out (a user reported exactly that on a 126 Mbit/s line) — and
    until v4.1.3 GitHub was the only place this installer asked, once. The
    mirror is a convenience, not a trust decision: whatever arrives must
    match the digest, from either place."""
    return [("kaprovpn.pro", paths.mirror_exe_url(version)),
            ("github.com", paths.github_release_exe_url(version))]


def _fetch_main_exe(url: str, tmp: Path, progress: ProgressCb) -> None:
    """Stream `url` into `tmp`. Raises requests.RequestException on a network
    failure and RuntimeError when the size limit is exceeded."""
    # Bypass system proxy — installer runs on a fresh machine where
    # there shouldn't be one, but if a previous KaproTUN crashed and
    # left a stale 127.0.0.1:2080 entry in the registry, this would
    # try to tunnel through a dead port and fail with WinError 10061.
    with requests.get(url, stream=True, timeout=(15, 30),
                      proxies={"http": "", "https": ""}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        # Reject an over-size download up front (declared size).
        if total > _MAX_MAIN_EXE:
            raise RuntimeError(
                f"файл подозрительно большой ({total // (1024 * 1024)} МБ, лимит "
                f"{_MAX_MAIN_EXE // (1024 * 1024)} МБ)")
        downloaded = 0
        last_pct = 5
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                # ...and abort mid-stream if the server lied / omitted it.
                if downloaded > _MAX_MAIN_EXE:
                    raise RuntimeError("скачивание превысило лимит размера")
                if not (progress and total):
                    continue
                # Map download progress to the 5..50 slice of overall.
                pct = 5 + int(downloaded * 45 / total)
                if pct == last_pct:
                    continue
                last_pct = pct
                mb = downloaded // (1024 * 1024)
                total_mb = total // (1024 * 1024)
                progress(f"Скачиваю KaproTUN.exe… {mb} / {total_mb} МБ", pct)


def _download_main_exe(version: str, target: Path,
                       progress: ProgressCb = None) -> Path:
    # What the download must hash to. Known before a single byte is fetched:
    # from this installer itself when it is a release build, else from the
    # release. Without it nothing is installed — an application that runs
    # with administrator rights is not put on disk unverified.
    want = (payload_digest.MAIN_EXE_SHA256 or "").strip().lower() or _release_digest(version)
    if len(want) != 64:
        raise RuntimeError(
            "Не удалось получить контрольную сумму KaproTUN.exe, поэтому "
            "установка остановлена: файл нечем проверить.\n\n"
            "Проверь интернет и доступ к github.com и запусти установщик заново."
        )
    if progress:
        progress(f"Скачиваю KaproTUN.exe v{version}…", 5)
    # Download to a sibling temp file, then os.replace() it into place
    # atomically. Two reasons: (a) a failed/interrupted download never
    # leaves a half-written KaproTUN.exe behind, and (b) if the target is
    # still locked (app didn't fully exit), the swap fails cleanly with a
    # clear message instead of corrupting the installed binary.
    tmp = target.with_name(target.name + ".download")
    problems: list = []
    global _download_host
    for host, url in _main_exe_sources(version):
        _download_host = host
        try:
            _fetch_main_exe(url, tmp, progress)
        except (requests.RequestException, RuntimeError, OSError) as e:
            _silent_unlink(tmp)
            problems.append(f"{host}: {e}")
            continue
        got = _sha256_of(tmp)
        if got == want:
            break
        # Not the file this installer was built with (a stale or damaged
        # copy, or one changed on the way). It is not installed; the next
        # source may have the right one.
        _silent_unlink(tmp)
        problems.append(f"{host}: контрольная сумма не совпала (получена {got[:16]}…)")
    else:
        raise RuntimeError(
            "Не удалось скачать KaproTUN.exe:\n"
            + "\n".join(problems)
            + "\n\nПроверь интернет и запусти установщик заново. Если не помогает, "
            f"скачай KaproTUN.exe вручную:\n{paths.release_page_url(version)}"
        )

    try:
        os.replace(tmp, target)
    except OSError as e:
        _silent_unlink(tmp)
        raise RuntimeError(
            f"Не удалось записать KaproTUN.exe — файл занят:\n{e}\n\n"
            "Закрой KaproTUN (правый клик по иконке в трее → «Выход») "
            "и запусти установщик заново."
        ) from e
    if progress:
        progress(f"Скачано {target.stat().st_size // (1024 * 1024)} МБ", 50)
    return target


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


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
        [system_tools.tool("powershell"), "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, timeout=15,
        creationflags=creationflags, env=system_tools.powershell_env(),
    )


def create_start_menu_shortcut() -> Path:
    lnk = paths.start_menu_dir() / f"{paths.APP_NAME}.lnk"
    _create_shortcut(
        target=paths.installed_exe_path(),
        lnk_path=lnk,
        description="Desktop VPN client with split-routing direct list",
        icon=paths.installed_exe_path(),
    )
    return lnk


def create_desktop_shortcut() -> Path:
    lnk = paths.desktop_dir() / f"{paths.APP_NAME}.lnk"
    _create_shortcut(
        target=paths.installed_exe_path(),
        lnk_path=lnk,
        description="Desktop VPN client with split-routing direct list",
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
    # The app holds a lock on its own exe while running, so deleting it
    # would silently fail (the unlink below swallows OSError) and leave a
    # half-removed install. Stop it first — gracefully if possible, so it
    # restores the system proxy / firewall on the way out.
    stop_running_app(progress)
    # Safety net for the force-kill path: if the app couldn't restore the
    # system proxy / firewall itself, undo our own entries so the machine
    # keeps its internet after the app is gone.
    _cleanup_network_state()

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
        progress("Удаляю запись из «Установка и удаление программ»…", 30)
    unregister_uninstaller()

    if progress:
        progress("Снимаю автозапуск…", 50)
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
    # A reinstall overwrites KaproTUN.exe in place — if the app is still
    # running, Windows holds the file lock and the write fails with
    # PermissionError. Stop it first (no-op on a fresh install).
    stop_running_app(progress)

    if progress:
        progress("Создаю папку установки…", 5)
    paths.install_dir().mkdir(parents=True, exist_ok=True)

    acquire_main_exe(version, progress=progress)

    if progress:
        progress("Копирую файл удаления…", 55)
    copy_uninstaller()

    if progress:
        progress("Создаю ярлык в меню Пуск…", 70)
    create_start_menu_shortcut()

    if create_desktop:
        if progress:
            progress("Создаю ярлык на Рабочем столе…", 85)
        create_desktop_shortcut()

    if progress:
        progress("Регистрирую удаление в Windows…", 95)
    register_uninstaller(version=version)

    if progress:
        progress("Готово", 100)
