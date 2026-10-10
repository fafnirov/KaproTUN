"""Main application window — compact, mobile-app-style single-screen layout."""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core.i18n import tr
from ..core import (
    app_log, net_conflicts, sing_box_config, storage, updater, xray_stats,
)
from ..core import controller as _controller
from ..core.controller import MODE_TUN
from .merge_prompt import merge_with_prompt, one_line
from ..core.controller import ConnectionManager as _CM
_HEALTH_OK, _HEALTH_DEGRADED, _HEALTH_DEAD = (
    _CM.HEALTH_OK, _CM.HEALTH_DEGRADED, _CM.HEALTH_DEAD)
from ..core.controller import ConnectionError as VPNConnectionError
from ..core.controller import ConnectionManager
from ..core.parser import ProxyConfig
from . import icons, kit
from .add_server_v2 import MODE_LINK, MODE_SUB, AddServerPage
from .servers_page import ServersPage
from .subscription_autorefresh import SubscriptionAutoRefresh
from .stats_page import StatsPage
from . import window_resize
from .installer_dialog import (ensure_geoip_ru_cached, ensure_sing_box_installed)
from .sites_dialog import SitesDialog
from .home_v2 import HomePage
from .settings_v2 import LogsPage, SettingsPage
from .titlebar import TitleBar
from .toast import show_toast
from .tray import TrayManager
from .widgets import NavBar
from . import connection_state


# ----- Pages ---------------------------------------------------------------

class _UpdateCheckWorker(QThread):
    """Background poll of GitHub Releases. Emits if a newer version is out."""
    update_available = Signal(object)  # updater.UpdateInfo
    no_update = Signal()

    def run(self) -> None:
        info = updater.latest_release()
        if info is not None and updater.is_newer(info.version):
            self.update_available.emit(info)
        else:
            self.no_update.emit()


class _ConnectWorker(QThread):
    """Runs ConnectionManager.connect() off the GUI thread.

    The connect path spends ~1-5 seconds in synchronous I/O (start xray
    subprocess, start tun2socks, wait for the TUN device to appear, add
    several thousand bypass routes). If we ran that on the main thread,
    the Qt event loop would freeze and the "ПОДКЛЮЧЕНИЕ…" pulse on the
    connect button would render zero frames — the user only sees the
    final "ПОДКЛЮЧЕНО" snap. Pushing it onto a QThread keeps the loop
    free to animate.
    """

    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, manager: ConnectionManager, config: ProxyConfig,
                 sites: list[str], parent=None):
        super().__init__(parent)
        self._manager = manager
        self._config = config
        self._sites = sites

    def run(self) -> None:
        try:
            self._manager.connect(self._config, self._sites)
            self.finished_ok.emit()
        except VPNConnectionError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(tr("mw.unexpected_error", err=f"{type(e).__name__}: {e}"))


class _IpProbeWorker(QThread):
    """Async fetch of public IP/country after successful connect.

    Triggered ~2 sec after _on_connect_success (gives xray time to fully
    bring its inbounds up — too early and the probe goes through the
    tunnel before it's actually serving traffic). Off the GUI thread so
    a 5-second timeout doesn't freeze the UI.

    On success emits resolved(ip, country_name, city). On any failure —
    silent on the UI (emits empty strings, the label hides) but verbose
    on the diag signal so the Logs page shows what went wrong. v1.10.0
    was completely silent on failure which made the "I see no IP" user
    report impossible to debug remotely; v1.10.1 wires the diag signal
    to logs_page.append.
    """

    # v1.14.0: 4th field — country_code (ISO 3166-1 alpha-2) — needed by
    # the new WorldMapWidget to place the pin. The localized
    # country_name is for human display; the code is for the lookup
    # table COUNTRY_COORDS in world_map.py.
    resolved = Signal(str, str, str, str)  # ip, country_name, city, country_code
    diag = Signal(str)                       # one line per significant step

    def __init__(self, socks_proxy: Optional[str], locale: str, parent=None):
        super().__init__(parent)
        self._socks_proxy = socks_proxy
        self._locale = locale

    def run(self) -> None:
        from ..core import ip_probe
        try:
            # v3.0.10 / v3.3.3: be cold-start tolerant. The probe fires right after
            # connect, while the tunnel's proxy connection pool is still warming up —
            # the first few HTTPS requests can time out even though the tunnel is
            # perfectly healthy (general traffic already flows). This is worse on
            # Trojan than on VLESS-REALITY: Trojan runs the probe's HTTPS INSIDE its
            # own outer TLS (TLS-in-TLS), so the cold handshake is heavier and a lone
            # 2 s single-shot (the old timeout=2.0, retries=0 — which flatly
            # contradicted this docstring) routinely missed the window and left the UI
            # on "Ваш IP: —" for a working Trojan VPN. Restore the ~12 s warm-up
            # window the docstring always promised: 4 attempts × 3 s with 1.5 s pauses.
            # Returns on the FIRST success, so a warm tunnel still resolves in well
            # under a second and pays none of this.
            info = ip_probe.fetch_public_ip(
                socks_proxy=self._socks_proxy,
                locale=self._locale,
                debug=None,
                timeout=3.0,
                retries=3,
                retry_delay=1.5,
            )
        except Exception as e:
            # Defence in depth — ip_probe already catches everything,
            # but if it ever surfaces something we still want a log
            # line and an empty UI rather than an unhandled thread
            # exception that kills Qt's event loop.
            self.diag.emit(f"[ip-probe] worker exception: {type(e).__name__}: {e}")
            info = None
        if info is None:
            self.resolved.emit("", "", "", "")
            return
        self.resolved.emit(
            info.ip, info.country_name, info.city or "", info.country_code,
        )


class _DnsWatchdog(QThread):
    """Runtime DNS and data-plane health monitor for TUN mode.

    The failure this guards against: the tunnel stays "up" (xray + tun2socks
    alive) but its DNS path dies mid-session — server-side resolver outage,
    :53 blocked upstream, half-open hysteria. Because TUN mode clears the
    physical NIC's DNS, that means the machine silently stops resolving
    *anything* while still showing "connected", and nothing in the existing
    process-death watcher notices (the processes are fine).

    The injected health probe also verifies real traffic through sing-box, so a
    dead outbound cannot hide behind the independent direct DoH resolver. On
    three consecutive failures (about one minute), the window starts a bounded
    self-heal. A single transient network error never triggers a reconnect.

    It self-gates on ConnectionManager.tun_dns_guarded(): when we're not in a
    DNS-clearing TUN session it doesn't probe at all, so it's cheap to leave
    running for the whole app lifetime and never touches HTTP-mode / leak-
    protection-off sessions where a lookup failure isn't ours to fix.
    """

    unhealthy = Signal()

    def __init__(self, is_guarded, probe_health=None, parent=None):
        super().__init__(parent)
        self._is_guarded = is_guarded   # callable -> bool (manager.tun_dns_guarded)
        self._probe_health = probe_health
        self._stop = False
        self._fail_streak = 0           # consecutive DEAD verdicts
        self._degraded_streak = 0       # consecutive DEGRADED verdicts
        # v3.5.1: 20 s -> 30 s. The probe competes for CPU with the userspace
        # network stack; polling it less often removes a chunk of the load that
        # made the probe fail in the first place.
        self._interval_s = 30
        # Only a tunnel proven DEAD is healed, and only after a sustained
        # streak (3 x 30 s = 90 s).
        self._fail_threshold = 3
        # DEGRADED (probes failing but the tunnel is still moving packets) is
        # NOT healed — that was the self-inflicted mid-game outage. It is only
        # escalated if it never clears: 20 x 30 s = 10 min of continuous
        # degradation means something really is wrong, so heal once.
        self._degraded_max = 20

    def run(self) -> None:
        from ..core import dns_health
        while not self._stop:
            # Sleep the interval in 1-second slices so stop() returns promptly
            # instead of blocking up to a full interval on quit.
            for _ in range(self._interval_s):
                if self._stop:
                    return
                self.msleep(1000)
            if self._stop:
                return
            if not self._guarded():
                self._fail_streak = 0
                continue
            verdict = self._verdict(dns_health)
            if self._stop:
                return
            # A disconnect may have landed while we were probing — never heal
            # a session that's already gone.
            if not self._guarded():
                self._fail_streak = self._degraded_streak = 0
                continue
            if verdict == _HEALTH_OK:
                self._fail_streak = self._degraded_streak = 0
                continue
            if verdict == _HEALTH_DEGRADED:
                # Alive but slow / probe starved (typical while a game saturates
                # the stack). Do NOT reconnect: that outage is worse than the
                # symptom. Only escalate if it never clears.
                self._fail_streak = 0
                self._degraded_streak += 1
                app_log.net("watchdog", verdict="degraded",
                            streak=self._degraded_streak, max=self._degraded_max)
                if self._degraded_streak >= self._degraded_max:
                    self._degraded_streak = 0
                    app_log.net("watchdog", action="heal", cause="degraded_timeout")
                    self.unhealthy.emit()
                continue
            # DEAD
            self._degraded_streak = 0
            self._fail_streak += 1
            app_log.net("watchdog", verdict="dead",
                        streak=self._fail_streak, threshold=self._fail_threshold)
            if self._fail_streak >= self._fail_threshold:
                self._fail_streak = 0
                app_log.net("watchdog", action="heal", cause="tunnel_dead")
                self.unhealthy.emit()

    def _guarded(self) -> bool:
        try:
            return bool(self._is_guarded())
        except Exception:
            return False

    def _verdict(self, dns_health) -> str:
        """Normalised health verdict for one tick.

        Accepts either the modern three-state probe (controller
        .tun_runtime_health) or a legacy bool callable — True -> OK,
        False -> DEAD — so older callers/tests keep their semantics."""
        try:
            if self._probe_health is not None:
                raw = self._probe_health()
            else:
                raw = bool(dns_health.probe(timeout=5.0, attempts=2))
        except Exception:
            # Our own probe blowing up is not evidence the tunnel died.
            return _HEALTH_DEGRADED
        if isinstance(raw, str):
            return raw if raw in (_HEALTH_OK, _HEALTH_DEGRADED, _HEALTH_DEAD) else _HEALTH_OK
        return _HEALTH_OK if raw else _HEALTH_DEAD

    def stop(self) -> None:
        self._stop = True
        self.wait(4000)


class _NetworkChangeWatchdog(QThread):
    """Detects a change of the physical egress interface (Ethernet ↔ Wi-Fi)
    while connected (v3.4.0).

    sing-box's auto_route is pinned to the interface that was default at
    connect; on Windows it does NOT re-home on a full adapter swap, so after
    unplugging Ethernet (or roaming to Wi-Fi) the tunnel silently leaks —
    traffic egresses the new interface DIRECT — or dies, and a manual reconnect
    on the stale routing table often lands in "no network". Catching the roam
    and driving a clean reconnect on the new interface fixes both.

    The check is cheap (a UDP-connect source-IP probe, no packets, no
    subprocess), so we poll faster than the DNS watchdog — a roam should heal in
    seconds. A 2-tick streak rides out the brief no-route moment mid-switch
    (egress_changed() returns False on an unresolvable fingerprint, so a
    transition never false-triggers)."""

    changed = Signal()

    def __init__(self, is_connected, egress_changed, parent=None):
        super().__init__(parent)
        self._is_connected = is_connected      # callable -> bool
        self._egress_changed = egress_changed  # callable -> bool
        self._stop = False
        self._interval_s = 5
        self._streak = 0
        self._threshold = 2                    # ~10 s sustained → real roam

    def run(self) -> None:
        while not self._stop:
            for _ in range(self._interval_s):
                if self._stop:
                    return
                self.msleep(1000)
            if self._stop:
                return
            try:
                roamed = bool(self._is_connected()) and bool(self._egress_changed())
            except Exception:
                roamed = False
            if not roamed:
                self._streak = 0
                continue
            self._streak += 1
            if self._streak >= self._threshold:
                self._streak = 0
                self.changed.emit()

    def stop(self) -> None:
        self._stop = True
        self.wait(3000)


# ----- Main window ---------------------------------------------------------

class MainWindow(QMainWindow):
    log_received = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("KaproTUN")
        self.setWindowIcon(icons.app_icon())
        # v1.16.1: resizable. Default 480×870 (the long-standing fixed
        # size that everything's been laid out for), restored from
        # settings if the user resized it before. Minimum 480×780 —
        # bandwidth chart is 420 wide so we need 420 + 24+24 margins =
        # 468 minimum, rounded up to 480 with a tiny safety buffer.
        # Height 780 fits the Stats page's live+24h blocks tightly;
        # smaller would clip the chart. No max — let the user go as
        # big as their monitor.
        #
        # 480px gives Russian labels enough breathing room — at 420 the
        # radio-button text and a few hints were getting clipped.
        # v1.14.5: 820 → 870 default H to restore the connect button
        # to a larger 220×220 with proper spacing.
        # Window sizing + resize policy.
        #
        # DEFAULT (allow_window_resize=False): a FIXED-size window. No mouse
        # resize — opens at the canonical 480×870 every launch. This kills the
        # "window resizes/creeps erratically" UX bug: the frameless edge-handles
        # let users drag-resize (often by accident on the 6px invisible border),
        # and resizeEvent persisted every intermediate size, so the window
        # drifted a few px each session. Fixed size + no handles = predictable.
        #
        # ADVANCED (allow_window_resize=True, opt-in via settings): the old
        # resizable behaviour — restore the persisted size, install the 8 edge
        # handles (below), persist on resize.
        # storage.load_settings is the canonical reader; manager caches the
        # same dict on .settings later. Read it now, before the manager exists.
        saved_settings = storage.load_settings()
        # Size preset: 'standard' (full) or 'compact' (shorter + a touch
        # narrower, for low-resolution / low-DPI screens). 'auto' (default)
        # picks compact when the screen can't comfortably fit standard.
        # v2 (4.1): the window is 460 x 720 as designed; "compact" is the same
        # layout with a smaller ring for screens that cannot fit 720.
        _PRESETS = {"standard": (460, 720), "compact": (460, 640)}
        preset = str(saved_settings.get("window_size_preset", "auto")).strip().lower()
        if preset not in _PRESETS:
            preset = "compact" if self._screen_too_short_for(720) else "standard"
        self._compact_preset = (preset == "compact")
        DEFAULT_W, DEFAULT_H = _PRESETS[preset]
        MIN_W, MIN_H = DEFAULT_W, DEFAULT_H - 90  # floor for advanced resizable mode
        self._allow_resize = self._window_resize_allowed(saved_settings)
        if self._allow_resize:
            self.setMinimumSize(MIN_W, MIN_H)
            saved_size = saved_settings.get("window_size", [DEFAULT_W, DEFAULT_H])
            try:
                w, h = int(saved_size[0]), int(saved_size[1])
            except (TypeError, ValueError, IndexError):
                w, h = DEFAULT_W, DEFAULT_H
            # Clamp restored size to the minimum so a corrupted settings
            # file can't shrink us below usable.
            self.resize(max(w, MIN_W), max(h, MIN_H))
        else:
            # min == max == fixed: Qt blocks any user OR programmatic resize,
            # and we never create the edge handles. Ignore any persisted
            # (possibly drifted) window_size so startup is always identical.
            self.setFixedSize(DEFAULT_W, DEFAULT_H)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowMinimizeButtonHint)

        self.manager = ConnectionManager(on_log=self.log_received.emit)
        self.configs: list[ProxyConfig] = storage.load_configs()
        self._active_config: Optional[ProxyConfig] = self._restore_last_config()
        self._connected_at: float = 0.0
        self._really_quitting = False
        self._connecting = False  # True while _ConnectWorker is running
        # Tray-menu quick-connect uses this cache to surface the 3
        # fastest configs. Populated by a background pinger that runs
        # once at startup + after any config-list mutation.
        # name → latency in ms, or None (unreachable), or -1 (UDP-only).
        self._tray_pings: dict[str, Optional[int]] = {}
        self._tray_pinger: Optional[object] = None  # PingerThread instance
        self._connect_worker: Optional[_ConnectWorker] = None
        self._prev_traffic: Optional[xray_stats.TrafficStats] = None
        # v1.15.0: rolling per-minute aggregation for the 24h stats db.
        # _poll_traffic fires every 1s, but we only flush a row to
        # bandwidth_history.record() once per 60-second window.
        self._minute_up_bytes = 0
        self._minute_down_bytes = 0
        self._minute_window_start = 0
        self._update_worker: Optional[_UpdateCheckWorker] = None
        self._crash_notified = False  # avoid spamming the same kill-switch toast
        self._crash_diag_logged = False  # one full [process_crash] diag per episode
        # v3.0.8: a single unreadable poll() must NOT tear down a fine session.
        # Require the core process to read not-running on TWO consecutive 1s
        # polls before arming reconnect — kills the spurious "Подключение…"
        # flicker on a tunnel the user considers fine.
        self._crash_confirm = 0
        # v2.0.1: one-shot flags so a SILENT helper-process death (tun2socks
        # engine / hysteria transport dies while xray itself stays alive ->
        # "connected but no internet") is logged once, not every poll tick.
        # Cleared when the session goes idle so the next connect reports fresh.
        self._tun_death_notified = False
        self._hy_death_notified = False
        # Auto-reconnect state: when xray dies without us asking, we try
        # to bring it back up to MAX times with exponential backoff. Reset
        # on successful connect, on user-initiated disconnect, or after
        # all attempts fail.
        self._reconnect_attempts = 0
        self._reconnect_max = 3
        # Backoff schedule: 1s after first crash, 5s after second, 15s
        # after third. Matches the rough "transient ISP blip / config-
        # being-rotated / brief endpoint downtime" timescales.
        self._reconnect_backoff = (1, 5, 15)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._do_auto_reconnect)

        # v2.1.8 stability guard. Auto-recovery (crash/DNS/memory reconnects) is
        # disabled after a hard stop until the user manually connects again, so
        # nothing can loop forever. The history is a reconnect-storm detector:
        # more than _RECONNECT_STORM_MAX reconnects inside the window — from ANY
        # combination of triggers — forces a clean emergency stop instead of
        # thrashing connect/disconnect.
        self._auto_recovery_disabled = False
        self._reconnect_history: list[float] = []
        self._RECONNECT_STORM_WINDOW_S = 120.0
        self._RECONNECT_STORM_MAX = 5

        # v2.2.0 socket-exhaustion guard. tun2socks->xray loopback port
        # exhaustion ("Only one usage of each socket address") is a distinct
        # root cause from memory: reconnecting doesn't fix it, so we allow ONE
        # reconnect then stop, and the memory watchdog defers to this when an
        # exhaustion event is recent. Lines flood, so handling is throttled.
        self._last_sock_exhaust_ts = 0.0
        self._last_sock_exhaust_handled_ts = 0.0
        self._sock_exhaust_bursts = 0
        self._SOCK_EXHAUST_HANDLE_COOLDOWN_S = 8.0
        self._SOCK_EXHAUST_MAX_RECONNECT = 1
        self._SOCK_EXHAUST_RECENT_S = 60.0

        # Runtime DNS watchdog (v2.1.4): catches the "tunnel up but DNS dead"
        # state the process-death watcher can't see. Self-gates on TUN-with-
        # leak-protection, so it's idle (no probing) in HTTP mode or when the
        # user has leak protection off. A sustained outage drives the same
        # bounded reconnect machinery as a crash. Started once, lives for the
        # app's lifetime; stopped on quit.
        self._dns_watchdog = _DnsWatchdog(
            self.manager.tun_dns_guarded,
            self.manager.tun_runtime_health,
            parent=self,
        )
        self._dns_watchdog.unhealthy.connect(self._on_dns_unhealthy)
        self._dns_watchdog.start()

        # Network-change watchdog (v3.4.0): catch an Ethernet↔Wi-Fi roam and
        # clean-reconnect on the new interface, so the tunnel doesn't silently
        # leak (traffic egressing the new NIC direct) after the switch.
        self._net_watchdog = _NetworkChangeWatchdog(
            self.manager.is_connected,
            self.manager.egress_changed,
            parent=self,
        )
        self._net_watchdog.changed.connect(self._on_network_changed)
        self._net_watchdog.start()

        # Runtime memory watchdog (v2.1.6): sample tun2socks/xray private memory
        # + handles every 10 s (cheap psutil reads, non-blocking → fine on the
        # UI thread), log a summary periodically, and on a genuine runaway do a
        # bounded clean reconnect (which frees the helper processes' memory)
        # with a cooldown + attempt cap so it can never loop.
        self._mem_log_tick = 0
        self._mem_heal_count = 0
        self._mem_heal_max = 4          # heal1, heal2→economy, heal3-4 economy, then stop
        self._mem_heal_cooldown_s = 180.0
        # v2.1.9 false-alarm guards: ignore breaches for a grace period after a
        # (re)connect (helper sits at its baseline until it settles), and only
        # act once the breach PERSISTS across several samples — so a stable high
        # idle baseline can never trigger a heal / reconnect loop.
        self._mem_breach_streak = 0
        self._MEM_GRACE_S = 120.0
        self._MEM_SUSTAIN_MODERATE = 3   # 3×10s ≈ 30s sustained
        self._MEM_SUSTAIN_CRITICAL = 2   # 2×10s ≈ 20s sustained
        self._last_mem_heal_ts = 0.0
        self._mem_heal_exhausted_notified = False
        self._mem_timer = QTimer(self)
        self._mem_timer.timeout.connect(self._check_memory)
        self._mem_timer.start(10000)

        # --- App-shell layout (everything inside the rounded dark frame) ---
        shell = QWidget()
        shell.setObjectName("appShell")
        self.setCentralWidget(shell)
        root = QVBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.titlebar = TitleBar()
        root.addWidget(self.titlebar)

        self.stack = QStackedWidget()
        self.home_page = HomePage(compact=self._compact_preset)
        # Custom-painted widgets that still take a theme getter (stats charts)
        # read the live setting through this closure.
        _theme = lambda: str(self.manager.settings.get("theme", "auto"))
        self.settings_page = SettingsPage(self.manager)
        self.logs_page = LogsPage()
        self.add_page = AddServerPage()
        self.servers_page = ServersPage()
        # v1.15.0: 24-hour bandwidth chart page. Same theme-getter so
        # the chart's colors track live theme switches.
        self.stats_page = StatsPage()
        self.stats_page.set_theme_getter(_theme)
        self.stack.addWidget(self.home_page)        # index 0
        self.stack.addWidget(self.settings_page)    # index 1
        self.stack.addWidget(self.logs_page)        # index 2
        self.stack.addWidget(self.add_page)         # index 3
        self.stack.addWidget(self.servers_page)     # index 4
        self.stack.addWidget(self.stats_page)       # index 5
        root.addWidget(self.stack, stretch=1)

        self.nav = NavBar()
        root.addWidget(self.nav)

        # v1.16.3: 8 transparent resize-handles around the window edges.
        # Same approach as Telegram Desktop / AmneziaVPN / every other
        # frameless Qt app — Qt strips the non-client area on
        # FramelessWindowHint so WM_NCHITTEST never fires (the v1.16.1
        # attempt). These child widgets sit on top of the layout
        # (raised in reposition()) and capture mouse events directly,
        # which works identically on Windows, macOS, and Linux.
        # Edge resize-handles exist ONLY in the opt-in resizable mode. In the
        # default fixed-size mode we never create them — so there are no
        # invisible 6px grab-zones at the borders (no accidental resize, no
        # edge-cursor artifacts on either side of the content).
        if self._allow_resize:
            self._resize_handles = window_resize.ResizeHandles(self)
            self._resize_handles.install()
        else:
            self._resize_handles = None

        # System tray (gracefully no-op if user's DE doesn't expose one)
        self.tray = TrayManager(self)
        self.tray.show()

        self._wire_signals()
        self._refresh_home()
        # A clean install opens on the same home screen as everyone else's:
        # with no server yet it shows what to do first (home_v2.EmptyCard).
        self.nav.set_active("home")
        if not self.configs:
            # Otherwise the first button in the tab order opens with a focus
            # ring on it, as if something had been pressed already.
            self.stack.setFocus()

        # Kick off the initial tray-pings background scan so the
        # quick-connect block appears within a few seconds of startup.
        # Defer by 500 ms so the splash → window swap finishes first.
        QTimer.singleShot(500, self._refresh_tray_pings)

        # Subscription auto-refresh — silent re-fetch every 12 h, adds
        # new servers from the provider's rotating list. Disabled per
        # config in Settings if user wants. Logs to LogsPage, no toast
        # unless the diff is non-empty (handled inside _on_sub_added).
        self._sub_autorefresh = SubscriptionAutoRefresh(self)
        self._sub_autorefresh.configs_added.connect(self._on_sub_autorefresh_added)
        self._sub_autorefresh.log_message.connect(self.logs_page.append)
        self._sub_autorefresh.userinfo_updated.connect(self._on_sub_userinfo_updated)
        self._sub_autorefresh.start()

        # Periodic status refresh — detects subprocess crashes and updates timer
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh_home)
        self._poll.start(1000)

        # Silent update check 2 s after launch — non-blocking, just a toast if newer
        QTimer.singleShot(2000, self._start_update_check)

    # --- wiring -----------------------------------------------------------

    def _wire_signals(self) -> None:
        self.home_page.connect_clicked.connect(self._on_connect_click)
        self.home_page.card_clicked.connect(self._on_open_servers)
        self.settings_page.sites_clicked.connect(self._on_edit_sites)
        self.settings_page.logs_clicked.connect(lambda: self._goto("logs"))
        self.settings_page.diagnostics_clicked.connect(self._on_open_diagnostics)
        self.settings_page.bypass_apps_clicked.connect(self._on_edit_bypass_apps)
        self.settings_page.subscription_clicked.connect(self._on_import_subscription)
        self.home_page.banner_clicked.connect(self._on_import_subscription)
        self.home_page.sites_clicked.connect(self._on_edit_sites)
        self.home_page.settings_clicked.connect(lambda: self._goto("settings"))
        self.home_page.logs_clicked.connect(lambda: self._goto("logs"))
        self.home_page.add_clicked.connect(self._on_open_add_page)
        # v1.14.0: nudge custom-painted widgets to repaint after any
        # setting flip (esp. theme — stylesheet change doesn't trigger
        # paintEvent on QPainter-drawn widgets, only on QSS-styled ones).
        self.settings_page.settings_changed.connect(self._on_settings_changed)
        self.settings_page.check_updates_requested.connect(
            lambda: self._start_update_check(interactive=True)
        )
        self.logs_page.back_clicked.connect(lambda: self._goto("settings"))
        self.add_page.back_clicked.connect(lambda: self._goto("servers"))
        self.add_page.config_ready.connect(self._on_add_page_saved)
        self.add_page.subscription_imported.connect(self._on_subscription_imported)
        self.servers_page.connect_requested.connect(self._on_server_chosen)
        self.servers_page.delete_requested.connect(self._on_delete_server)
        self.servers_page.add_clicked.connect(self._on_open_add_page)
        self.servers_page.subscription_clicked.connect(self._on_import_subscription)
        self.servers_page.ping_requested.connect(self._refresh_tray_pings)
        self.servers_page.refresh_requested.connect(self._on_refresh_subscriptions)
        self.nav.home_clicked.connect(lambda: self._goto("home"))
        self.nav.servers_clicked.connect(lambda: self._goto("servers"))
        self.nav.stats_clicked.connect(lambda: self._goto("stats"))
        self.nav.settings_clicked.connect(lambda: self._goto("settings"))
        self.log_received.connect(self.logs_page.append)
        # v2.2.0: also scan helper logs for socket-exhaustion so we treat it as
        # its own root cause (not a memory leak to reconnect-loop on).
        self.log_received.connect(self._scan_log_line)
        # Title-bar window controls (frameless mode)
        self.titlebar.minimize_clicked.connect(self.showMinimized)
        self.titlebar.close_clicked.connect(self._on_close_to_tray)
        # System tray
        self.tray.toggle_clicked.connect(self._on_connect_click)
        self.tray.show_window_clicked.connect(self._on_show_window)
        self.tray.quit_clicked.connect(self._on_quit_for_real)
        self.tray.config_selected.connect(self._on_tray_config_picked)

    def _goto(self, name: str) -> None:
        target_index, nav_key = {
            "home":     (0, "home"),
            "settings": (1, "settings"),
            "logs":     (2, None),     # no nav highlight for logs
            "add":      (3, "servers"),  # adding a server is part of Servers
            "servers":  (4, "servers"),
            "stats":    (5, "stats"),  # v1.15.0
        }.get(name, (0, "home"))
        if target_index == self.stack.currentIndex():
            return
        self._fade_to(target_index)
        if nav_key is not None:
            self.nav.set_active(nav_key)

    def _fade_to(self, target_index: int) -> None:
        """Crossfade-style page transition: fade in the incoming widget."""
        target = self.stack.widget(target_index)
        # Clear any previous opacity effect on the same widget
        if isinstance(target.graphicsEffect(), QGraphicsOpacityEffect):
            target.setGraphicsEffect(None)
        effect = QGraphicsOpacityEffect(target)
        effect.setOpacity(0.0)
        target.setGraphicsEffect(effect)

        self.stack.setCurrentIndex(target_index)

        anim = QPropertyAnimation(effect, b"opacity", target)
        anim.setDuration(180)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        # Drop the effect once the fade completes so it doesn't sit on the
        # widget forever (effects break some QPainter operations).
        def cleanup():
            if target.graphicsEffect() is effect:
                target.setGraphicsEffect(None)
        anim.finished.connect(cleanup)
        anim.start()
        # Keep a reference so the GC doesn't collect mid-animation.
        self._last_page_anim = anim

    # --- state helpers ----------------------------------------------------

    def _restore_last_config(self) -> Optional[ProxyConfig]:
        last = self.manager.settings.get("last_config_name", "")
        if not last:
            return self.configs[0] if self.configs else None
        for c in self.configs:
            if c.name == last:
                return c
        return self.configs[0] if self.configs else None

    def _refresh_tray_pings(self) -> None:
        """Re-ping every saved config in the background, then refresh the
        tray quick-connect block with the new ordering.

        Called once at startup and after every config-list mutation (add,
        remove, subscription import). One-shot per call — no looping
        timer; ping results don't go stale fast enough to warrant
        background polling.
        """
        if not self.configs:
            self._tray_pings = {}
            self.servers_page.set_pings({})
            self.servers_page.set_pinging(False)
            return
        from .pinger import PingerThread

        # Stop any previous pinger before starting a new one. quit() — what
        # this used to call — only ends a thread's event loop, and PingerThread
        # has none, so the old pinger ran to completion and its finished signal
        # then overwrote the newer results with a stale set. Unhook its signals
        # first so nothing it still emits can land, then ask it to stop; it
        # checks isInterruptionRequested() between pings. parent=self keeps the
        # object alive until the thread actually exits.
        old = self._tray_pinger
        if old is not None:
            for sig in (old.pinged, old.finished):
                try:
                    sig.disconnect()
                except (TypeError, RuntimeError):
                    pass
            try:
                old.requestInterruption()
            except RuntimeError:
                pass

        new_pings: dict[str, Optional[int]] = {}

        def on_pinged(name: str, ms) -> None:
            new_pings[name] = ms

        def on_finished() -> None:
            self._tray_pings = new_pings
            # Push the new pings into the tray menu — this rebuilds the
            # quick-connect top-3.
            active_name = self._active_config.name if self._active_config else ""
            self.tray.set_configs(self.configs, active_name, self._tray_pings)
            # The Servers tab gets the full set (and re-sorts by speed).
            self.servers_page.set_pings(self._tray_pings)
            self.servers_page.set_pinging(False)

        # The Servers tab shows the same measurement as it comes in.
        self.servers_page.set_pings({}, pending=True)
        self.servers_page.set_pinging(True)
        self._tray_pinger = PingerThread(list(self.configs), parent=self)
        self._tray_pinger.pinged.connect(on_pinged)
        self._tray_pinger.pinged.connect(self._on_tray_pinged)
        self._tray_pinger.finished.connect(on_finished)
        self._tray_pinger.start()

    def _on_tray_pinged(self, name: str, ms) -> None:
        # Only from the measurement in progress: a result of the one before,
        # already on its way when that was stopped, must not paint a row.
        if self.sender() is self._tray_pinger:
            self.servers_page.set_ping(name, ms)

    def _diagnose_component_death(self) -> None:
        """No-op since v3.1.0: sing-box is the single dataplane process, so there
        are no separate helper processes (tun2socks / hysteria) whose silent death
        needs detecting — a sing-box exit is handled by the crash watchdog in
        _refresh_home via _primary_process()."""
        return

    def _engine_tag(self) -> str:
        """Short status-line label for the active dataplane."""
        if self.manager.current_mode() != MODE_TUN:
            return tr("mw.engine_proxy")
        return "TUN · sing-box"

    def _engine_is_sing_box(self) -> bool:
        """Always True since v3.1.0 — sing-box is the only engine."""
        return True

    def _primary_process(self):
        """The single dataplane process whose death means the tunnel crashed."""
        return self.manager.sing_box_process

    def _primary_process_name(self) -> str:
        """Human label for the core process, for crash logs."""
        return "sing-box"

    def _crash_diagnostics(self, proc, core_name: str) -> str:
        """Build a redacted, single-line process_crash diagnostic: old pid,
        returncode, uptime, and the last ~10 raw log lines of the dead process —
        so app.log shows WHY it died, not just 'process_crash'. Best-effort:
        every field is optional and never raises (it runs on the crash path)."""
        from ..core import app_log as _al
        parts = [f"engine={core_name}"]
        try:
            pid = (proc.last_pid() if hasattr(proc, "last_pid")
                   else getattr(getattr(proc, "_proc", None), "pid", None))
            if pid:
                parts.append(f"pid={pid}")
        except Exception:
            pass
        try:
            parts.append(f"returncode={proc.returncode()}")
        except Exception:
            pass
        try:
            if hasattr(proc, "uptime"):
                parts.append(f"uptime={int(proc.uptime())}s")
        except Exception:
            pass
        try:
            logs = proc.recent_logs()[-10:] if hasattr(proc, "recent_logs") else []
            if logs:
                tail = " | ".join(_al.redact(str(l)) for l in logs)
                parts.append(f"last_logs=[{tail}]")
        except Exception:
            pass
        return "; ".join(parts)

    def _refresh_home(self) -> None:
        # Don't fight the connect worker for the button state — the
        # "connecting" pulse keeps animating until the worker finishes
        # and we get the success/failure signal.
        if self._connecting:
            self.tray.set_state("connecting", self._active_config.name if self._active_config else "")
            self._sync_servers_page()
            return

        # Detect external crash of the ACTIVE engine's core process. The core
        # is engine-specific: sing-box TUN runs a single sing-box process (xray
        # is never started), classic TUN / HTTP runs xray. Checking the wrong
        # process would mis-read a healthy sing-box session as an Xray crash and
        # reconnect-loop, then tempt the user into legacy (v3.0.2).
        primary = self._primary_process()
        core_name = self._primary_process_name()
        if self.manager._active is not None and not primary.is_running():
            # Debounce: require not-running on TWO consecutive 1s polls before
            # tearing down. A single transient unreadable poll() must not trigger
            # a spurious disconnect+reconnect of a healthy session (the v3.0.2
            # "healthy sing-box mis-read as a crash" class). A process that truly
            # exited stays exited and is caught on the very next tick (≤1s later).
            self._crash_confirm += 1
            if self._crash_confirm < 2:
                return
            rc = primary.returncode()

            # Log a FULL diagnostic (old pid, returncode, uptime, last 10 raw
            # log lines, redacted) exactly once per crash episode, so app.log
            # explains WHY the engine died — not just "reason=process_crash".
            # _crash_diag_logged resets in the healthy branch below.
            if not getattr(self, "_crash_diag_logged", False):
                self._crash_diag_logged = True
                try:
                    app_log.log("[process_crash] "
                                + self._crash_diagnostics(primary, core_name))
                except Exception:
                    pass

            # sing-box is a single process: when it dies the TUN is gone too,
            # so the only way back is a reconnect. Most crashes are transient,
            # so try a few times before giving up. Every teardown on this path
            # HOLDS the kill-switch (v4.0.0): the firewall rules stay through
            # the reconnect, and stay if it gives up, until the user reconnects
            # or releases them. Before, disconnect() removed them right here —
            # at the one moment a kill-switch is for.
            if (self._reconnect_attempts < self._reconnect_max
                    and not self._reconnect_timer.isActive()
                    and not self._auto_recovery_disabled):
                delay = self._reconnect_backoff[self._reconnect_attempts]
                self._reconnect_attempts += 1
                # Gate + log (reason=process_crash); aborts on no-config /
                # storm without starting another reconnect.
                if self._arm_reconnect("process_crash",
                                       self._reconnect_attempts, self._reconnect_max):
                    self.logs_page.append(
                        f"[!] {core_name} упал (код {rc}). "
                        f"Авто-переподключение #{self._reconnect_attempts}/"
                        f"{self._reconnect_max} через {delay} с…"
                    )
                    show_toast(
                        self,
                        tr("mw.toast_reconnecting", n=self._reconnect_attempts),
                        kind="info", duration_ms=delay * 1000,
                    )
                    # Tear down xray/proxy state but DON'T clear self._active
                    # — the timer reuses the SAME config for the reconnect.
                    saved = self._active_config
                    self.manager.disconnect(hold_killswitch=True)
                    self._active_config = saved
                    self._connected_at = 0.0
                    self._reconnect_timer.start(delay * 1000)
            elif self._reconnect_attempts >= self._reconnect_max:
                if not self._crash_notified:
                    self.logs_page.append(
                        f"[!] Авто-переподключение не удалось после "
                        f"{self._reconnect_max} попыток. Ткни «ВКЛЮЧИТЬ» "
                        f"вручную когда захочешь снова."
                    )
                    show_toast(
                        self,
                        tr("mw.toast_reconnect_failed", n=self._reconnect_max),
                        kind="error", duration_ms=10000,
                    )
                    self._crash_notified = True
                self.manager.disconnect(hold_killswitch=True)
                self._connected_at = 0.0
                self._note_killswitch_hold()
        else:
            # Reset crash-notified flags when state is healthy
            self._crash_notified = False
            self._crash_diag_logged = False
            # Reset the crash-debounce counter so it measures CONSECUTIVE
            # not-running ticks (a healthy tick clears a lone transient blip).
            self._crash_confirm = 0
            # Successful steady-state — reset the auto-reconnect counter
            # so the NEXT crash gets a fresh 3-attempt budget instead of
            # immediately giving up.
            if self.manager.is_connected() and self._reconnect_attempts > 0:
                self._reconnect_attempts = 0
            # xray is up — but the tunnel can still be dead if a HELPER process
            # silently exited. Diagnose that once. When fully idle (no active
            # config) clear the one-shot flags so the next session reports anew.
            if self.manager._active is not None:
                self._diagnose_component_death()
            else:
                self._tun_death_notified = False
                self._hy_death_notified = False

        active_name = self._active_config.name if self._active_config else ""
        if self.manager.is_connected():
            elapsed = int(time.time() - self._connected_at) if self._connected_at else 0
            mm, ss = divmod(elapsed, 60)
            hh, mm = divmod(mm, 60)
            timer = f"{hh:d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
            # The timer alone in full-TUN mode; proxy mode is worth naming,
            # since not every app is covered there.
            detail = timer if self.manager.current_mode() == MODE_TUN \
                else f"{timer} · {self._engine_tag()}"
            self.home_page.set_state("connected", detail)
            self.tray.set_state("connected", active_name)
            # v1.15.3: flip the Stats live-block badge based on the
            # connection-manager truth BEFORE attempting to poll xray
            # stats. _poll_traffic may fail / skip the first sample —
            # but the badge should already say "● Подключено" because
            # we ARE connected. (v1.15.2 bug: badge stayed "Не
            # подключено" until on_live_sample fired, which never
            # happened if xray-api stats was slow.)
            self.stats_page.set_live_connected(True)
            self._poll_traffic()
        else:
            # Not connected — but distinguish WHY so the UI is never ambiguous
            # (instead of always "Не подключено"). Mirrors the crash / reconnect
            # / kill-switch branches above (which already logged + toasted the
            # reason); here we set the matching home state, single-source.
            if self._reconnect_timer.isActive():
                self.home_page.set_state(
                    connection_state.RECONNECTING,
                    tr("home.attempt", n=self._reconnect_attempts,
                       total=self._reconnect_max))
                self.tray.set_state("connecting", active_name)
            elif self._killswitch_holding():
                self.home_page.set_state(connection_state.KILLSWITCH_ACTIVE)
                self.tray.set_state("idle", active_name)
            elif (self._active_config is not None
                    and self._reconnect_attempts >= self._reconnect_max):
                self.home_page.set_state(connection_state.ERROR, tr("mw.state_vpn_failed"))
                self.tray.set_state("idle", active_name)
            else:
                self.home_page.set_state(connection_state.DISCONNECTED)
                self.tray.set_state("idle", active_name)
            self._prev_traffic = None  # reset session counter when disconnected
            # Reset live-block on Stats: sparkline empties + badge greys.
            self.stats_page.set_live_connected(False)

        self.home_page.set_config(self._active_config)
        self.home_page.set_ping(self._tray_pings.get(active_name),
                                known=active_name in self._tray_pings)
        self.tray.set_configs(self.configs, active_name, self._tray_pings)
        self._sync_servers_page()

    def _sync_servers_page(self) -> None:
        """Hand the Servers tab the current list, the active server and
        whether it is in use. Cheap when nothing changed — it runs on every
        refresh tick, so no path that changes the list has to remember it."""
        self.servers_page.set_configs(
            self.configs,
            self._active_config.name if self._active_config else "",
            self.manager.is_connected() or self._connecting)

    def _poll_traffic(self) -> None:
        """Pull the latest cumulative byte counters and feed rates to HomePage.

        Reads kernel byte counters from the sing-box TUN interface via psutil
        (rock-solid, zero subprocess overhead) — the counters exist as soon as
        sing-box brings the "KaproTun" interface up."""
        sample = self.manager.traffic_sample()
        if sample is None:
            return
        if self._prev_traffic is None:
            # First sample of the session — record but don't display until we
            # have a delta to compute rate from.
            self._prev_traffic = sample
            self._minute_window_start = int(time.time())
            return
        # Per-second deltas for the home-page rates display.
        up_delta = max(0, sample.uplink_bytes - self._prev_traffic.uplink_bytes)
        down_delta = max(0, sample.downlink_bytes - self._prev_traffic.downlink_bytes)
        up_rate, down_rate = sample.delta_rate(self._prev_traffic)
        self._prev_traffic = sample
        self.home_page.set_traffic(
            up_rate, down_rate,
            sample.uplink_bytes, sample.downlink_bytes,
        )
        # v1.15.2: same per-second sample feeds the Stats page live block.
        # Cheap when Stats isn't visible — the widget just updates a few
        # labels and appends to a deque(maxlen=60); no repaint happens
        # until the widget is shown again (Qt skips paintEvent for hidden
        # widgets).
        self.stats_page.on_live_sample(
            up_rate, down_rate,
            sample.uplink_bytes, sample.downlink_bytes,
        )
        # v1.15.0: roll the per-second deltas into a 60-second bucket,
        # flush to the bandwidth-history db at minute boundaries. We
        # write a row only when the window closes — keeps the db slim
        # (one row per minute instead of one per second).
        self._minute_up_bytes += up_delta
        self._minute_down_bytes += down_delta
        now = int(time.time())
        if now - self._minute_window_start >= 60:
            from ..core import bandwidth_history
            bandwidth_history.record(
                self._minute_up_bytes,
                self._minute_down_bytes,
                ts=self._minute_window_start,
            )
            self._minute_up_bytes = 0
            self._minute_down_bytes = 0
            self._minute_window_start = now

    # --- actions ----------------------------------------------------------

    def _on_connect_click(self) -> None:
        # Ignore clicks while a connect/disconnect is already in flight —
        # otherwise the user can double-tap and we end up with a race
        # between two workers fighting for the same routes/sockets.
        if self._connecting:
            return
        if self.manager.is_connected():
            self._do_disconnect()
            return
        if self._active_config is None:
            kit.notify(self, "info", tr("mw.no_config_title"), tr("mw.no_config_body"),
                       ok_label=tr("dlg.ok"))
            return
        # User-initiated connect → clear any auto-recovery lockout + storm
        # history and reset the memory-heal budget (a fresh session gets a
        # fresh chance; only the USER re-enables auto-recovery).
        self._auto_recovery_disabled = False
        self._reconnect_history = []
        self._mem_heal_count = 0
        self._mem_heal_exhausted_notified = False
        self._last_mem_heal_ts = 0.0
        self._mem_breach_streak = 0
        self._sock_exhaust_bursts = 0
        self._last_sock_exhaust_ts = 0.0
        app_log.log(f"[reconnect] reason=user_requested, config={self._active_config.name}")
        self._do_connect()

    def _do_connect(self) -> None:
        # The in-flight guard lives HERE, not only in the callers. It used to
        # sit in _on_connect_click alone, so the tray's server-picker
        # (_on_tray_config_picked) walked straight past it and could start a
        # second _ConnectWorker on top of a running one — two threads writing
        # the same routes and fighting over the same sockets. Every entry point
        # funnels through this method, so one check here covers all of them.
        if self._connecting:
            return
        # sing-box TUN: ensure the engine binary (+ WinTUN driver) is present.
        if not ensure_sing_box_installed(self):
            return
        # Soft-required — TUN works without it but RU split-routing is less
        # comprehensive. Don't gate connection on it.
        ensure_geoip_ru_cached(self)

        # Warn about third-party apps that hijack Windows networking (virtual
        # adapters / packet filters) and can silently eat all VPN traffic — the
        # classic "connected but nothing loads" that looks like OUR bug. We
        # can't override another app's network driver from userspace, so surface
        # the culprit instead of letting the user hunt a phantom VPN failure.
        try:
            conflicts = net_conflicts.detect_running_conflicts()
        except Exception:
            conflicts = []
        if conflicts:
            names = ", ".join(conflicts)
            app_log.log(f"[net-conflict] running: {names} (can break VPN routing)")
            self.logs_page.append(
                f"[!] Обнаружено сетевое приложение «{names}» — оно "
                f"перехватывает трафик и может ломать VPN (нет интернета при "
                f"«Подключено»). Закрой его, если сайты не грузятся."
            )
            show_toast(self, tr("mw.net_conflict_warn", app=names),
                       kind="error", duration_ms=14000)

        # Kick off the worker BEFORE flipping UI state — that way the
        # set_state call starts its burst + pulse animation on a Qt event
        # loop that's about to be free, not one we're about to block.
        self._connecting = True
        self.home_page.set_state("connecting")
        self.tray.set_state(
            "connecting",
            self._active_config.name if self._active_config else "",
        )

        sites = storage.load_sites()
        self._connect_worker = _ConnectWorker(
            self.manager, self._active_config, sites, parent=self,
        )
        self._connect_worker.finished_ok.connect(self._on_connect_success)
        self._connect_worker.failed.connect(self._on_connect_failed)
        self._connect_worker.start()

    def _warn_inactive_protections(self) -> None:
        """Say so when a protection the user switched on did not arm.

        The Settings checkbox records what the user asked for, and until
        v3.7.5 it was the only thing the UI ever showed: a kill-switch whose
        firewall rules failed to install still looked ticked, and the truth
        lived in one log line. For a VPN that is the worst kind of wrong —
        believing you are protected when you are not."""
        try:
            if not self.manager.is_connected():
                return
            inactive = self.manager.inactive_protections()
            proxy_off = self.manager.system_proxy_applied is False
        except Exception:
            return
        notices = []
        if proxy_off:
            # Proxy mode came up but macOS refused the system-proxy change:
            # the listener works, yet nothing is using it. Without this the
            # window would say "connected" over a VPN that carries nothing.
            notices.append(tr(
                "mw.proxy_not_applied",
                addr=f"{sing_box_config.PROXY_LISTEN_HOST}:"
                     f"{sing_box_config.PROXY_LISTEN_PORT}"))
        if inactive:
            items = "; ".join(
                f"{tr('prot.' + name)} — {tr('prot.reason.' + state)}"
                for name, state in inactive)
            notices.append(tr("mw.protection_inactive", items=items))
            app_log.log("[protection] inactive after connect: "
                        + ", ".join(f"{n}={s}" for n, s in inactive))
        if not notices:
            return
        for note in notices:
            self.logs_page.append(f"[!] {note}")
        # One toast for everything: show_toast replaces the one on screen, so
        # two separate warnings would leave only the second visible.
        show_toast(self, "\n".join(notices), kind="error", duration_ms=15000)

    def _on_connect_success(self) -> None:
        self._connecting = False
        # Successful connect ⇒ wipe the auto-reconnect counter so the
        # next crash gets its own full 3-attempt budget.
        self._reconnect_attempts = 0
        self.manager.update_settings(last_config_name=self._active_config.name)
        self._connected_at = time.time()
        self.logs_page.append(
            f"[*] Подключено к «{self._active_config.name}» ({self._engine_tag()})"
        )
        # On-disk lifecycle line — no server name/secret.
        app_log.log(f"[connect] mode=TUN engine={self.manager.current_engine()}")
        show_toast(self, tr("mw.toast_connected", name=self._active_config.name), kind="success")
        # After the "connected" toast has had its moment — show_toast replaces
        # whatever is on screen, so firing now would erase it.
        QTimer.singleShot(4000, self._warn_inactive_protections)
        self._refresh_home()
        # v1.14.3: show country + map immediately based on the config
        # name's flag emoji. No waiting for the 2-second probe — user
        # sees something the instant connect lands. If the probe later
        # succeeds with a real IP, set_public_ip overwrites with the
        # more-accurate data; if probe fails (AdGuard blocking, etc.),
        # this flag-based placeholder stays. Either way the map+country
        # block never disappears mid-session.
        self._prefill_country_from_config()
        # v1.10.0: confirm to the user that the tunnel is actually working
        # by fetching the public IP as seen from outside and showing it
        # under the status line. Delayed 2s so xray has time to bring its
        # inbounds fully up; if too early the probe times out and the
        # user sees no IP, which is worse than waiting a beat.
        if self.manager.settings.get("public_ip_probe", True):
            session_token = self._connected_at
            QTimer.singleShot(
                2000,
                lambda token=session_token: self._kick_ip_probe(token),
            )
        # v3.1.9: now there's a live tunnel, pull the latest server list +
        # balance from the subscription. The DPI-fallback can finally ride the
        # tunnel, so a provider that's unreachable directly now refreshes.
        # Delayed 5s so the tunnel + health-proxy are fully up; rate-limited
        # internally (refresh_on_connect skips if refreshed in the last 10 min).
        QTimer.singleShot(5000, self._sub_autorefresh.refresh_on_connect)

    def _prefill_country_from_config(self) -> None:
        """Show country + map immediately on connect (v1.14.3).

        Pulls the country code from the leading flag emoji of the
        active config's name. No-op if the config name doesn't start
        with a flag (e.g. user named it "MyServer"). The probe still
        runs and overwrites with the real IP when it lands.
        """
        if self._active_config is None:
            return
        from .world_map import country_code_from_flag
        from ..core.ip_probe import _RU_COUNTRY_NAMES
        cc = country_code_from_flag(self._active_config.name)
        if not cc:
            return
        country_name = _RU_COUNTRY_NAMES.get(cc, cc)
        # "…" placeholder while probe is in flight — once probe returns
        # we'll either replace with real IP, or replace with "—" if
        # probe failed (in _on_ip_probe_resolved fallback path).
        self.home_page.set_public_ip("…", country_name, "", cc)

    def _kick_ip_probe(self, session_token: Optional[float] = None) -> None:
        """Start the async public-IP fetch. Routes through SOCKS5 in
        HTTP-proxy mode (otherwise the probe would see the local IP);
        in TUN mode the system route table already tunnels everything,
        no proxy override needed.
        """
        # If the user disconnected in the 2s between connect-success
        # and this firing, bail — showing the IP for a now-dead session
        # would be misleading.
        if (not self.manager.is_connected()
                or (session_token is not None
                    and session_token != self._connected_at)):
            return
        session_token = self._connected_at
        # Probe via the sing-box loopback health proxy (forced to outbound=proxy),
        # so the shown IP is the VPN egress, not the local IP.
        socks_proxy = (f"{sing_box_config.HEALTH_PROXY_HOST}:"
                       f"{sing_box_config.HEALTH_PROXY_PORT}")
        from ..core.i18n import current_locale
        self._ip_probe = _IpProbeWorker(socks_proxy, current_locale(), parent=self)
        self._ip_probe.resolved.connect(
            lambda ip, country, city, code, token=session_token:
                self._on_ip_probe_resolved(ip, country, city, code)
                if (token == self._connected_at and self.manager.is_connected())
                else None
        )
        # Pipe probe diagnostics into the Logs page so a silent failure
        # (no IP shown, no obvious reason) becomes a one-glance debug.
        self._ip_probe.diag.connect(
            lambda line, token=session_token:
                self.logs_page.append(line)
                if (token == self._connected_at and self.manager.is_connected())
                else None
        )
        self._ip_probe.start()

    def _on_ip_probe_resolved(
        self, ip: str, country_name: str, city: str, country_code: str,
    ) -> None:
        # Don't paint stale data: if the user disconnected while the
        # probe was in flight, just drop the result on the floor.
        if not self.manager.is_connected():
            return

        # v1.14.3: if probe failed entirely (empty ip — happens when
        # AdGuard / similar blocks every fallback endpoint we have),
        # recover the country from the config name's leading flag
        # emoji (e.g. "🇳🇱 BMV1+ ..." → "NL"). Better to show map +
        # country than a completely empty block. IP gets a "—"
        # placeholder; the country_name is looked up from the same
        # localization table the probe would use.
        if not ip and self._active_config:
            from .world_map import country_code_from_flag
            from ..core.ip_probe import _RU_COUNTRY_NAMES
            fallback_cc = country_code_from_flag(self._active_config.name)
            if fallback_cc:
                fallback_country = _RU_COUNTRY_NAMES.get(
                    fallback_cc, fallback_cc,
                )
                self.home_page.set_public_ip(
                    "—", fallback_country, "", fallback_cc,
                )
                return

        self.home_page.set_public_ip(ip, country_name, city, country_code)

    def _on_connect_failed(self, msg: str) -> None:
        self._connecting = False
        self.home_page.set_state("idle")
        self._refresh_home()
        if self._reconnect_attempts <= 0 and self._killswitch_holding():
            # Not an auto-reconnect attempt (those report on their own when
            # they give up): a manual connect, a server switch or a self-heal
            # restart failed while the kill-switch is up. The error alone would
            # not explain why the internet is gone.
            self._note_killswitch_hold()
            msg = f"{msg}\n\n{tr('mw.toast_killswitch_hold')}"
        # If this was triggered by auto-reconnect (we're mid-attempts),
        # silently let the timer try again instead of popping a modal
        # — the user would be furious to OK 3 dialogs in 30 seconds.
        if self._reconnect_attempts > 0:
            self.logs_page.append(
                f"[!] Попытка #{self._reconnect_attempts} не удалась: {msg}"
            )
            # v3.3.2: re-arm the NEXT backoff step. Previously we just returned,
            # so a hard-down server got only attempt #1 and the 5s/15s backoff
            # steps never ran — the whole multi-attempt self-heal was dead after
            # the first failure (backoff collapsed to a single 1s try, then a
            # silent drop to DISCONNECTED that contradicted the "#1/3" toast).
            # Walk the backoff to _reconnect_max, then give up cleanly (same
            # notify + DNS/routes-restoring teardown as the crash path).
            if (self._reconnect_attempts < self._reconnect_max
                    and not self._reconnect_timer.isActive()
                    and not self._auto_recovery_disabled):
                delay = self._reconnect_backoff[self._reconnect_attempts]
                self._reconnect_attempts += 1
                if self._arm_reconnect("retry", self._reconnect_attempts,
                                       self._reconnect_max):
                    self.logs_page.append(
                        f"[!] Повторная попытка #{self._reconnect_attempts}/"
                        f"{self._reconnect_max} через {delay} с…"
                    )
                    show_toast(
                        self,
                        tr("mw.toast_reconnecting", n=self._reconnect_attempts),
                        kind="info", duration_ms=delay * 1000,
                    )
                    self._reconnect_timer.start(delay * 1000)
            elif self._reconnect_attempts >= self._reconnect_max:
                if not self._crash_notified:
                    self.logs_page.append(
                        f"[!] Авто-переподключение не удалось после "
                        f"{self._reconnect_max} попыток. Ткни «ВКЛЮЧИТЬ» "
                        f"вручную когда захочешь снова."
                    )
                    show_toast(
                        self,
                        tr("mw.toast_reconnect_failed", n=self._reconnect_max),
                        kind="error", duration_ms=10000,
                    )
                    self._crash_notified = True
                self._reconnect_timer.stop()
                self.manager.disconnect(hold_killswitch=True)
                self._connected_at = 0.0
                self._note_killswitch_hold()
                self._refresh_home()
            return
        self._show_connect_error(msg)

    def _show_connect_error(self, msg: str) -> None:
        """A failed connect that the user started: say why, and offer the
        log. The message can quote the engine's log, so it is shown as plain
        text that scrolls instead of growing the dialog off the window."""
        dlg = kit.OverlayDialog(self, wide=True)
        dlg.head("x-circle", tr("mw.connect_failed_title"), "", tone="danger")
        dlg.add_long_text(str(msg), max_height=260)
        dlg.add_actions([("close", tr("leak.close_btn"), "primary")], default="close",
                        left=(("logs", tr("dlg.logs"), "ghost"),), icons={"logs": "file"})
        if dlg.ask() == "logs":
            self._goto("logs")

    def _arm_reconnect(self, reason: str, attempt: int, total: int) -> bool:
        """Gate + log EVERY auto-reconnect initiation. Returns True if the
        caller may proceed, False if it must abort. Centralises:
          * the reason trail in app.log (reason=dns_watchdog/process_crash/
            memory_*/user_requested);
          * the 'no active server → don't pick a random one, stop' rule;
          * the reconnect-storm cap → emergency stop.
        This is what stops the unexplained reconnect/server-switch churn."""
        if self._auto_recovery_disabled:
            app_log.log(f"[reconnect] stopped: reason=disabled (trigger={reason})")
            return False
        if self._active_config is None:
            self.logs_page.append("[!] Авто-переподключение остановлено: нет "
                                  "активного сервера (вручную выбери сервер).")
            app_log.log(f"[reconnect] stopped: reason=no_active_config (trigger={reason})")
            return False
        now = time.time()
        self._reconnect_history = [t for t in self._reconnect_history
                                   if now - t <= self._RECONNECT_STORM_WINDOW_S]
        self._reconnect_history.append(now)
        if len(self._reconnect_history) > self._RECONNECT_STORM_MAX:
            self._emergency_stop(
                "reconnect",
                f"stopped: reason=storm ({len(self._reconnect_history)} reconnects "
                f"in {int(self._RECONNECT_STORM_WINDOW_S)}s; trigger={reason})")
            return False
        # config NAME only — never a URL/UUID (app_log redacts those anyway).
        name = self._active_config.name if self._active_config else "?"
        app_log.log(f"[reconnect] reason={reason}, attempt={attempt}/{total}, config={name}")
        return True

    def _emergency_stop(self, reason_tag: str, detail: str) -> None:
        """Hard, SAFE stop: tear down tun2socks/xray/hysteria + restore
        routes/DNS/proxy/firewall (a normal disconnect), reset connected state,
        and disable further auto-recovery this session so nothing re-arms a
        loop. The user can reconnect manually. Used for critical-runaway-
        exhausted and the reconnect-storm cap."""
        self._reconnect_timer.stop()
        self._auto_recovery_disabled = True
        try:
            # Stops the engine and restores routes/DNS/proxy. An armed
            # kill-switch stays: an emergency stop is not the user asking for
            # unprotected internet.
            self.manager.disconnect(hold_killswitch=True)
        except Exception:
            pass
        self._connected_at = 0.0
        line = f"[{reason_tag}] {detail}"
        self.logs_page.append("[!] " + line)
        app_log.log(line)
        show_toast(self, tr("mw.toast_emergency_stop"), kind="error", duration_ms=12000)
        # After the emergency toast, which would otherwise replace it: of the
        # two, "the internet is blocked on purpose" is the one to leave on screen.
        self._note_killswitch_hold()
        self._refresh_home()

    def _do_auto_reconnect(self) -> None:
        """Fired by self._reconnect_timer after backoff elapses.

        Re-runs _do_connect with the SAME active config (never a different
        server). _do_connect spawns its own worker and routes the result
        through _on_connect_success / _on_connect_failed.
        """
        if self._auto_recovery_disabled:
            app_log.log("[reconnect] stopped: reason=disabled (timer)")
            return
        if self._connecting:
            return
        if self._active_config is None:
            # Never silently fall back to a different/first server — just stop.
            self.logs_page.append("[!] Авто-переподключение остановлено: нет "
                                  "активного сервера.")
            app_log.log("[reconnect] stopped: reason=no_active_config (timer)")
            return
        self.logs_page.append(
            f"[*] Авто-переподключение к «{self._active_config.name}»: "
            f"попытка #{self._reconnect_attempts}…"
        )
        self._do_connect()

    def _on_dns_unhealthy(self) -> None:
        """DNS watchdog reported a sustained resolution outage through the
        tunnel. Drive a *bounded* self-heal that reuses the crash-reconnect
        machinery: a clean disconnect (guaranteed to restore DNS / routes /
        proxy) followed by a backed-off reconnect whose own connect-time DNS
        health-check will roll back again if the tunnel is still broken. After
        _reconnect_max failed heals, stop and leave the machine cleanly
        DISCONNECTED (real DNS restored) with a clear message — never loop.
        """
        # The worker emitted this from another thread a moment ago; the session
        # may have changed underneath us. Re-validate on the GUI thread.
        if not self.manager.tun_dns_guarded():
            return
        # Don't pile onto an in-flight connect or an already-scheduled heal.
        if self._connecting or self._reconnect_timer.isActive():
            return

        if self._reconnect_attempts >= self._reconnect_max:
            # Budget spent — guarantee a clean, DNS-restored teardown and stop.
            if not self._crash_notified:
                self.logs_page.append(
                    f"[!] Watchdog: DNS через туннель не восстановился после "
                    f"{self._reconnect_max} попыток. Отключаюсь начисто — "
                    f"DNS, маршруты и системный прокси возвращены в исходное "
                    f"состояние. Переподключи вручную, когда будешь готов."
                )
                show_toast(
                    self,
                    tr("mw.toast_dns_failed_stop"),
                    kind="error", duration_ms=10000,
                )
                self._crash_notified = True
            self._reconnect_timer.stop()
            self.manager.disconnect(hold_killswitch=True)      # restores DNS/routes/proxy, clears journal
            self._connected_at = 0.0
            self._note_killswitch_hold()
            self._refresh_home()
            return

        delay = self._reconnect_backoff[self._reconnect_attempts]
        self._reconnect_attempts += 1
        if not self._arm_reconnect("dns_watchdog",
                                   self._reconnect_attempts, self._reconnect_max):
            return  # disabled / no-config / storm — _arm_reconnect handled it
        self.logs_page.append(
            f"[!] Watchdog: нет резолва DNS через туннель — самовосстановление "
            f"#{self._reconnect_attempts}/{self._reconnect_max} через {delay} с "
            f"(чистый reconnect; DNS/маршруты/прокси будут восстановлены)…"
        )
        show_toast(
            self,
            tr("mw.toast_dns_recover", n=self._reconnect_attempts),
            kind="info", duration_ms=delay * 1000,
        )
        # Same tear-down-but-keep-config dance as the crash path, so the timer
        # reconnects to the SAME server. disconnect() restores DNS/routes/proxy
        # and clears the recovery journal; the reconnect re-marks it.
        saved = self._active_config
        self.manager.disconnect(hold_killswitch=True)
        self._active_config = saved
        self._connected_at = 0.0
        self._reconnect_timer.start(delay * 1000)

    def _on_network_changed(self) -> None:
        """The physical egress interface changed (Ethernet↔Wi-Fi) while we were
        connected. sing-box's auto_route stayed pinned to the old interface, so
        the tunnel is now leaking (traffic egressing the new NIC direct) or
        dead. Drive a clean reconnect on the NEW interface using the same
        proven tear-down-and-reconnect machinery as the crash/DNS paths.
        """
        # The worker emitted from another thread; the session may have changed.
        if not self.manager.is_connected():
            return
        # Don't pile onto an in-flight connect or an already-scheduled reconnect.
        if self._connecting or self._reconnect_timer.isActive():
            return
        # A roam is a fresh situation, not a failure streak — give it a full
        # attempt budget on the new interface (v3.3.2 backoff re-arm handles any
        # retries; the reconnect-storm cap in _arm_reconnect still applies).
        self._reconnect_attempts = 0
        delay = self._reconnect_backoff[self._reconnect_attempts]
        self._reconnect_attempts += 1
        if not self._arm_reconnect("network_change",
                                   self._reconnect_attempts, self._reconnect_max):
            return  # disabled / no-config / storm — _arm_reconnect handled it
        self.logs_page.append(
            "[!] Сменился сетевой интерфейс (Ethernet↔Wi-Fi) — переустанавливаю "
            "туннель на новом подключении…"
        )
        show_toast(self, tr("mw.toast_network_changed"),
                   kind="info", duration_ms=delay * 1000)
        saved = self._active_config
        self.manager.disconnect(hold_killswitch=True)
        self._active_config = saved
        self._connected_at = 0.0
        self._reconnect_timer.start(delay * 1000)

    def _scan_log_line(self, line: str) -> None:
        """No-op since v3.1.0 — the tun2socks loopback SOCKS bridge (whose
        127.0.0.1 ephemeral-port exhaustion this watched) is gone with the legacy
        engine. sing-box dials the proxy directly, no local bridge to exhaust."""
        return

    def _on_socket_exhaustion(self, dest) -> None:
        """Local tun2socks->xray ephemeral-port exhaustion ('Only one usage of
        each socket address'). Reconnecting does NOT fix the cause (a private/
        LAN flood into the TUN), so: log it, allow at most ONE reconnect, then
        emergency-stop on recurrence — never loop. These lines flood, so the
        handling is throttled to once per cooldown."""
        now = time.time()
        self._last_sock_exhaust_ts = now
        if now - self._last_sock_exhaust_handled_ts < self._SOCK_EXHAUST_HANDLE_COOLDOWN_S:
            return
        self._last_sock_exhaust_handled_ts = now
        msg = "[socket-exhaustion] tun2socks->xray local SOCKS exhausted"
        if dest:
            msg += f" (dest={dest})"
        self.logs_page.append("[!] " + msg)
        app_log.log(msg)
        if not (self.manager.is_connected()
                and self.manager.current_mode() == MODE_TUN):
            return
        if (self._connecting or self._reconnect_timer.isActive()
                or self._auto_recovery_disabled):
            return
        self._sock_exhaust_bursts += 1
        if self._sock_exhaust_bursts > self._SOCK_EXHAUST_MAX_RECONNECT:
            # Recurred after a reconnect — reconnect can't fix a private/LAN
            # flood. Stop cleanly with an honest reason.
            self._emergency_stop(
                "socket_exhaustion",
                "повторное исчерпание локальных сокетов (слишком много локальных "
                "соединений / private-трафик попал в TUN) — останавливаю, не "
                "зацикливаюсь" + (f"; dest={dest}" if dest else ""))
            return
        # First burst: one clean reconnect — re-applies the private-bypass routes
        # and clears the loopback TIME_WAIT backlog.
        if self._arm_reconnect("socket_exhaustion", self._sock_exhaust_bursts,
                               self._SOCK_EXHAUST_MAX_RECONNECT):
            em = (f"[socket-exhaustion] чистый reconnect "
                  f"#{self._sock_exhaust_bursts}/{self._SOCK_EXHAUST_MAX_RECONNECT} "
                  f"(reason=socket_exhaustion)")
            self.logs_page.append("[!] " + em)
            app_log.log(em)
            show_toast(self, tr("mw.toast_socket_exhaust"), kind="info", duration_ms=4000)
            saved = self._active_config
            self.manager.disconnect(hold_killswitch=True)
            self._active_config = saved
            self._connected_at = 0.0
            self._reconnect_timer.start(1000)

    def _check_memory(self) -> None:
        """Periodic (10 s) runtime sample for tun2socks + xray. Logs a summary
        ~once a minute and, on a SUSTAINED runaway, hands off to the tiered
        self-heal.

        Two false-alarm guards (v2.1.9) stop a high IDLE baseline from looping
        reconnects on a perfectly healthy connection:
          * grace period — ignore breaches for _MEM_GRACE_S after a (re)connect,
            while the helper settles at its baseline;
          * sustained breach — only act once the breach persists across several
            consecutive samples, not on a single reading.
        Only while a TUN session is up; cheap, non-blocking → fine on the UI."""
        if not self.manager.is_connected() or self.manager.current_mode() != MODE_TUN:
            self._mem_breach_streak = 0
            return
        try:
            stats = self.manager.sample_runtime_stats()
        except Exception:
            return
        self._mem_log_tick = (self._mem_log_tick + 1) % 6
        if self._mem_log_tick == 0:
            try:
                line = self.manager.format_runtime_stats(stats)
                self.logs_page.append(line)
                app_log.log(line)
            except Exception:
                pass

        # v3.0.0 — softer sing-box watchdog. The self-heal (_on_memory_pressure)
        # restarts tun2socks and forces the 'economy' buffer preset; both are
        # classic-engine concepts. sing-box is a single native-TUN process with
        # no loopback SOCKS bridge, so the UDP-session storm the heal targets
        # can't occur. We keep sampling/logging the [mem] line above for
        # diagnostics, but never run the tun2socks heal against sing-box.
        if self.manager.current_engine() != _controller.ENGINE_CLASSIC:
            self._mem_breach_streak = 0
            return
        try:
            verdict = self.manager.memory_pressure_reason(stats)
        except Exception:
            verdict = None

        # Grace period: don't judge a just-(re)connected helper. _connected_at
        # is 0.0 while connecting and set to now() on success.
        now = time.time()
        if self._connected_at <= 0.0 or (now - self._connected_at) < self._MEM_GRACE_S:
            self._mem_breach_streak = 0
            if verdict:
                try:
                    app_log.log(f"[mem-pressure/{verdict[0]}] {verdict[1]} "
                                f"(grace {int(self._MEM_GRACE_S)}s — без действия)")
                except Exception:
                    pass
            return

        if not verdict:
            self._mem_breach_streak = 0
            return

        # Sustained-breach gate: require N consecutive over-threshold samples.
        severity, reason = verdict
        self._mem_breach_streak += 1
        need = (self._MEM_SUSTAIN_CRITICAL if severity == "critical"
                else self._MEM_SUSTAIN_MODERATE)
        try:
            app_log.log(f"[mem-pressure/{severity}] {reason} "
                        f"streak={self._mem_breach_streak}/{need} | "
                        f"{self.manager.format_runtime_stats(stats)}")
        except Exception:
            pass
        if self._mem_breach_streak >= need:
            self._mem_breach_streak = 0
            self._on_memory_pressure(severity, reason)

    def _on_memory_pressure(self, severity: str, reason: str) -> None:
        """Tiered, BOUNDED self-heal for a memory/handle/thread runaway.
        CRITICAL heals immediately; MODERATE respects a cooldown; after repeated
        runaway we force the 'economy' preset (smallest buffers + shortest UDP
        timeout) for the next connect; after the attempt cap we stop and ask the
        user to reconnect manually — never an endless loop. A clean disconnect
        frees both helper processes' memory and restores DNS/routes/proxy."""
        if self._connecting or self._reconnect_timer.isActive():
            return
        if self._auto_recovery_disabled:
            return  # a prior emergency stop disabled auto-recovery this session
        # v2.2.0: if socket exhaustion is the recent root cause, the memory
        # pressure is just a SYMPTOM (the loopback handles). Don't reconnect-
        # loop on it — stop cleanly. The socket handler usually fires first;
        # this guards the race where memory fires first.
        if (time.time() - self._last_sock_exhaust_ts) < self._SOCK_EXHAUST_RECENT_S:
            if not self._mem_heal_exhausted_notified:
                self._mem_heal_exhausted_notified = True
                self._emergency_stop(
                    "socket_exhaustion",
                    "высокое потребление из-за исчерпания локальных сокетов "
                    "(private/LAN-трафик в TUN) — останавливаю вместо "
                    "reconnect-цикла")
            return
        now = time.time()
        d = _controller.mem_heal_decision(
            severity, now, self._last_mem_heal_ts, self._mem_heal_count,
            max_heals=self._mem_heal_max, cooldown_s=self._mem_heal_cooldown_s)

        if d["exhausted"]:
            # v2.1.8 — exhausted is NOT the end of the story for a CRITICAL
            # runaway: leaving tun2socks/xray at 3-4 GB can wedge/crash the
            # client. mem_exhausted_action() says whether we must force a stop.
            action = _controller.mem_exhausted_action(severity)
            if action["force_shutdown"]:
                if not self._mem_heal_exhausted_notified:
                    self._mem_heal_exhausted_notified = True
                    stats = ""
                    try:
                        stats = self.manager.format_runtime_stats()
                    except Exception:
                        pass
                    # Hard, safe stop: helpers killed, routes/DNS/proxy/firewall
                    # restored, auto-recovery disabled — no "leave it running".
                    self._emergency_stop(
                        "mem-critical",
                        "exhausted: emergency disconnect, helpers stopped — "
                        + reason + (f" | {stats}" if stats else ""))
                return
            # MODERATE + exhausted: survivable — leave as-is, ask the user.
            if not self._mem_heal_exhausted_notified:
                msg = (f"[!] Память: {reason}. Авто-восстановление исчерпано "
                       f"({self._mem_heal_max}) — оставляю как есть, переподключи "
                       f"вручную, когда удобно.")
                self.logs_page.append(msg)
                app_log.log(msg)
                show_toast(self, tr("mw.toast_mem_exhausted"),
                           kind="error", duration_ms=10000)
                self._mem_heal_exhausted_notified = True
            return

        if not d["do_heal"]:
            # Moderate breach still inside the cooldown — record, wait.
            msg = f"[!] Память: {reason} — жду окончания cooldown…"
            self.logs_page.append(msg)
            app_log.log(msg)
            return

        # Escalate to economy after repeated runaway, so the restart actually
        # changes behaviour instead of refilling the same buffers.
        if (d["escalate_economy"]
                and str(self.manager.settings.get("performance_preset")) != "economy"):
            self.manager.update_settings(performance_preset="economy")
            em = ("[!] Память: повторный runaway — переключаю профиль на "
                  "«Экономия памяти» (512к буферы, UDP-timeout 5с) для следующих "
                  "подключений.")
            self.logs_page.append(em)
            app_log.log(em)
            try:                       # keep the Settings combo in sync
                combo = self.settings_page.perf_combo
                keys = [combo.itemData(i) for i in range(combo.count())]
                combo.setCurrentIndex(keys.index("economy"))
            except Exception:
                pass

        self._mem_heal_count += 1
        self._last_mem_heal_ts = now
        tag = "КРИТИЧНО" if severity == "critical" else "повышено"
        when = "немедленно" if severity == "critical" else "после cooldown"
        msg = (f"[!] Память [{tag}]: {reason} — чистый reconnect "
               f"#{self._mem_heal_count}/{self._mem_heal_max} ({when}); "
               f"память helper-процессов освободится.")
        self.logs_page.append(msg)
        app_log.log(msg)
        # Gate + log (reason=memory_critical/memory_moderate) through the storm
        # cap. If it aborts (storm), _emergency_stop already tore everything
        # down — don't start another reconnect.
        if not self._arm_reconnect(f"memory_{severity}",
                                   self._mem_heal_count, self._mem_heal_max):
            return
        show_toast(self, tr("mw.toast_mem_reset", n=self._mem_heal_count),
                   kind="info", duration_ms=4000)
        saved = self._active_config
        self.manager.disconnect(hold_killswitch=True)
        self._active_config = saved
        self._connected_at = 0.0
        self._reconnect_timer.start(1000)

    def _do_disconnect(self, reason: str = "user_requested",
                       hold_killswitch: bool = False) -> None:
        # Disconnect with an HONEST reason. Default 'user_requested' — this is
        # only called from the user's connect/disconnect button and the tray
        # server-switch; auto paths (memory/DNS/socket/crash) tear down via
        # manager.disconnect()/_emergency_stop with their OWN reason, never this.
        self._reconnect_timer.stop()
        self._reconnect_attempts = 0
        # Fresh session next time → fresh memory-heal budget + auto-recovery
        # re-enabled + storm/socket history cleared.
        self._mem_heal_count = 0
        self._mem_heal_exhausted_notified = False
        self._last_mem_heal_ts = 0.0
        self._mem_breach_streak = 0
        self._auto_recovery_disabled = False
        self._reconnect_history = []
        self._sock_exhaust_bursts = 0
        self._last_sock_exhaust_ts = 0.0
        # A user disconnect releases the kill-switch; the one caller that is
        # about to connect again straight away (server switch) keeps it.
        self.manager.disconnect(hold_killswitch=hold_killswitch)
        self._connected_at = 0.0
        self.logs_page.append("[*] Отключено, системный прокси восстановлен")
        app_log.log(f"[disconnect] reason={reason}")
        show_toast(self, tr("mw.toast_disconnected"), kind="info")
        self._refresh_home()

    # --- update checking --------------------------------------------------

    def _start_update_check(self, interactive: bool = False) -> None:
        if self._update_worker is not None and self._update_worker.isRunning():
            return
        if interactive:
            self.settings_page.set_update_status(tr("mw.update_checking"), accent=False)
        self._update_worker = _UpdateCheckWorker(parent=self)
        self._update_worker.update_available.connect(
            lambda info: self._on_update_available(info, interactive)
        )
        self._update_worker.no_update.connect(
            lambda: self._on_no_update(interactive)
        )
        self._update_worker.start()

    def _on_update_available(self, info: "updater.UpdateInfo",
                              interactive: bool) -> None:
        msg = tr("mw.update_available", version=info.version)
        self.settings_page.set_update_status(
            tr("mw.update_available_status", msg=msg), accent=True,
        )
        # Hook the Settings "Update" button to the in-app updater dialog.
        try:
            self.settings_page.check_updates_btn.clicked.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.settings_page.check_updates_btn.setText(tr("mw.update_to_version", version=info.version))
        self.settings_page.check_updates_btn.clicked.connect(
            lambda _checked=False, i=info: self._open_updater(i)
        )
        # Toast nudge — only on background check; if user explicitly
        # asked, the Settings banner is already telling them.
        if not interactive:
            show_toast(
                self,
                tr("mw.update_toast", msg=msg),
                kind="info",
                duration_ms=8000,
            )

    def _on_no_update(self, interactive: bool) -> None:
        if interactive:
            self.settings_page.set_update_status(
                tr("mw.update_latest", version=__version__), accent=False,
            )

    def _open_updater(self, info: "updater.UpdateInfo") -> None:
        """Open the in-app updater. Handles download + silent install."""
        from .updater_dialog import UpdaterDialog
        dlg = UpdaterDialog(info, parent=self)
        dlg.exec()

    def trigger_autoconnect(self) -> None:
        """Called from main.py shortly after launch if autoconnect_on_launch is on."""
        if self.manager.is_connected() or self._connecting:
            return
        if self._active_config is None:
            return
        self._do_connect()

    def _on_open_servers(self) -> None:
        """The server card on the home screen: show the list, on the server
        in use."""
        if self._active_config is not None:
            self.servers_page.select(self._active_config.name)
        self._goto("servers")

    def _on_server_chosen(self, cfg: ProxyConfig) -> None:
        """"Connect" on the Servers tab (and "Save and connect" on the add
        page): make this server the one in use and bring the tunnel up on it.
        Same meaning as picking a server in the tray menu."""
        if self._connecting:
            # A connect is in flight, and it reads the active server when it
            # lands: changing it now would report "connected to B" over a
            # tunnel to A. Nothing changes; the user picks again in a moment.
            show_toast(self, tr("srv.busy"), kind="info")
            return
        self._goto("home")
        if self.manager.is_connected():
            self._on_tray_config_picked(cfg)
            return
        self._active_config = cfg
        self.manager.update_settings(last_config_name=cfg.name)
        self._refresh_home()
        self._on_connect_click()

    def _on_delete_server(self, cfg: ProxyConfig) -> None:
        """The Servers tab asked (and the user confirmed) to delete a server.
        Names are unique in the saved list, so the name identifies it even if
        the list was re-read from disk since the page drew its rows."""
        # The page checked this before asking; the tunnel may have come up
        # on this very server while the question was on screen.
        in_use = (self._active_config is not None and self._active_config.name == cfg.name
                  and (self.manager.is_connected() or self._connecting))
        if in_use:
            show_toast(self, tr("srv.del_connected_tip"), kind="info")
            return
        self.configs[:] = [c for c in self.configs if c.name != cfg.name]
        storage.save_configs(self.configs)
        self._tray_pings.pop(cfg.name, None)
        if self._active_config is not None and self._active_config.name == cfg.name:
            self._active_config = self.configs[0] if self.configs else None
            self.manager.update_settings(
                last_config_name=self._active_config.name if self._active_config else "")
        self._refresh_home()
        show_toast(self, tr("srv.deleted", name=one_line(cfg.name, 40)), kind="info")

    def _rebind_active_config(self) -> None:
        """After the list was replaced: point the active server at its entry
        in the new list. A merge that updates a server (a provider rotated
        its address or key) keeps the name and brings a new object; the old
        one would keep dialling the dead address."""
        if self._active_config is not None:
            name = self._active_config.name
            self._active_config = next((c for c in self.configs if c.name == name),
                                       self._active_config)

    def _on_open_add_page(self) -> None:
        """"Add" on the Servers tab or on the empty home screen."""
        self.add_page.open(MODE_LINK)
        self._goto("add")

    def _on_add_page_saved(self, new_cfg: ProxyConfig) -> None:
        """A single server, parsed and named on the add page."""
        merged, _added, _updated = merge_with_prompt(self, self.configs, [new_cfg])
        self.configs[:] = merged
        storage.save_configs(self.configs)
        self._rebind_active_config()
        # As stored: under "keep both" the new server lives under its own name.
        new_cfg = next((c for c in self.configs if c.outbound == new_cfg.outbound), new_cfg)
        # A new server has no ping yet, and the tray's quick-connect list only
        # shows servers that do — without this it stayed invisible there until
        # the next restart.
        self._refresh_tray_pings()
        show_toast(self, tr("mw.toast_config_added", name=one_line(new_cfg.name, 40)),
                   kind="success")
        # The button says "Save and connect" — so connect. (If a connect to
        # another server is in flight, the new one is saved and waits in the list.)
        self._goto("servers")
        self._on_server_chosen(new_cfg)

    def _on_import_subscription(self) -> None:
        """Every "import a subscription" entry point — Servers, Settings, the
        home banner, the empty home screen — opens the same page."""
        self.add_page.open(MODE_SUB)
        self._goto("add")

    def _subscription_urls(self) -> list[str]:
        """Every subscription link imported so far, in order, without
        repeats. Falls back to the single `subscription_url` of installs that
        predate the list."""
        from ..core.subscription import is_https_url
        s = storage.load_settings()
        urls = [u for u in (s.get("subscription_urls") or []) if u]
        if not urls and s.get("subscription_url"):
            urls = [s["subscription_url"]]
        # https only, as on import: a link saved by an old version (or typed
        # into settings.json) over http would send its token, and take the
        # server list back, in the clear.
        return [u for u in dict.fromkeys(urls) if is_https_url(u)]

    def _on_refresh_subscriptions(self) -> None:
        """Re-fetch every saved subscription and merge what it returns."""
        running = getattr(self, "_subs_refresher", None)
        if running is not None and running.isRunning():
            return
        urls = self._subscription_urls()
        if not urls:
            show_toast(self, tr("srv.no_subs"), kind="info", duration_ms=5000)
            return
        from .sub_workers import SubscriptionsRefresh
        self.servers_page.set_refreshing(True)
        self._subs_refresher = SubscriptionsRefresh(urls, parent=self)
        self._subs_refresher.done.connect(self._on_subs_refreshed)
        self._subs_refresher.crashed.connect(self._on_subs_refresh_crashed)
        self._subs_refresher.start()

    def _on_subs_refresh_crashed(self, text: str) -> None:
        self._subs_refresher = None
        self.servers_page.set_refreshing(False)
        show_toast(self, tr("srv.refresh_failed", n=1, reason=one_line(text, 80)),
                   kind="error", duration_ms=6000)

    def _on_subs_refreshed(self, agg: dict) -> None:
        self._subs_refresher = None
        self.servers_page.set_refreshing(False)
        # New servers are added, a subscription's own servers are updated
        # (providers rotate addresses and keys), nothing is ever deleted — a
        # failed or partial fetch must not wipe a working list.
        merged, added, updated = merge_with_prompt(self, self.configs, agg["configs"])
        if added or updated:
            self.configs[:] = merged
            storage.save_configs(self.configs)
            self._rebind_active_config()
            self._refresh_tray_pings()
        if agg["ok"] > 0:
            import time as _t
            changes = {"subscription_last_refresh": int(_t.time())}
            if agg["userinfo"] is not None:
                changes["subscription_userinfo"] = agg["userinfo"].to_dict()
            storage.update_settings(changes, fallback=self.manager.settings)
            self.settings_page.refresh_sub_info()
            self.home_page.refresh_sub_banner()
        self._refresh_home()
        text = tr("srv.refresh_done", ok=agg["ok"], total=agg["total"], added=added, updated=updated)
        errors = agg["errors"]
        if errors:
            text += "\n" + tr("srv.refresh_failed", n=len(errors),
                              reason=one_line(errors[0][1].title, 80))
        show_toast(self, text, kind="error" if errors else "success", duration_ms=6000)

    def _on_subscription_imported(self, imported: list) -> None:
        """The add page fetched (or was given) a subscription and the user
        pressed "Add to list"."""
        if not imported:
            return
        self._goto("servers")
        # The source-aware merge: a subscription may update its own servers,
        # never silently another source's.
        merged, added, replaced = merge_with_prompt(self, self.configs, imported)
        self.configs[:] = merged
        storage.save_configs(self.configs)
        self._rebind_active_config()
        # Same as a single add: freshly imported servers have no ping, so they
        # were missing from the tray's quick-connect list until a restart.
        self._refresh_tray_pings()
        # Subscription-Userinfo was just persisted by the page — reflect
        # the fresh remaining-traffic / expiry in the Settings subtitle + the
        # home-screen expiry banner. (refresh_sub_info lives on SettingsPage;
        # calling it on the window raised AttributeError, which the runtime
        # guard swallowed — so the import saved the servers to disk and then
        # died before refreshing the UI, auto-selecting a config or toasting.
        # From the user's side the import silently did nothing.)
        self.settings_page.refresh_sub_info()
        self.home_page.refresh_sub_banner()
        # If no active config yet, pick the first imported one
        if self._active_config is None and self.configs:
            self._active_config = self.configs[0]
            self.manager.update_settings(last_config_name=self._active_config.name)
        self._refresh_home()
        show_toast(
            self,
            tr("mw.toast_import_done", added=added, replaced=replaced),
            kind="success",
            duration_ms=4000,
        )
        # Auto-pick the fastest server out of what we just imported — as they
        # are stored NOW (a "keep both" may have renamed some), so a ping
        # measured against one server can never select another by its name.
        from ..core.subscription import endpoint_key
        landed = {(c.source, endpoint_key(c)) for c in imported}
        self._auto_pick_fastest(
            [c for c in self.configs if (c.source, endpoint_key(c)) in landed])

    def _auto_pick_fastest(self, candidates: list[ProxyConfig]) -> None:
        """TCP-ping each candidate; once all results in, switch to min-latency."""
        if not candidates:
            return
        from .pinger import PingerThread
        results: dict[str, Optional[int]] = {}

        def on_pinged(name: str, ms) -> None:
            results[name] = ms

        def on_finished() -> None:
            valid = [(n, ms) for n, ms in results.items() if ms is not None]
            if not valid:
                show_toast(
                    self, tr("mw.toast_no_servers_respond"),
                    kind="error", duration_ms=5000,
                )
                return
            fastest_name, fastest_ms = min(valid, key=lambda x: x[1])
            cfg = next((c for c in self.configs if c.name == fastest_name), None)
            if cfg is None:
                return
            # Don't yank the user's active server if they're already
            # connected — they explicitly picked it. Just notify.
            if self.manager.is_connected():
                show_toast(
                    self,
                    tr("mw.toast_fastest_connected", name=fastest_name[:40], ms=fastest_ms),
                    kind="info", duration_ms=6000,
                )
                return
            self._active_config = cfg
            self.manager.update_settings(last_config_name=cfg.name)
            self._refresh_home()
            short = fastest_name[:40] + ("…" if len(fastest_name) > 40 else "")
            show_toast(
                self,
                tr("mw.toast_fastest_picked", name=short, ms=fastest_ms),
                kind="success", duration_ms=6000,
            )

        self._autopick_pinger = PingerThread(candidates, parent=self)
        self._autopick_pinger.pinged.connect(on_pinged)
        self._autopick_pinger.finished.connect(on_finished)
        self._autopick_pinger.start()

    def _on_settings_changed(self) -> None:
        """Repaint custom-painted widgets after any setting change.

        QSS-styled widgets re-style automatically when app.setStyleSheet
        is called in SettingsPage._on_theme_changed; but our QPainter-
        drawn widgets (WorldMapWidget, eventually Sparkline +
        CircleConnectButton) read palette on each paintEvent — and
        paintEvent isn't auto-triggered by a stylesheet change.
        update() schedules one.
        """
        self.home_page.refresh_ip_setting()
        self.home_page.circle.update()
        self.home_page.update()
        self.nav.update()

    def _on_open_diagnostics(self) -> None:
        """Network Diagnostics — adapters, routes, MTU, server, TCP/UDP tests."""
        from .diagnostics_dialog import DiagnosticsDialog
        DiagnosticsDialog(self.manager, self).exec()

    def _on_edit_bypass_apps(self) -> None:
        """Edit the user's list of apps that skip the VPN."""
        from .bypass_apps_dialog import BypassAppsDialog
        dlg = BypassAppsDialog(self.manager, self)
        if dlg.exec() and self.manager.is_connected():
            # Routing rules are baked into the config at connect time, so an
            # edit mid-session only lands on the next connection — say so
            # instead of letting the user think it took effect now.
            self.logs_page.append(tr("mw.bypass_apps_applied"))
            show_toast(self, tr("mw.bypass_apps_applied"), kind="info",
                       duration_ms=6000)

    def _on_edit_sites(self) -> None:
        dlg = SitesDialog(self)
        if dlg.exec() != SitesDialog.Accepted:
            return
        self.home_page.refresh_sites_count()
        self.settings_page.refresh_sites_count()
        if self.manager.is_connected():
            show_toast(
                self,
                tr("mw.toast_sites_updated_apply"),
                kind="info",
                duration_ms=5000,
            )
        else:
            show_toast(self, tr("mw.toast_sites_updated"), kind="success")

    # --- tray + window lifecycle ------------------------------------------

    def _on_show_window(self) -> None:
        """Bring the main window back from minimized / tray-hidden."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_close_to_tray(self) -> None:
        """X button: hide to tray. Real quit is via tray menu → Выход."""
        if self.tray.is_available():
            self.hide()
            # Show a hint once so the user knows the app is still running
            if not getattr(self, "_close_hint_shown", False):
                self.tray.show_message(
                    tr("mw.tray_minimized_title"),
                    tr("mw.tray_minimized_body"),
                )
                self._close_hint_shown = True
        else:
            # No tray support — fall back to real quit so user isn't stranded.
            self._on_quit_for_real()

    def _join_workers(self) -> None:
        """Stop + join every owned worker QThread before teardown.

        A QThread destroyed while still running aborts the whole process with a
        C++ qFatal ("QThread: Destroyed while thread is still running"). On quit
        the app-lifetime workers below (ping / probe / connect / update) may
        still be mid-run; requestInterruption()+quit()+wait() each so none is
        deleted while running. Best-effort + bounded — never let cleanup raise."""
        try:
            self.add_page.shutdown()
        except Exception:
            pass
        for name in ("_connect_worker", "_ip_probe", "_tray_pinger",
                     "_autopick_pinger", "_update_worker"):
            w = getattr(self, name, None)
            try:
                if w is not None and w.isRunning():
                    if hasattr(w, "requestInterruption"):
                        w.requestInterruption()
                    w.quit()
                    w.wait(4000)
            except Exception:
                pass

    def _on_quit_for_real(self) -> None:
        """Disconnect, tear down tray, terminate the QApplication event loop.

        v3.0.9: this is now the SINGLE source of truth for shutdown — the tray
        "Выход", the closeEvent real-quit, the installer ping, AND
        QApplication.aboutToQuit (OS session-end / any quit()) all route here.
        Guard against double-run so disconnect() fires exactly once."""
        if getattr(self, "_really_quitting", False):
            return
        self._really_quitting = True
        try:
            self._dns_watchdog.stop()
        except Exception:
            pass
        try:
            self._net_watchdog.stop()
        except Exception:
            pass
        self._join_workers()
        if self.manager.is_connected():
            self.manager.disconnect()
        elif self.manager.killswitch_held:
            # Not connected, but still blocking: never leave the machine
            # without internet after the app is gone.
            self.manager.release_killswitch()
        self.tray.hide()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    def _on_sub_autorefresh_added(self, count: int) -> None:
        """Subscription auto-refresh found N new servers — reload from
        storage so the picker, tray quick-connect, and home stay current.
        Toast the count so the user knows their list grew.
        """
        self.configs = storage.load_configs()
        self._rebind_active_config()
        self._refresh_home()
        self._refresh_tray_pings()
        msg = (tr("mw.toast_sub_refreshed_one") if count == 1
               else tr("mw.toast_sub_refreshed_many", n=count))
        show_toast(
            self,
            msg,
            kind="success", duration_ms=5000,
        )

    def _on_sub_userinfo_updated(self) -> None:
        """A subscription fetch refreshed the cached balance/expiry (and the
        'last refreshed' stamp) — re-render the Settings sub-info line and the
        home-screen expiry banner, even if no new servers arrived."""
        self.settings_page.refresh_sub_info()
        self.home_page.refresh_sub_banner()

    def _on_tray_config_picked(self, cfg: ProxyConfig) -> None:
        """User picked a config from the tray (quick-connect or submenu).

        Always end up connected: if currently disconnected, just
        connect. If already connected, disconnect first then connect
        to the newly-picked server. This matches the tray-menu
        affordance: clicking a server name means "I want to use this
        server NOW".
        """
        self._active_config = cfg
        self.manager.update_settings(last_config_name=cfg.name)
        if self.manager.is_connected():
            # Switching servers: the tunnel is down for a moment by design, so
            # the kill-switch stays up across it.
            self._do_disconnect(hold_killswitch=True)
        self._do_connect()
        self._refresh_home()

    # --- frameless resize support (v1.16.3) -------------------------------

    @staticmethod
    def _window_resize_allowed(settings: dict) -> bool:
        """Whether the window is user-resizable. Default False — a fixed-size
        window that can't drift. Advanced users opt in via the
        `allow_window_resize` setting. Gates BOTH the edge-handle install and
        the size-persistence in resizeEvent, so flipping it off guarantees no
        handles are ever created. Pure + unit-testable."""
        return bool(settings.get("allow_window_resize", False))

    @staticmethod
    def _screen_too_short_for(needed_h: int) -> bool:
        """True if the primary screen's available height can't comfortably fit
        a window of `needed_h` px (with headroom for the taskbar/titlebar).
        Drives the 'auto' compact-preset fallback. Never raises."""
        try:
            from PySide6.QtWidgets import QApplication
            scr = QApplication.primaryScreen()
            if scr is None:
                return False
            return scr.availableGeometry().height() < needed_h + 80
        except Exception:
            return False

    def _killswitch_holding(self) -> bool:
        """True while the kill-switch is blocking traffic with no tunnel up:
        the client is between reconnect attempts, or gave up and is waiting for
        the user. (Hard-wired to False from v3.1.0 to v3.8.3, when every
        teardown removed the rules.)"""
        return bool(self.manager.killswitch_held) and not self.manager.is_connected()

    def warn_killswitch_stuck(self) -> None:
        """Startup: kill-switch rules from a session that died are still in the
        firewall and this launch could not delete them (not elevated). Without
        this the user has no internet and nothing on screen to connect it to
        KaproTUN."""
        app_log.log("[protection] stale kill_switch rules survived startup (not elevated)")
        self.logs_page.append(
            "[!] Правила kill-switch от прошлой сессии остались в файрволе и "
            "блокируют интернет. Снять их может только запуск от администратора."
        )
        dlg = kit.OverlayDialog(self, wide=True)
        dlg.head("shield-alert", tr("mw.killswitch_stuck_title"), "", tone="danger")
        dlg.add_long_text(tr("mw.killswitch_stuck_body"), max_height=300)
        dlg.add_actions([("later", tr("mw.killswitch_stuck_later"), "secondary"),
                         ("relaunch", tr("mw.relaunch_admin"), "primary")], default="relaunch")
        if dlg.ask() == "relaunch":
            self.settings_page._on_relaunch_admin()

    def _note_killswitch_hold(self) -> None:
        """Auto-recovery has stopped with the kill-switch still armed. Say
        plainly that the internet is blocked on purpose and how to lift it —
        otherwise it just looks like the network died."""
        if not self._killswitch_holding():
            return
        self.logs_page.append(
            "[!] Kill-switch держит блокировку: VPN не восстановился, поэтому "
            "интернета нет. Нажми «ВКЛЮЧИТЬ», чтобы переподключиться, или сними "
            "галочку Kill-switch в настройках, чтобы вернуть интернет без VPN."
        )
        app_log.log("[protection] kill_switch holding with no tunnel")
        show_toast(self, tr("mw.toast_killswitch_hold"),
                   kind="error", duration_ms=15000)

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Persist size to settings + reposition the 8 edge resize-handles.

        Guards: Qt may fire resizeEvent during __init__ before manager
        or _resize_handles exist (e.g. the implicit resize fired by
        setMinimumSize). hasattr/getattr keeps early-init firing
        from crashing here.

        Persistence is cheap — storage.save_settings does an atomic
        temp-write + rename, so a crash mid-resize can't corrupt the
        settings file even at fast drag rates.
        """
        super().resizeEvent(event)
        sz = event.size()
        # Persist size ONLY in resizable mode. In fixed mode the size never
        # changes, and persisting a forced value would be pointless (and is
        # exactly the drift loop we removed). getattr keeps the early-init
        # resize (from setFixedSize/resize, before _allow_resize is read) safe.
        if getattr(self, "_allow_resize", False) and hasattr(self, "manager"):
            self.manager.update_settings(window_size=[sz.width(), sz.height()])
        handles = getattr(self, "_resize_handles", None)
        if handles is not None:
            handles.reposition()

    # --- shutdown ---------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._really_quitting:
            try:
                self._dns_watchdog.stop()
            except Exception:
                pass
            try:
                self._net_watchdog.stop()
            except Exception:
                pass
            self._join_workers()
            if self.manager.is_connected():
                self.manager.disconnect()
            elif self.manager.killswitch_held:
                self.manager.release_killswitch()
            event.accept()
        else:
            # Frameless mode doesn't show an X button, but Alt+F4 still
            # triggers closeEvent — route it through the tray-hide path.
            event.ignore()
            self._on_close_to_tray()
