"""Runtime crash guard — keep the app alive + log exceptions that escape a Qt
slot / timer / QThread callback during the event loop.

Why this exists: PySide6 (6.5+) ABORTS the whole process on the first unhandled
Python exception raised inside a slot — a 1-second poll tick, the DNS watchdog,
the subscription auto-refresh, etc. The user just sees the window vanish after a
few minutes, with NO traceback anywhere (the startup crash_handler only covers
exceptions before the event loop). One latent bug in any periodic callback =
hard crash.

install() replaces sys.excepthook (and threading.excepthook) with a handler that:
  * writes a one-line, redacted entry to app.log + a full traceback file
    (deduped by signature, so a per-second failure can't flood the disk);
  * returns WITHOUT re-raising, so the Qt event loop keeps running — one bad
    callback degrades to "that tick failed + got logged" instead of killing
    the whole app.

KeyboardInterrupt / SystemExit are passed through to the original hook so Ctrl+C
and explicit sys.exit() still work. Startup exceptions are unaffected: those
unwind the normal call stack into main()'s try/except (they never reach
sys.excepthook), so the friendly startup crash dialog still fires.
"""
from __future__ import annotations

import sys
import threading
import traceback
from datetime import datetime

_installed = False
_orig_excepthook = None
_seen_signatures: set[str] = set()
_lock = threading.Lock()


def _signature(exc_type, tb) -> str:
    """Stable id for a crash site: exc type + innermost frame file:line. Lets us
    log a repeating failure's full traceback once, not every tick."""
    try:
        last = traceback.extract_tb(tb)[-1]
        return f"{exc_type.__name__}@{last.filename}:{last.lineno}"
    except Exception:
        return getattr(exc_type, "__name__", "?")


def _write_traceback_file(exc_type, exc, tb) -> None:
    try:
        from . import paths
        try:
            from .. import __version__ as ver
        except Exception:
            ver = "?"
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = paths.logs_dir() / f"runtime-crash-{ts}.log"
        body = (
            f"KaproTUN {ver} — runtime exception (app kept running)\n"
            f"time     : {datetime.now().isoformat()}\n"
            f"platform : {sys.platform}\n"
            + "-" * 60 + "\n"
            + "".join(traceback.format_exception(exc_type, exc, exc.__traceback__))
        )
        path.write_text(body, encoding="utf-8")
    except Exception:
        pass


def _report(exc_type, exc, tb) -> None:
    try:
        from . import app_log
        app_log.log(f"[runtime-exception] {exc_type.__name__}: {exc}")
    except Exception:
        pass
    sig = _signature(exc_type, tb)
    with _lock:
        first = sig not in _seen_signatures
        _seen_signatures.add(sig)
    if first:
        _write_traceback_file(exc_type, exc, tb)


def _excepthook(exc_type, exc, tb) -> None:
    # Let interrupt / explicit exit propagate untouched.
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        if _orig_excepthook is not None:
            _orig_excepthook(exc_type, exc, tb)
        return
    _report(exc_type, exc, tb)
    # Deliberately return without re-raising: this is what stops PySide6 from
    # aborting the process on a slot exception.


def _threading_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    _report(args.exc_type, args.exc_value, args.exc_traceback)


def install() -> None:
    """Install the guard. Idempotent. Call once, right before app.exec()."""
    global _installed, _orig_excepthook
    if _installed:
        return
    _orig_excepthook = sys.excepthook
    sys.excepthook = _excepthook
    try:
        threading.excepthook = _threading_excepthook  # py3.8+
    except Exception:
        pass
    try:
        import faulthandler
        from . import paths
        # Native (C++/segfault) crashes can't be caught in Python, but
        # faulthandler dumps the offending stack to a file we can ask for.
        _fh = open(paths.logs_dir() / "faulthandler.log", "w", encoding="utf-8")
        faulthandler.enable(_fh)
    except Exception:
        pass
    _installed = True
