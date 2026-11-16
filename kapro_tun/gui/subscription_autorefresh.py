"""Refresh the last saved subscription in the background.

Merge by source so providers can rotate their own servers without replacing
another provider's entries. Missing servers are retained; conflicts wait for
a manual refresh. Metadata is recorded per provider for My subscriptions.
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
    # Emitted after any successful fetch (even with no new servers) so the UI
    # can re-render the remaining-traffic / expiry banner + "обновлено N назад".
    userinfo_updated = Signal()

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

    # Don't re-fetch on every (re)connect — a flapping tunnel could otherwise
    # hammer the provider. Only refresh on connect if the last successful
    # refresh is older than this. The point of on-connect refresh is "pick up
    # rotated servers + fresh balance now that we have a working tunnel", which
    # a 10-minute floor satisfies without spamming.
    ON_CONNECT_MIN_INTERVAL_SEC = 10 * 60

    def refresh_on_connect(self) -> None:
        """Fire a refresh right after a successful connect — now there's a live
        tunnel the DPI-fallback can ride, so a previously-blocked provider
        becomes reachable. Skipped if we refreshed within the last
        ON_CONNECT_MIN_INTERVAL_SEC."""
        last = int(storage.load_settings().get("subscription_last_refresh", 0) or 0)
        if last and (int(time.time()) - last) < self.ON_CONNECT_MIN_INTERVAL_SEC:
            return
        self._tick()

    # --- internal --------------------------------------------------------

    def _first_tick(self) -> None:
        """End of initial 90 s delay — fire first refresh + start the
        recurring 12 h interval.
        """
        # Privacy mode (v3.6.x): a background fetch every 12 h is a clean heartbeat
        # telling the provider this device is alive, and how many devices a
        # subscription has. When the user opts into minimal metadata we simply do
        # not schedule it — the subscription is refreshed only when they ask.
        try:
            from ..core import storage as _st
            if bool(_st.load_settings().get("minimal_metadata", False)):
                return
        except Exception:
            pass
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
        self._fetched_url = url
        self._worker = _RefreshWorker(url, parent=self)
        self._worker.succeeded.connect(self._on_fetched)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_fetched(self, result: SubscriptionResult) -> None:
        from ..core.desktop_experience import remember_subscription
        from ..core.subscription import merge_configs
        existing = storage.load_configs()
        merged = merge_configs(existing, result.configs)
        # Cross-provider conflicts wait for an explicit manual refresh.
        changed = merged.configs != existing
        if changed and not storage.save_configs(merged.configs):
            return
        settings = storage.load_settings()
        settings["subscription_last_refresh"] = int(time.time())
        settings["subscription_userinfo"] = result.userinfo.to_dict() if result.userinfo else {}
        storage.save_settings(settings)
        url = getattr(self, "_fetched_url", "") or settings.get("subscription_url", "")
        if url:
            remember_subscription(url, result, existing, merged.configs)
        self.userinfo_updated.emit()
        if changed:
            self.configs_added.emit(merged.added)

    def _on_failed(self, msg: str) -> None:
        # DPI block / network down / provider site flaky — these are
        # the EXPECTED failure mode. We try again in 12h, don't bother
        # the user. Only log it so it's traceable if they ever wonder.
        self.log_message.emit(f"[!] Auto-refresh пропущен: {msg[:120]}")
