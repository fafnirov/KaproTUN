"""Subscription downloads off the UI thread (see background.py for why these
are not QThreads)."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from ..core.subscription import classify_fetch_error, import_with_dpi_fallback
from .background import Background


class SubscriptionFetch(Background):
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


class SubscriptionsRefresh(Background):
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
