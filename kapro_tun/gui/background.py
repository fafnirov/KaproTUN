"""Blocking work off the UI thread, for things that cannot be interrupted.

A subscription download, a leak test, a network diagnostics pass: each sits
in socket calls for seconds and has no way to be told "stop". As QThreads
they were a trap — a QThread still running when its owner is destroyed aborts
the whole process ("QThread: Destroyed while thread is still running"), so
closing a dialog or quitting meant waiting the work out, or killing a thread
that may hold Python's locks.

Here the work runs on a daemon Python thread, which simply ends with the
process, and the object that carries its signals belongs to nobody: it keeps
itself alive until it has reported, then deletes itself. A dialog that closes
early just stops listening — Qt drops the connection when the dialog goes.

The object lives on the UI thread; only its signals cross over, so slots
connected to it run on the UI thread as usual.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

_live: set = set()      # workers still running, so nothing collects them mid-flight


class Background(QObject):
    """Subclass, declare the result signals, implement _work().

    _work() does the blocking part on the worker thread and returns a function
    that emits the outcome. It should catch what can go wrong and report it
    through its own signals; if it raises anyway, `crashed` carries the error
    so that whoever is waiting is not left waiting.
    """

    _finished = Signal()
    crashed = Signal(str)      # "TypeName: message" — a bug in the job itself

    def __init__(self, parent: Optional[QObject] = None):
        # No parent on purpose (see the module note); the argument is accepted
        # so call sites read like the QThreads they replaced.
        super().__init__(None)
        self._thread: Optional[threading.Thread] = None
        self._finished.connect(self._retire)

    def start(self) -> None:
        _live.add(self)
        self._thread = threading.Thread(target=self._run, name=type(self).__name__, daemon=True)
        self._thread.start()

    def isRunning(self) -> bool:  # noqa: N802 — the name QThread uses
        return self._thread is not None and self._thread.is_alive()

    def _work(self) -> Callable[[], None]:
        raise NotImplementedError

    def wait(self, msecs: int = 0) -> bool:
        """Like QThread.wait(): True once the job has ended. Never needed for
        safety — only to give a cancelled job a moment to unwind."""
        if self._thread is None:
            return True
        self._thread.join(msecs / 1000 if msecs else None)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            emit = self._work()
        except Exception as e:      # the job's own bug: end it, and say so
            text = f"{type(e).__name__}: {e}"
            try:
                from ..core import app_log
                app_log.log(f"[background] {type(self).__name__} failed: {type(e).__name__}")
            except Exception:
                pass
            emit = lambda: self.crashed.emit(text)   # noqa: E731
        try:
            emit()
            self._finished.emit()
        except RuntimeError:
            # Qt is already gone (the app is quitting): nobody to tell.
            _live.discard(self)

    def _retire(self) -> None:
        _live.discard(self)
        self.deleteLater()
