"""Subscription downloads off the UI thread.

These are daemon Python threads, not QThreads, on purpose. A download cannot
be interrupted — it sits in a socket call for up to half a minute, twice when
it retries through the tunnel. A QThread still running when its owner is
destroyed aborts the whole process ("QThread: Destroyed while thread is still
running"), so quitting mid-download would mean either waiting it out or
killing the thread, and killing a thread that holds Python's locks is worse
than both. A daemon thread simply ends with the process.

The worker object lives on the UI thread and only its signals cross over, so
slots connected to it run on the UI thread as usual.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from ..core.subscription import classify_fetch_error, import_with_dpi_fallback


class _Background(QObject):
    """Runs `work()` once on a daemon thread; `_deliver(result)` then emits
    whatever the subclass promises. Deletes itself after delivering."""

    _finished = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._thread: Optional[threading.Thread] = None
        self._finished.connect(self.deleteLater)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=type(self).__name__, daemon=True)
        self._thread.start()

    def isRunning(self) -> bool:  # noqa: N802 — same name as QThread's
        return self._thread is not None and self._thread.is_alive()

    def _work(self) -> Callable[[], None]:
        """Do the blocking part; return a function that emits the outcome."""
        raise NotImplementedError

    def _run(self) -> None:
        emit = self._work()
        try:
            emit()
            self._finished.emit()
        except RuntimeError:
            # The window is gone (the app is quitting): nobody to tell.
            pass


class SubscriptionFetch(_Background):
    """Download one subscription. Direct first, then — if that looks blocked
    and the VPN is up — through the tunnel."""

    succeeded = Signal(object)   # SubscriptionResult
    failed = Signal(object)      # FetchError, already classified

    def __init__(self, url: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.url = url

    def _work(self) -> Callable[[], None]:
        try:
            result = import_with_dpi_fallback(self.url)
        except Exception as e:
            info = classify_fetch_error(e)
            return lambda: self.failed.emit(info)
        return lambda: self.succeeded.emit(result)


class SubscriptionsRefresh(_Background):
    """Download every saved subscription. One dead provider does not stop
    the others: failures are collected, not raised."""

    done = Signal(object)   # dict: configs / userinfo / ok / errors / total

    def __init__(self, urls: list[str], parent: Optional[QObject] = None):
        super().__init__(parent)
        self._urls = list(urls)

    def _work(self) -> Callable[[], None]:
        configs: list = []
        userinfo = None
        ok = 0
        errors: list[tuple] = []   # (url, FetchError)
        for url in self._urls:
            try:
                res = import_with_dpi_fallback(url)
            except Exception as e:
                errors.append((url, classify_fetch_error(e)))
                continue
            configs.extend(res.configs)
            # Keep the most recent traffic / expiry summary that says anything.
            if res.userinfo is not None and res.userinfo.summary():
                userinfo = res.userinfo
            ok += 1
        payload = {"configs": configs, "userinfo": userinfo, "ok": ok,
                   "errors": errors, "total": len(self._urls)}
        return lambda: self.done.emit(payload)
