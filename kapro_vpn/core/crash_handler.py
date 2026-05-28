"""Top-level startup-crash handling: log, friendly dialog, recovery.

Replaces the raw "Failed to execute script 'run'" / Python traceback
popup that PyInstaller shows when main() throws during startup. Any
unhandled exception from app startup lands in handle_startup_crash():
we write a timestamped crash log, then show a branded dialog that lets
the user open the log folder or reset settings — instead of dumping a
stack trace they can't act on. Crucially, a startup crash means the
in-app auto-updater never runs, so without this the user is stuck on a
broken build with no path forward except a manual reinstall.

Kept dependency-light and Qt-lazy (PySide6 is imported inside the dialog
function) so that a failure in Qt *itself* degrades to a native message
box instead of recursing into a second crash. Text is bilingual RU/EN
on purpose: i18n may not have been initialised yet when we crash.
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

from . import paths


def write_crash_log(exc: BaseException) -> Path | None:
    """Persist the traceback + environment to a timestamped file.

    Best-effort: this runs from inside the crash handler, so it must
    never raise. Returns the log path, or None if even logging failed.
    """
    try:
        try:
            from .. import __version__ as ver
        except Exception:
            ver = "?"
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = paths.logs_dir() / f"crash-{ts}.log"
        body = (
            f"KaproVPN {ver} — startup crash\n"
            f"time     : {datetime.now().isoformat()}\n"
            f"platform : {sys.platform}\n"
            f"python   : {sys.version}\n"
            f"frozen   : {getattr(sys, 'frozen', False)}\n"
            f"argv     : {sys.argv}\n"
            + "-" * 60 + "\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        )
        path.write_text(body, encoding="utf-8")
        return path
    except Exception:
        return None


def handle_startup_crash(exc: BaseException) -> int:
    """Log the crash, show a friendly dialog, optionally recover.

    Always returns a non-zero exit code so the process exits with an
    error status, but never re-raises.
    """
    log_path = write_crash_log(exc)
    action = "close"
    try:
        action = _show_dialog(exc, log_path)
    except Exception:
        pass  # dialog itself failed — we already wrote the log
    try:
        if action == "reset":
            did = _quarantine_settings()
            _native_message(
                "KaproVPN",
                ("Настройки сброшены. Открой KaproVPN заново.\n"
                 "Settings reset — please open KaproVPN again.")
                if did else
                ("Сбрасывать нечего. Открой KaproVPN заново.\n"
                 "Nothing to reset — please open KaproVPN again."),
            )
        elif action == "logs" and log_path is not None:
            _open_path(log_path.parent)
    except Exception:
        pass
    return 1


# --------------------------------------------------------------------------
# Dialog — Qt if usable, native message box as last resort.
# --------------------------------------------------------------------------

def _build_message_box(summary: str, details: str, log_path: Path | None):
    """Construct the crash QMessageBox. Separated from exec() so it can be
    built and verified headless without blocking on a modal loop. Returns
    ``(box, {action: button})`` where action is "reset"/"logs"/"close".
    """
    from PySide6.QtWidgets import QApplication, QMessageBox

    # Reuse the app if startup got far enough to create one; otherwise
    # spin up a throwaway instance just for the dialog.
    if QApplication.instance() is None:
        QApplication(sys.argv)

    log_line = f"\n\nЛог / log:\n{log_path}" if log_path else ""
    box = QMessageBox()
    box.setIcon(QMessageBox.Critical)
    box.setWindowTitle("KaproVPN — ошибка запуска / startup error")
    box.setText("KaproVPN не смог запуститься.\nKaproVPN couldn't start.")
    box.setInformativeText(summary + log_line)
    box.setDetailedText(details)
    buttons = {
        "reset": box.addButton("Сбросить настройки / Reset settings",
                               QMessageBox.ResetRole),
        "logs": box.addButton("Открыть логи / Open logs", QMessageBox.ActionRole),
        "close": box.addButton("Закрыть / Close", QMessageBox.RejectRole),
    }
    box.setDefaultButton(buttons["close"])
    return box, buttons


def _show_dialog(exc: BaseException, log_path: Path | None) -> str:
    """Returns one of: "reset" | "logs" | "close"."""
    summary = f"{type(exc).__name__}: {exc}"
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        box, buttons = _build_message_box(summary, details, log_path)
        box.exec()
        clicked = box.clickedButton()
        for action, btn in buttons.items():
            if clicked is btn:
                return action
        return "close"
    except Exception:
        log_line = f"\n\nЛог / log:\n{log_path}" if log_path else ""
        _native_message(
            "KaproVPN — ошибка запуска / startup error",
            "KaproVPN не смог запуститься / couldn't start.\n\n"
            + summary + log_line,
        )
        return "close"


def _native_message(title: str, text: str) -> None:
    """Last-resort message that doesn't depend on Qt at all."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    print(f"[{title}] {text}", file=sys.stderr)


# --------------------------------------------------------------------------
# Recovery helpers.
# --------------------------------------------------------------------------

def _quarantine_settings() -> bool:
    """Move settings.json aside so the next launch starts with defaults.

    configs.json is deliberately left alone — it holds the user's servers
    (precious) and load_configs already degrades gracefully on corruption.
    Returns True if a file was actually moved.
    """
    try:
        f = paths.settings_file()
        if not f.is_file():
            return False
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        f.replace(f.with_name(f"settings.bad-{ts}.json"))
        return True
    except Exception:
        return False


def _open_path(path: Path) -> None:
    """Open a file or folder in the OS file manager. Best-effort."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass
