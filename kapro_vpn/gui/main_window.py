"""Main application window — compact, mobile-app-style single-screen layout."""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import admin, autostart, storage, tun2socks_installer, xray_installer, xray_stats
from ..core.controller import MODE_HTTP_PROXY, MODE_TUN
from ..core.controller import ConnectionError as VPNConnectionError
from ..core.controller import ConnectionManager
from ..core.parser import ProxyConfig
from . import icons
from .config_dialog import AddConfigDialog
from .configs_picker import ConfigsPickerDialog
from .installer_dialog import ensure_geoip_ru_cached, ensure_tun2socks_installed, ensure_xray_installed
from .sites_dialog import SitesDialog
from .titlebar import TitleBar
from .toast import show_toast
from .tray import TrayManager
from .widgets import CircleConnectButton, ConfigCard, NavBar, StatusLabel


# ----- Pages ---------------------------------------------------------------

class HomePage(QWidget):
    """Connect circle + active config card."""

    connect_clicked = Signal()
    card_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(0)

        # Title
        title = QLabel("KaproVPN")
        title.setObjectName("h1")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        layout.addStretch(1)

        # Connect button — centered with surrounding stretchers
        self.circle = CircleConnectButton()
        self.circle.clicked.connect(self.connect_clicked)
        circle_row = QHBoxLayout()
        circle_row.addStretch(1)
        circle_row.addWidget(self.circle)
        circle_row.addStretch(1)
        layout.addLayout(circle_row)
        layout.addSpacing(20)

        self.status_label = StatusLabel()
        layout.addWidget(self.status_label)

        # Traffic stats — only visible while connected. RichText so we can
        # tint the two values without nested widgets.
        self.traffic_label = QLabel("")
        self.traffic_label.setAlignment(Qt.AlignCenter)
        self.traffic_label.setTextFormat(Qt.RichText)
        layout.addSpacing(6)
        layout.addWidget(self.traffic_label)

        layout.addStretch(1)

        # Info row about split routing
        self._info_label = QLabel()
        self._info_label.setAlignment(Qt.AlignCenter)
        self._info_label.setTextFormat(Qt.RichText)
        self.refresh_sites_count()
        layout.addWidget(self._info_label)
        layout.addSpacing(12)

        # Active config card
        self.config_card = ConfigCard()
        self.config_card.clicked.connect(self.card_clicked)
        layout.addWidget(self.config_card)

    def set_state(self, state: str, detail: str = "") -> None:
        self.circle.set_state(state)
        self.status_label.set_state(state, detail)
        if state != "connected":
            self.traffic_label.clear()

    def set_traffic(self, up_rate: float, down_rate: float,
                    up_total: int, down_total: int) -> None:
        """Refresh the traffic-rate label. Called once per second."""
        from ..core.xray_stats import format_bytes, format_rate
        self.traffic_label.setText(
            f"<span style='color:#a1a1aa'>↑ </span>"
            f"<span style='color:#fafafa'>{format_rate(up_rate)}</span>"
            f"<span style='color:#71717a'>  ·  </span>"
            f"<span style='color:#a1a1aa'>↓ </span>"
            f"<span style='color:#fafafa'>{format_rate(down_rate)}</span>"
            f"<br/>"
            f"<span style='color:#71717a; font-size:8pt'>"
            f"за сессию: ↑ {format_bytes(up_total)}  ·  ↓ {format_bytes(down_total)}"
            f"</span>"
        )

    def set_config(self, cfg: Optional[ProxyConfig]) -> None:
        self.config_card.set_config(cfg)

    def refresh_sites_count(self) -> None:
        sites_count = len(storage.load_sites())
        self._info_label.setText(
            f"<span style='color:#a1a1aa'>Российские сайты — </span>"
            f"<span style='color:#fafafa'>{sites_count}</span> "
            f"<span style='color:#a1a1aa'>доменов идут напрямую</span>"
        )


class SettingsPage(QWidget):
    """Listen port, auto-proxy toggle, sites editor link, log viewer, about."""

    sites_clicked = Signal()
    logs_clicked = Signal()
    subscription_clicked = Signal()
    settings_changed = Signal()

    def __init__(self, manager: ConnectionManager, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        self._manager = manager

        # The settings list is taller than the fixed 760-px window can hold,
        # so wrap it in a scroll area. Wrapper layout has zero margins; the
        # `outer` layout (inside the scrolled content) keeps the actual
        # padding so scrollbar appears flush with the right edge.
        wrapper = QVBoxLayout(self)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setObjectName("settingsScroll")
        wrapper.addWidget(scroll)

        content = QWidget()
        content.setObjectName("page")
        scroll.setWidget(content)
        outer = QVBoxLayout(content)
        outer.setContentsMargins(24, 20, 24, 16)
        outer.setSpacing(14)

        title = QLabel("Настройки")
        title.setObjectName("h1")
        outer.addWidget(title)

        # --- Mode (http proxy vs TUN) ---
        mode_label = QLabel("Режим работы")
        mode_label.setObjectName("h2")
        outer.addWidget(mode_label)

        self.mode_group = QButtonGroup(self)
        self.radio_http = QRadioButton("HTTP-прокси (только браузер)")
        self.radio_tun = QRadioButton("TUN (все приложения, нужен админ)")
        self.mode_group.addButton(self.radio_http)
        self.mode_group.addButton(self.radio_tun)
        current_mode = manager.settings.get("mode", MODE_HTTP_PROXY)
        if current_mode == MODE_TUN:
            self.radio_tun.setChecked(True)
        else:
            self.radio_http.setChecked(True)
        self.radio_http.toggled.connect(self._on_mode_changed)
        self.radio_tun.toggled.connect(self._on_mode_changed)
        outer.addWidget(self.radio_http)
        http_hint = QLabel("Работает с Chrome/Edge/Firefox. ТГ и игры не туннелируются.")
        http_hint.setObjectName("dim")
        http_hint.setWordWrap(True)
        http_hint.setContentsMargins(28, 0, 0, 4)
        outer.addWidget(http_hint)
        outer.addWidget(self.radio_tun)
        tun_hint = QLabel("Туннелирует все программы системно: ТГ, Steam, игры.")
        tun_hint.setObjectName("dim")
        tun_hint.setWordWrap(True)
        tun_hint.setContentsMargins(28, 0, 0, 0)
        outer.addWidget(tun_hint)

        # Admin status / relaunch button shown only when relevant
        self._admin_row = QHBoxLayout()
        self._admin_label = QLabel()
        self._admin_label.setObjectName("dim")
        self._admin_row.addWidget(self._admin_label, stretch=1)
        self._relaunch_btn = QPushButton("Перезапустить от админа")
        self._relaunch_btn.clicked.connect(self._on_relaunch_admin)
        self._admin_row.addWidget(self._relaunch_btn)
        admin_row_widget = QWidget()
        admin_row_widget.setLayout(self._admin_row)
        outer.addWidget(admin_row_widget)
        self._refresh_admin_row()

        sep0 = QFrame()
        sep0.setFrameShape(QFrame.HLine)
        outer.addWidget(sep0)

        # --- Port ---
        port_block = QVBoxLayout()
        port_block.setSpacing(4)
        port_label = QLabel("Порт локального прокси")
        port_block.addWidget(port_label)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(manager.settings.get("listen_port", 2080)))
        self.port_spin.setFixedWidth(110)  # don't stretch across the row
        self.port_spin.valueChanged.connect(self._on_port_changed)
        port_block.addWidget(self.port_spin)
        port_hint = QLabel("Браузер должен ходить на 127.0.0.1:<этот порт>")
        port_hint.setObjectName("dim")
        port_block.addWidget(port_hint)
        outer.addLayout(port_block)

        # --- Auto system proxy ---
        self.auto_proxy_check = QCheckBox(
            "Автоматически ставить системный прокси Windows"
        )
        self.auto_proxy_check.setChecked(
            bool(manager.settings.get("auto_set_system_proxy", True))
        )
        self.auto_proxy_check.toggled.connect(self._on_auto_proxy_changed)
        outer.addWidget(self.auto_proxy_check)
        proxy_hint = QLabel("Только для HTTP-режима. В TUN не нужно.")
        proxy_hint.setObjectName("dim")
        proxy_hint.setContentsMargins(28, 0, 0, 0)
        outer.addWidget(proxy_hint)

        # --- Auto-start with Windows ---
        sep_startup = QFrame()
        sep_startup.setFrameShape(QFrame.HLine)
        outer.addWidget(sep_startup)

        startup_label = QLabel("При запуске Windows")
        startup_label.setObjectName("h2")
        outer.addWidget(startup_label)

        self.autostart_check = QCheckBox("Запускать вместе с Windows")
        self.autostart_check.setChecked(autostart.is_enabled())
        self.autostart_check.toggled.connect(self._on_autostart_changed)
        outer.addWidget(self.autostart_check)
        autostart_hint = QLabel("Запустится свёрнутым в трей.")
        autostart_hint.setObjectName("dim")
        autostart_hint.setContentsMargins(28, 0, 0, 0)
        outer.addWidget(autostart_hint)

        self.autoconnect_check = QCheckBox("Сразу подключаться при старте")
        self.autoconnect_check.setChecked(
            bool(manager.settings.get("autoconnect_on_launch", False))
        )
        self.autoconnect_check.toggled.connect(self._on_autoconnect_changed)
        outer.addWidget(self.autoconnect_check)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        outer.addWidget(sep)

        # --- Subscription import link ---
        sub_row, _ = self._make_link_row(
            "Импорт по подписке",
            "одна ссылка → много конфигов от провайдера",
            self.subscription_clicked.emit,
        )
        outer.addLayout(sub_row)

        # --- Sites editor link ---
        sites_row, self._sites_count_label = self._make_link_row(
            "Российские сайты (всегда напрямую)",
            f"{len(storage.load_sites())} доменов",
            self.sites_clicked.emit,
        )
        outer.addLayout(sites_row)

        # --- Logs viewer link ---
        logs_row, _ = self._make_link_row(
            "Логи Xray-core",
            "посмотреть последние строки",
            self.logs_clicked.emit,
        )
        outer.addLayout(logs_row)

        outer.addStretch(1)

        # --- About ---
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        outer.addWidget(sep2)

        engine_version = xray_installer.get_installed_version() or "не установлен"
        tun_version = tun2socks_installer.get_installed_version() or "не установлен"
        about = QLabel(
            f"<div style='color:#fafafa; font-weight:600'>KaproVPN v{__version__}</div>"
            f"<div style='color:#71717a; font-size:9pt'>Xray-core: {engine_version}</div>"
            f"<div style='color:#71717a; font-size:9pt'>tun2socks: {tun_version}</div>"
            f"<div style='color:#71717a; font-size:9pt'>GPL v3 · "
            f"<a href='https://github.com/fafnirov/KaproVPN' style='color:#f59e0b'>"
            f"github.com/fafnirov/KaproVPN</a></div>"
        )
        about.setOpenExternalLinks(True)
        about.setTextFormat(Qt.RichText)
        outer.addWidget(about)

    def _make_link_row(self, title: str, hint: str, on_click) -> tuple[QHBoxLayout, QLabel]:
        """Title + hint on the left, action button on the right. Returns (layout, hint_label)."""
        row = QHBoxLayout()
        row.setSpacing(8)
        text_block = QVBoxLayout()
        text_block.setSpacing(2)
        text_block.addWidget(QLabel(title))
        hint_lbl = QLabel(hint)
        hint_lbl.setObjectName("dim")
        text_block.addWidget(hint_lbl)
        row.addLayout(text_block, stretch=1)
        btn = QPushButton("Открыть")
        btn.clicked.connect(on_click)
        row.addWidget(btn)
        return row, hint_lbl

    def refresh_sites_count(self) -> None:
        if self._sites_count_label is not None:
            self._sites_count_label.setText(f"{len(storage.load_sites())} доменов")

    def _on_port_changed(self, value: int) -> None:
        self._manager.update_settings(listen_port=int(value))
        self.settings_changed.emit()

    def _on_auto_proxy_changed(self, checked: bool) -> None:
        self._manager.update_settings(auto_set_system_proxy=checked)
        self.settings_changed.emit()

    def _on_autostart_changed(self, checked: bool) -> None:
        ok = autostart.enable(minimized=True) if checked else autostart.disable()
        if not ok:
            # Revert checkbox state if the registry write failed
            self.autostart_check.blockSignals(True)
            self.autostart_check.setChecked(not checked)
            self.autostart_check.blockSignals(False)

    def _on_autoconnect_changed(self, checked: bool) -> None:
        self._manager.update_settings(autoconnect_on_launch=checked)
        self.settings_changed.emit()

    def _on_mode_changed(self, _checked: bool) -> None:
        # Both radios fire toggled — only act when the new selection is "checked".
        mode = MODE_TUN if self.radio_tun.isChecked() else MODE_HTTP_PROXY
        self._manager.update_settings(mode=mode)
        self._refresh_admin_row()
        self.settings_changed.emit()

    def _refresh_admin_row(self) -> None:
        is_admin = admin.is_admin()
        mode = self._manager.settings.get("mode", MODE_HTTP_PROXY)
        if mode == MODE_TUN and not is_admin:
            self._admin_label.setText("⚠ Запущено без прав администратора — TUN не сработает")
            self._relaunch_btn.setVisible(True)
        elif mode == MODE_TUN and is_admin:
            self._admin_label.setText("✓ Запущено с правами администратора")
            self._relaunch_btn.setVisible(False)
        else:
            self._admin_label.setText("")
            self._relaunch_btn.setVisible(False)

    def _on_relaunch_admin(self) -> None:
        import sys
        rc = admin.relaunch_as_admin()
        if rc > 32:
            # New elevated instance is starting. Quit this one.
            sys.exit(0)
        else:
            QMessageBox.warning(
                self, "Не удалось перезапустить",
                "Ты отменил запрос UAC или произошла ошибка. "
                "Запусти KaproVPN вручную правым кликом → «Запуск от имени администратора».",
            )


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
            self.failed.emit(f"Неожиданная ошибка: {type(e).__name__}: {e}")


class LogsPage(QWidget):
    """Read-only viewer for Xray-core logs."""

    back_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        back_btn = QPushButton("← Назад")
        back_btn.clicked.connect(self.back_clicked)
        header.addWidget(back_btn)
        header.addStretch(1)
        clear_btn = QPushButton("Очистить")
        clear_btn.clicked.connect(self._on_clear)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        title = QLabel("Логи Xray-core")
        title.setObjectName("h2")
        layout.addWidget(title)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        layout.addWidget(self.log_view, stretch=1)

    def append(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _on_clear(self) -> None:
        self.log_view.clear()


# ----- Main window ---------------------------------------------------------

class MainWindow(QMainWindow):
    log_received = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("KaproVPN")
        self.setWindowIcon(icons.app_icon())
        # 480 px gives Russian labels enough breathing room — at 420 the
        # radio-button text and a few hints were getting clipped.
        self.setFixedSize(480, 760)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowMinimizeButtonHint)

        self.manager = ConnectionManager(on_log=self.log_received.emit)
        self.configs: list[ProxyConfig] = storage.load_configs()
        self._active_config: Optional[ProxyConfig] = self._restore_last_config()
        self._connected_at: float = 0.0
        self._really_quitting = False
        self._connecting = False  # True while _ConnectWorker is running
        self._connect_worker: Optional[_ConnectWorker] = None
        self._prev_traffic: Optional[xray_stats.TrafficStats] = None

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
        self.home_page = HomePage()
        self.settings_page = SettingsPage(self.manager)
        self.logs_page = LogsPage()
        self.stack.addWidget(self.home_page)     # index 0
        self.stack.addWidget(self.settings_page) # index 1
        self.stack.addWidget(self.logs_page)     # index 2
        root.addWidget(self.stack, stretch=1)

        nav_sep = QFrame()
        nav_sep.setFrameShape(QFrame.HLine)
        root.addWidget(nav_sep)

        self.nav = NavBar()
        root.addWidget(self.nav)

        # System tray (gracefully no-op if user's DE doesn't expose one)
        self.tray = TrayManager(self)
        self.tray.show()

        self._wire_signals()
        self._refresh_home()
        self.nav.set_active("home")

        # Periodic status refresh — detects subprocess crashes and updates timer
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh_home)
        self._poll.start(1000)

    # --- wiring -----------------------------------------------------------

    def _wire_signals(self) -> None:
        self.home_page.connect_clicked.connect(self._on_connect_click)
        self.home_page.card_clicked.connect(self._on_open_picker)
        self.settings_page.sites_clicked.connect(self._on_edit_sites)
        self.settings_page.logs_clicked.connect(lambda: self.stack.setCurrentIndex(2))
        self.settings_page.subscription_clicked.connect(self._on_import_subscription)
        self.logs_page.back_clicked.connect(lambda: self._goto("settings"))
        self.nav.home_clicked.connect(lambda: self._goto("home"))
        self.nav.settings_clicked.connect(lambda: self._goto("settings"))
        self.nav.add_clicked.connect(self._on_add_config)
        self.log_received.connect(self.logs_page.append)
        # Title-bar window controls (frameless mode)
        self.titlebar.minimize_clicked.connect(self.showMinimized)
        self.titlebar.close_clicked.connect(self._on_close_to_tray)
        # System tray
        self.tray.toggle_clicked.connect(self._on_connect_click)
        self.tray.show_window_clicked.connect(self._on_show_window)
        self.tray.quit_clicked.connect(self._on_quit_for_real)
        self.tray.config_selected.connect(self._on_tray_config_picked)

    def _goto(self, name: str) -> None:
        if name == "home":
            self.stack.setCurrentIndex(0)
            self.nav.set_active("home")
        elif name == "settings":
            self.stack.setCurrentIndex(1)
            self.nav.set_active("settings")

    # --- state helpers ----------------------------------------------------

    def _restore_last_config(self) -> Optional[ProxyConfig]:
        last = self.manager.settings.get("last_config_name", "")
        if not last:
            return self.configs[0] if self.configs else None
        for c in self.configs:
            if c.name == last:
                return c
        return self.configs[0] if self.configs else None

    def _refresh_home(self) -> None:
        # Don't fight the connect worker for the button state — the
        # "connecting" pulse keeps animating until the worker finishes
        # and we get the success/failure signal.
        if self._connecting:
            self.tray.set_state("connecting", self._active_config.name if self._active_config else "")
            return

        # Detect external crash
        if self.manager._active is not None and not self.manager.process.is_running():
            self.logs_page.append(
                f"[!] Xray-core завершился неожиданно "
                f"(код {self.manager.process.returncode()}). Отключаюсь."
            )
            self.manager.disconnect()
            self._connected_at = 0.0

        active_name = self._active_config.name if self._active_config else ""
        if self.manager.is_connected():
            elapsed = int(time.time() - self._connected_at) if self._connected_at else 0
            mm, ss = divmod(elapsed, 60)
            hh, mm = divmod(mm, 60)
            timer = f"{hh:d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
            mode_tag = "TUN" if self.manager.current_mode() == MODE_TUN else "HTTP"
            self.home_page.set_state("connected", f"{timer} · {mode_tag}")
            self.tray.set_state("connected", active_name)
            self._poll_traffic()
        else:
            self.home_page.set_state("idle")
            self.tray.set_state("idle", active_name)
            self._prev_traffic = None  # reset session counter when disconnected

        self.home_page.set_config(self._active_config)
        self.tray.set_configs(self.configs, active_name)

    def _poll_traffic(self) -> None:
        """Pull the latest cumulative byte counters and feed rates to HomePage."""
        sample = xray_stats.query_stats()
        if sample is None:
            return
        if self._prev_traffic is None:
            # First sample of the session — record but don't display until we
            # have a delta to compute rate from.
            self._prev_traffic = sample
            return
        up_rate, down_rate = sample.delta_rate(self._prev_traffic)
        self._prev_traffic = sample
        self.home_page.set_traffic(
            up_rate, down_rate,
            sample.uplink_bytes, sample.downlink_bytes,
        )

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
            QMessageBox.information(
                self, "Нет конфига",
                "Сначала добавь конфиг — нажми «+» в нижней панели или тапни карточку.",
            )
            return
        self._do_connect()

    def _do_connect(self) -> None:
        if not ensure_xray_installed(self):
            return
        # In TUN mode we additionally need tun2socks + wintun.dll + geoip:ru list
        if self.manager.current_mode() == MODE_TUN:
            if not ensure_tun2socks_installed(self):
                return
            # Soft-required — TUN works without it but RU split-routing is
            # less comprehensive. Don't gate connection on it.
            ensure_geoip_ru_cached(self)

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

    def _on_connect_success(self) -> None:
        self._connecting = False
        self.manager.update_settings(last_config_name=self._active_config.name)
        self._connected_at = time.time()
        mode_tag = "TUN" if self.manager.current_mode() == MODE_TUN else "HTTP"
        self.logs_page.append(
            f"[*] Подключено к «{self._active_config.name}» ({mode_tag})"
        )
        show_toast(self, f"Подключено к «{self._active_config.name}»", kind="success")
        self._refresh_home()

    def _on_connect_failed(self, msg: str) -> None:
        self._connecting = False
        self.home_page.set_state("idle")
        self._refresh_home()
        QMessageBox.critical(self, "Не удалось подключиться", msg)

    def _do_disconnect(self) -> None:
        self.manager.disconnect()
        self._connected_at = 0.0
        self.logs_page.append("[*] Отключено, системный прокси восстановлен")
        show_toast(self, "Отключено", kind="info")
        self._refresh_home()

    def trigger_autoconnect(self) -> None:
        """Called from main.py shortly after launch if autoconnect_on_launch is on."""
        if self.manager.is_connected() or self._connecting:
            return
        if self._active_config is None:
            return
        self._do_connect()

    def _on_open_picker(self) -> None:
        current_name = self._active_config.name if self._active_config else ""
        dlg = ConfigsPickerDialog(self.configs, current_name, self)
        result = dlg.exec()
        # Always reload — picker may have mutated saved list via add/remove
        self.configs = storage.load_configs()
        if result == ConfigsPickerDialog.Accepted:
            chosen = dlg.selected_config()
            if chosen is not None:
                self._active_config = chosen
                self.manager.update_settings(last_config_name=chosen.name)
        else:
            # User cancelled but may have added/removed — re-sync selection.
            names = {c.name for c in self.configs}
            if self._active_config and self._active_config.name not in names:
                self._active_config = self.configs[0] if self.configs else None
        self._refresh_home()

    def _on_add_config(self) -> None:
        dlg = AddConfigDialog(self)
        if dlg.exec() != AddConfigDialog.Accepted:
            return
        new_cfg = dlg.result_config()
        if new_cfg is None:
            return
        for i, c in enumerate(self.configs):
            if c.name == new_cfg.name:
                self.configs[i] = new_cfg
                break
        else:
            self.configs.append(new_cfg)
        storage.save_configs(self.configs)
        self._active_config = new_cfg
        self.manager.update_settings(last_config_name=new_cfg.name)
        self._goto("home")
        self._refresh_home()
        show_toast(self, f"Конфиг «{new_cfg.name}» добавлен", kind="success")

    def _on_import_subscription(self) -> None:
        from .subscription_dialog import SubscriptionDialog
        dlg = SubscriptionDialog(self)
        if dlg.exec() != SubscriptionDialog.Accepted:
            return
        imported = dlg.imported_configs()
        if not imported:
            return
        existing_by_name = {c.name: i for i, c in enumerate(self.configs)}
        added = replaced = 0
        for cfg in imported:
            if cfg.name in existing_by_name:
                self.configs[existing_by_name[cfg.name]] = cfg
                replaced += 1
            else:
                self.configs.append(cfg)
                existing_by_name[cfg.name] = len(self.configs) - 1
                added += 1
        storage.save_configs(self.configs)
        # If no active config yet, pick the first imported one
        if self._active_config is None and self.configs:
            self._active_config = self.configs[0]
            self.manager.update_settings(last_config_name=self._active_config.name)
        self._refresh_home()
        show_toast(
            self,
            f"Импорт: +{added} новых, ↻{replaced} обновлено",
            kind="success",
            duration_ms=4000,
        )

    def _on_edit_sites(self) -> None:
        dlg = SitesDialog(self)
        if dlg.exec() != SitesDialog.Accepted:
            return
        self.home_page.refresh_sites_count()
        self.settings_page.refresh_sites_count()
        if self.manager.is_connected():
            show_toast(
                self,
                "Список сайтов обновлён. Применится при следующем подключении.",
                kind="info",
                duration_ms=5000,
            )
        else:
            show_toast(self, "Список сайтов обновлён", kind="success")

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
                    "KaproVPN свёрнут в трей",
                    "Программа работает в фоне. Чтобы полностью выйти — "
                    "правый клик по иконке в трее → Выход.",
                )
                self._close_hint_shown = True
        else:
            # No tray support — fall back to real quit so user isn't stranded.
            self._on_quit_for_real()

    def _on_quit_for_real(self) -> None:
        """Disconnect, tear down tray, terminate the QApplication event loop."""
        self._really_quitting = True
        if self.manager.is_connected():
            self.manager.disconnect()
        self.tray.hide()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    def _on_tray_config_picked(self, cfg: ProxyConfig) -> None:
        """User picked a config from the tray submenu — switch + reconnect."""
        self._active_config = cfg
        self.manager.update_settings(last_config_name=cfg.name)
        if self.manager.is_connected():
            self._do_disconnect()
            self._do_connect()
        self._refresh_home()

    # --- shutdown ---------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._really_quitting:
            if self.manager.is_connected():
                self.manager.disconnect()
            event.accept()
        else:
            # Frameless mode doesn't show an X button, but Alt+F4 still
            # triggers closeEvent — route it through the tray-hide path.
            event.ignore()
            self._on_close_to_tray()
