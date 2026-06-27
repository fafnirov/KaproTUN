"""Background subscription auto-refresher.

Periodically re-fetches the user's saved subscription URL and silently
merges new servers into the configs list. Lets users forget that
KaproTUN exists between paying their provider — when they next launch
the app (or just leave it running), the list is current.

Safety: this is ADDITIVE-ONLY. We never delete configs. Why:

  - Subscription fetches can transiently fail (DPI hit, provider
    maintenance, expired API key). An "authoritative replace" would
    nuke the user's known-good servers on a temporary glitch.
  - Users sometimes add a non-subscription server manually alongside
    their provider's list. We don't tag origin, so we can't tell
    "this came from the sub" from "user pasted this themselves".

Worst case: the user accumulates dead servers. They can clean those
up by hand in the configs picker — much better than losing their
working server to a flaky network minute.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ..core import storage
from ..core.parser import ProxyConfig
from ..core.subscription import (
    SubscriptionResult,
    import_with_dpi_fallback,
)


# Re-check the subscription every 12 hours. Same cadence as most
# providers' typical rotation window (some rotate weekly, some daily —
# 12h splits the diff and stays well under typical "session" length
# so changes are picked up the same day a provider makes them).
REFRESH_INTERVAL_MS = 12 * 60 * 60 * 1000

# Don't fire IMMEDIATELY on launch — splash screen / first-time
# downloads (xray binary) need network bandwidth more. Delay 90 sec
# so the app feels snappy at startup.
INITIAL_DELAY_MS = 90 * 1000


class _RefreshWorker(QThread):
    """Run import_with_dpi_fallback off the UI thread, emit results."""
    succeeded = Signal(object)  # SubscriptionResult
    failed = Signal(str)

    def __init__(self, url: str,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self._url = url

    def run(self) -> None:
        try:
            # DPI-blocked direct fetch falls back through the sing-box
            # health-proxy (tunnels via the active VPN). v3.1.2.
            result = import_with_dpi_fallback(self._url)
            self.succeeded.emit(result)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class SubscriptionAutoRefresh(QObject):
    """Owns the QTimer + active worker. One per app instance.

    Signals:
      configs_added(int)  — N new configs merged. Emit count, not the
                            list, so the consumer (MainWindow) can show
                            a count-toast and call its own
                            storage.load_configs() to get the fresh state.
      log_message(str)    — Goes to the Logs page so the user can see
                            what auto-refresh actually did/didn't do.
    """
    configs_added = Signal(int)
    log_message = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._worker: Optional[_RefreshWorker] = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # Single-shot kicker for the initial 90s delay; after that the
        # main timer repeats every REFRESH_INTERVAL_MS.
        self._kicker = QTimer(self)
        self._kicker.setSingleShot(True)
        self._kicker.timeout.connect(self._first_tick)

    def start(self) -> None:
        """Begin scheduling. Safe to call multiple times — idempotent."""
        if self._timer.isActive() or self._kicker.isActive():
            return
        self._kicker.start(INITIAL_DELAY_MS)

    def stop(self) -> None:
        """Cancel any pending refresh + active worker."""
        self._timer.stop()
        self._kicker.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
        self._worker = None

    def refresh_now(self) -> None:
        """Manual trigger — e.g. from a "Refresh now" button in Settings.

        Just calls _tick() directly; the result still routes through the
        same configs_added / log_message signals as a scheduled refresh.
        """
        self._tick()

    # --- internal --------------------------------------------------------

    def _first_tick(self) -> None:
        """End of initial 90 s delay — fire first refresh + start the
        recurring 12 h interval.
        """
        self._timer.start(REFRESH_INTERVAL_MS)
        self._tick()

    def _tick(self) -> None:
        settings = storage.load_settings()
        if not settings.get("subscription_auto_refresh", True):
            return  # user disabled it
        url = (settings.get("subscription_url") or "").strip()
        if not url:
            return  # no subscription configured — nothing to refresh
        # Avoid stacking workers — if the previous run is still going
        # (slow network), skip this tick instead of doubling up.
        if self._worker is not None and self._worker.isRunning():
            return
        self._worker = _RefreshWorker(url, parent=self)
        self._worker.succeeded.connect(self._on_fetched)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_fetched(self, result: SubscriptionResult) -> None:
        # Refresh the cached remaining-traffic / expiry even when no new
        # servers arrived — the balance still moves between refreshes.
        if result.userinfo is not None:
            settings = storage.load_settings()
            settings["subscription_userinfo"] = result.userinfo.to_dict()
            storage.save_settings(settings)
        if not result.configs:
            return  # empty body — treat as transient, don't touch state
        existing = storage.load_configs()
        existing_names = {c.name for c in existing}
        new_configs = [
            cfg for cfg in result.configs if cfg.name not in existing_names
        ]
        if not new_configs:
            # Routine no-op — nothing new from provider since last check.
            # Don't spam the log with these; only mention when there's
            # actual news.
            return
        # Append + persist. ConfigsPicker / MainWindow will reload from
        # storage on their next refresh cycle (1-second QTimer).
        existing.extend(new_configs)
        storage.save_configs(existing)
        self.log_message.emit(
            f"[*] Auto-refresh подписки: добавлено {len(new_configs)} "
            f"новых серверов"
        )
        self.configs_added.emit(len(new_configs))

    def _on_failed(self, msg: str) -> None:
        # DPI block / network down / provider site flaky — these are
        # the EXPECTED failure mode. We try again in 12h, don't bother
        # the user. Only log it so it's traceable if they ever wonder.
        self.log_message.emit(f"[!] Auto-refresh пропущен: {msg[:120]}")
