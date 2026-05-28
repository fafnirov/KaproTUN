"""Main application window — compact, mobile-app-style single-screen layout."""
from __future__ import annotations

import sys
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
    QButtonGroup,
    QCheckBox,
    QComboBox,
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
from ..core import (
    admin, autostart, dns_options, storage, tun2socks_installer,
    updater, xray_installer, xray_stats,
)
from ..core.controller import MODE_HTTP_PROXY, MODE_TUN
from ..core.controller import ConnectionError as VPNConnectionError
from ..core.controller import ConnectionManager
from ..core.parser import ProxyConfig
from . import icons
from .add_page import AddConfigPage
from .onboarding import OnboardingPage
from .subscription_autorefresh import SubscriptionAutoRefresh
from .config_dialog import AddConfigDialog
from .stats_page import StatsPage
from .world_map import WorldMapWidget
from . import window_resize
from .configs_picker import ConfigsPickerDialog
from .installer_dialog import ensure_geoip_ru_cached, ensure_tun2socks_installed, ensure_xray_installed
from .sites_dialog import SitesDialog
from .sparkline import TrafficSparkline
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
        # v1.14.5: 20 → 28 — gives more breathing room between the
        # connect button's halo and the status text so the map+IP
        # block sits visually lower, as the user requested.
        layout.addSpacing(28)

        self.status_label = StatusLabel()
        layout.addWidget(self.status_label)

        # Public IP / country reveal — shown ~2 seconds after a successful
        # connect, hidden when idle or connecting. Empty when the probe
        # fails (so we don't show a misleading "fetching..." that might
        # never finish). v1.10.0.
        self.public_ip_label = QLabel("")
        self.public_ip_label.setAlignment(Qt.AlignCenter)
        self.public_ip_label.setTextFormat(Qt.RichText)
        self.public_ip_label.setObjectName("dim")
        self.public_ip_label.setVisible(False)
        layout.addSpacing(2)
        layout.addWidget(self.public_ip_label)

        # World map with a pin on the active VPN country (v1.14.0).
        # Centered, hidden until the IP probe resolves a known country
        # code. Theme follows the user's choice — getter reads settings
        # at paint time so it auto-updates on theme switch.
        # v1.14.1: bumped the leading addSpacing from 4 to 14 so the
        # map visually separates from the IP label above (user reported
        # they looked merged together — no overlap, just no breathing
        # room).
        self.world_map = WorldMapWidget()
        self.world_map.setVisible(False)
        map_row = QHBoxLayout()
        map_row.addStretch(1)
        map_row.addWidget(self.world_map)
        map_row.addStretch(1)
        layout.addSpacing(14)
        layout.addLayout(map_row)
        layout.addSpacing(8)

        # Traffic stats — only visible while connected. RichText so we can
        # tint the two values without nested widgets.
        self.traffic_label = QLabel("")
        self.traffic_label.setAlignment(Qt.AlignCenter)
        self.traffic_label.setTextFormat(Qt.RichText)
        layout.addSpacing(6)
        layout.addWidget(self.traffic_label)

        # Sparkline graph — 1-minute history of bandwidth, shown under the
        # numbers when connected. Hidden when idle.
        self.sparkline = TrafficSparkline()
        self.sparkline.setVisible(False)
        layout.addSpacing(4)
        layout.addWidget(self.sparkline)

        # v1.14.6: bottom stretch replaced with a small fixed gap so
        # the map sits right above "Прямые сайты". Previously the
        # stretch ate ~100 px of empty space when sparkline + traffic
        # rows were hidden (most of the time — xray Stats API often
        # returns nothing on TUN-mode connections). Result: a huge
        # void between the map and "Прямые сайты", which the user
        # rightly called out. Free vertical space now all goes to the
        # top stretch (line 83) between the title and the connect
        # circle — pushes the circle down to vertical centre and the
        # status/IP/map block hugs the "Прямые сайты" line below.
        layout.addSpacing(24)

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
            self.sparkline.setVisible(False)
            self.sparkline.reset()
            # Public IP banner + map only make sense while connected;
            # clear immediately on disconnect/connecting so the user
            # doesn't briefly see stale data from the previous session.
            self.public_ip_label.clear()
            self.public_ip_label.setVisible(False)
            self.world_map.set_country(None)
            self.world_map.setVisible(False)

    def set_public_ip(
        self,
        ip: str,
        country_name: str,
        city: Optional[str] = None,
        country_code: str = "",
    ) -> None:
        """Render the just-fetched public IP + plant a map pin.

        v1.14.0: country_code added so the WorldMapWidget can place
        the pin. Empty/unknown code → map stays hidden but IP label
        still shows (graceful degradation when the IP-probe fallback
        returned a hit without country info — e.g. ipify-only path).
        """
        if not ip:
            self.public_ip_label.clear()
            self.public_ip_label.setVisible(False)
            self.world_map.set_country(None)
            self.world_map.setVisible(False)
            return
        # Single line: "Ваш IP: 1.2.3.4 · Нидерланды (Amsterdam)". City
        # is the optional bit since ipinfo free tier sometimes omits it.
        place = country_name if country_name else "—"
        if city:
            place = f"{country_name} · {city}" if country_name else city
        self.public_ip_label.setText(
            f"<span style='color:#71717a'>Ваш IP: </span>"
            f"<span style='color:#fafafa'>{ip}</span>"
            f"<span style='color:#71717a'>  ·  {place}</span>"
        )
        self.public_ip_label.setVisible(True)
        # Map gets shown only when we have a known country — silent
        # hide when the probe fallback gave IP-only (httpbin.org).
        from .world_map import COUNTRY_COORDS
        if country_code and country_code.upper() in COUNTRY_COORDS:
            self.world_map.set_country(country_code)
            self.world_map.setVisible(True)
        else:
            self.world_map.set_country(None)
            self.world_map.setVisible(False)

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
        self.sparkline.setVisible(True)
        self.sparkline.add_sample(up_rate, down_rate)

    def set_config(self, cfg: Optional[ProxyConfig]) -> None:
        self.config_card.set_config(cfg)

    def refresh_sites_count(self) -> None:
        sites_count = len(storage.load_sites())
        self._info_label.setText(
            f"<span style='color:#a1a1aa'>Прямые сайты — </span>"
            f"<span style='color:#fafafa'>{sites_count}</span> "
            f"<span style='color:#a1a1aa'>доменов идут напрямую</span>"
        )


class SettingsPage(QWidget):
    """Listen port, auto-proxy toggle, sites editor link, log viewer, about."""

    sites_clicked = Signal()
    logs_clicked = Signal()
    subscription_clicked = Signal()
    check_updates_requested = Signal()
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
        # Per-OS phrasing for the admin requirement — Windows users see
        # "UAC", Unix users see "sudo/root".
        tun_admin_label = {
            "win32":  "TUN (все приложения, нужен админ)",
            "darwin": "TUN (все приложения, нужен sudo)",
        }.get(sys.platform, "TUN (все приложения, нужен root)")
        self.radio_tun = QRadioButton(tun_admin_label)
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

        # --- Kill-switch ---
        sep_kill = QFrame()
        sep_kill.setFrameShape(QFrame.HLine)
        outer.addWidget(sep_kill)

        kill_label = QLabel("Безопасность")
        kill_label.setObjectName("h2")
        outer.addWidget(kill_label)

        self.kill_check = QCheckBox("Kill-switch — не пускать трафик мимо VPN")
        self.kill_check.setChecked(
            bool(manager.settings.get("kill_switch", False))
        )
        self.kill_check.toggled.connect(self._on_kill_switch_changed)
        outer.addWidget(self.kill_check)
        kill_hint = QLabel(
            "Если xray упадёт или VPN отвалится — туннель останется поднят, "
            "иностранный трафик будет блокироваться вместо утечки через "
            "реальную сеть. Только для TUN-режима."
        )
        kill_hint.setObjectName("dim")
        kill_hint.setWordWrap(True)
        kill_hint.setContentsMargins(28, 0, 0, 0)
        outer.addWidget(kill_hint)

        # --- IPv6 leak protection (v1.11.0) ---
        # TUN tunnels IPv4 only. On IPv6-enabled hosts (most RU residential
        # ISPs hand out public v6), apps with AAAA records leak through
        # the real ISP. Default ON because the user is almost always
        # surprised when we explain it ("я думал VPN покрывает всё").
        self.ipv6_check = QCheckBox(
            "Защита от IPv6 leak — блокировать v6-трафик в TUN"
        )
        self.ipv6_check.setChecked(
            bool(manager.settings.get("ipv6_leak_protection", True))
        )
        self.ipv6_check.toggled.connect(self._on_ipv6_leak_changed)
        outer.addWidget(self.ipv6_check)
        ipv6_hint = QLabel(
            "TUN-режим туннелирует только IPv4. Если у вашего провайдера "
            "включён IPv6 (Билайн, МТС, Ростелеком обычно дают), v6-трафик "
            "идёт мимо туннеля и провайдер видит куда вы заходите. Это "
            "правило блокирует outbound IPv6 к публичным адресам через "
            "Windows Firewall — локальная сеть (fe80::) и mDNS не трогаются."
        )
        ipv6_hint.setObjectName("dim")
        ipv6_hint.setWordWrap(True)
        ipv6_hint.setContentsMargins(28, 0, 0, 0)
        outer.addWidget(ipv6_hint)

        # --- WebRTC leak protection (v1.16.0) ---
        # Browser WebRTC API uses STUN over UDP to discover the user's
        # public IP — and any JavaScript on the page can read it,
        # bypassing the VPN. HTTP-proxy mode is the most exposed
        # (system proxy only catches TCP); TUN mode is technically safe
        # but defence-in-depth is cheap. Default ON for both.
        self.webrtc_check = QCheckBox(
            "Защита от WebRTC leak — блокировать STUN UDP-трафик"
        )
        self.webrtc_check.setChecked(
            bool(manager.settings.get("webrtc_leak_protection", True))
        )
        self.webrtc_check.toggled.connect(self._on_webrtc_leak_changed)
        outer.addWidget(self.webrtc_check)
        webrtc_hint = QLabel(
            "Браузер через WebRTC может узнать ваш реальный IP, "
            "запросив STUN-сервер (это используется в видеозвонках). "
            "Любой JavaScript на странице может прочитать этот IP. "
            "Правило блокирует outbound UDP к STUN-портам (3478, 5349, "
            "19302, 19305-19308) через Windows Firewall. "
            "⚠ Может сломать видеозвонки в браузере (Google Meet, "
            "Discord-web, Jitsi). Десктопные приложения (Zoom, Teams, "
            "Discord native) — не страдают, у них свой STUN."
        )
        webrtc_hint.setObjectName("dim")
        webrtc_hint.setWordWrap(True)
        webrtc_hint.setContentsMargins(28, 0, 0, 0)
        outer.addWidget(webrtc_hint)

        # --- Public IP probe toggle (v1.10.0) ---
        # We dial one third-party endpoint (ipinfo.io) after connect to
        # show "Ваш IP: X (страна)" in the UI as visible proof the
        # tunnel works. Some users prefer zero "phone home"-looking
        # calls — let them opt out.
        self.ip_probe_check = QCheckBox(
            "Показывать публичный IP после подключения"
        )
        self.ip_probe_check.setChecked(
            bool(manager.settings.get("public_ip_probe", True))
        )
        self.ip_probe_check.toggled.connect(self._on_ip_probe_changed)
        outer.addWidget(self.ip_probe_check)
        ip_probe_hint = QLabel(
            "Один запрос к ipinfo.io через VPN-туннель после connect. "
            "Никаких user-ID, никакого логирования. Выключи если не хочешь "
            "никаких 'phone-home' запросов в принципе."
        )
        ip_probe_hint.setObjectName("dim")
        ip_probe_hint.setWordWrap(True)
        ip_probe_hint.setContentsMargins(28, 0, 0, 0)
        outer.addWidget(ip_probe_hint)

        # --- DNS server choice ---
        # Some subscription providers don't filter ads at the DNS layer, so
        # picking AdGuard here gives ad-blocking that works on any server.
        # System is the leave-it-alone default. RadioButton group instead of
        # a combo so the user sees all four options + their hints at once —
        # they're meaningfully different and a one-line dropdown loses that.
        sep_dns = QFrame()
        sep_dns.setFrameShape(QFrame.HLine)
        outer.addWidget(sep_dns)

        dns_label = QLabel("DNS-сервер")
        dns_label.setObjectName("h2")
        outer.addWidget(dns_label)

        self.dns_group = QButtonGroup(self)
        self.dns_group.setExclusive(True)
        self._dns_radios: dict[str, QRadioButton] = {}
        self._ublock_helper: Optional[QLabel] = None
        current_dns = str(manager.settings.get("dns_option", dns_options.DEFAULT_KEY))
        for opt in dns_options.OPTIONS:
            radio = QRadioButton(opt.label_ru)
            radio.setChecked(opt.key == current_dns)
            radio.toggled.connect(
                lambda checked, k=opt.key: self._on_dns_option_changed(k, checked)
            )
            self.dns_group.addButton(radio)
            outer.addWidget(radio)
            hint = QLabel(opt.hint_ru)
            hint.setObjectName("dim")
            hint.setWordWrap(True)
            hint.setContentsMargins(28, 0, 0, 4)
            outer.addWidget(hint)
            self._dns_radios[opt.key] = radio

            # YouTube-ads helper — shown only under AdGuard, because that's
            # the option users pick for "block ads" and immediately notice
            # YouTube isn't covered. Native YT ads come from the same
            # domains as content (googlevideo.com) so no DNS/SNI filter
            # can touch them — only DOM-level browser extensions can.
            # Rather than fake a fix or hide the limitation, point users
            # at the tool that actually works, with a one-click link.
            if opt.key == "adguard":
                self._ublock_helper = QLabel(
                    "📺 <b>YouTube-реклама всё равно показывается?</b> "
                    "Это нативные ad'ы — режутся только браузером. Установи "
                    "<a href='https://ublockorigin.com/' style='color:#f59e0b'>"
                    "uBlock Origin</a> для Chrome/Firefox/Edge — 30 секунд, "
                    "бесплатно, режет YouTube-ads на 100% (поверх нашего AdGuard)."
                )
                self._ublock_helper.setObjectName("dim")
                self._ublock_helper.setWordWrap(True)
                self._ublock_helper.setTextFormat(Qt.RichText)
                self._ublock_helper.setOpenExternalLinks(True)
                self._ublock_helper.setContentsMargins(28, 4, 0, 8)
                self._ublock_helper.setVisible(opt.key == current_dns)
                outer.addWidget(self._ublock_helper)

        dns_footer = QLabel(
            "DoH (зашифрованный DNS) — провайдер не видит запросы. "
            "Применяется при следующем подключении."
        )
        dns_footer.setObjectName("dim")
        dns_footer.setWordWrap(True)
        dns_footer.setContentsMargins(28, 0, 0, 0)
        outer.addWidget(dns_footer)

        # --- Language toggle ---
        # Lives in Security section because it's the only other "global
        # preference" — too small to deserve its own section header.
        from ..core import i18n as _i18n
        lang_row = QHBoxLayout()
        lang_row.setContentsMargins(0, 6, 0, 0)
        lang_label = QLabel(_i18n.tr("settings.language_label"))
        lang_row.addWidget(lang_label)
        lang_row.addStretch(1)
        self.lang_combo = QComboBox()
        # Order: Auto first (most users will leave it as detected),
        # then RU/EN alphabetical so it's predictable.
        self.lang_combo.addItem(_i18n.tr("settings.language_auto"), "auto")
        self.lang_combo.addItem("English", "en")
        self.lang_combo.addItem("Русский", "ru")
        current_lang = manager.settings.get("language", "auto")
        for i in range(self.lang_combo.count()):
            if self.lang_combo.itemData(i) == current_lang:
                self.lang_combo.setCurrentIndex(i)
                break
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        lang_row.addWidget(self.lang_combo)
        outer.addLayout(lang_row)
        lang_hint = QLabel(
            "Изменение применится после перезапуска KaproVPN."
            if _i18n.current_locale() == "ru"
            else "Changes take effect after KaproVPN restarts."
        )
        lang_hint.setObjectName("dim")
        lang_hint.setWordWrap(True)
        lang_hint.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(lang_hint)

        # --- Theme toggle (v1.13.0) ---
        # Same pattern as language: QComboBox with 3 options, takes effect
        # at next restart (Qt doesn't cleanly re-style already-constructed
        # widgets without each widget participating, and our code uses
        # both global stylesheet AND a few setStyleSheet calls in widgets
        # — restart is simpler than chasing every label).
        theme_row = QHBoxLayout()
        theme_row.setContentsMargins(0, 6, 0, 0)
        theme_label_text = "Тема" if _i18n.current_locale() == "ru" else "Theme"
        theme_label = QLabel(theme_label_text)
        theme_row.addWidget(theme_label)
        theme_row.addStretch(1)
        self.theme_combo = QComboBox()
        # Same order convention as language — Auto first (sensible default),
        # then alphabetical.
        auto_label = "Авто (по системе)" if _i18n.current_locale() == "ru" else "Auto (system)"
        dark_label = "Тёмная" if _i18n.current_locale() == "ru" else "Dark"
        light_label = "Светлая" if _i18n.current_locale() == "ru" else "Light"
        self.theme_combo.addItem(auto_label, "auto")
        self.theme_combo.addItem(dark_label, "dark")
        self.theme_combo.addItem(light_label, "light")
        current_theme = manager.settings.get("theme", "auto")
        for i in range(self.theme_combo.count()):
            if self.theme_combo.itemData(i) == current_theme:
                self.theme_combo.setCurrentIndex(i)
                break
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        theme_row.addWidget(self.theme_combo)
        outer.addLayout(theme_row)
        theme_hint = QLabel(
            "Применяется сразу. Если несколько мелких элементов "
            "(круглая кнопка, график) остались в старой палитре — "
            "перезапусти приложение, остальные обновятся."
            if _i18n.current_locale() == "ru"
            else "Applied instantly. If a few small widgets (round "
                 "button, graph) stay in the old palette, restart "
                 "KaproVPN to refresh them."
        )
        theme_hint.setObjectName("dim")
        theme_hint.setWordWrap(True)
        theme_hint.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(theme_hint)

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
            "Прямые сайты (всегда напрямую)",
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
        tun_row = f"<div style='color:#71717a; font-size:9pt'>tun2socks: {tun_version}</div>"
        # Xray's version string is long ("Xray 26.3.27 ... go1.26.1 windows/amd64")
        # — word-wrap so we don't clip the right edge of the panel.
        about = QLabel(
            f"<div style='color:#fafafa; font-weight:600'>KaproVPN v{__version__}</div>"
            f"<div style='color:#71717a; font-size:9pt'>Xray-core: {engine_version}</div>"
            f"{tun_row}"
            f"<div style='color:#71717a; font-size:9pt'>GPL v3 · "
            f"<a href='https://github.com/fafnirov/KaproVPN' style='color:#f59e0b'>"
            f"github.com/fafnirov/KaproVPN</a></div>"
        )
        about.setOpenExternalLinks(True)
        about.setTextFormat(Qt.RichText)
        about.setWordWrap(True)
        outer.addWidget(about)

        # --- Updates row ---
        upd_row = QHBoxLayout()
        upd_row.setSpacing(8)
        self.update_status_label = QLabel("")
        self.update_status_label.setObjectName("dim")
        self.update_status_label.setWordWrap(True)
        upd_row.addWidget(self.update_status_label, stretch=1)
        self.check_updates_btn = QPushButton("Проверить обновления")
        self.check_updates_btn.clicked.connect(self._on_check_updates_clicked)
        upd_row.addWidget(self.check_updates_btn)
        outer.addLayout(upd_row)

    def _make_link_row(self, title: str, hint: str, on_click) -> tuple[QHBoxLayout, QLabel]:
        """Title + hint on the left, action button on the right. Returns (layout, hint_label)."""
        row = QHBoxLayout()
        row.setSpacing(8)
        text_block = QVBoxLayout()
        text_block.setSpacing(2)
        title_lbl = QLabel(title)
        title_lbl.setWordWrap(True)
        text_block.addWidget(title_lbl)
        hint_lbl = QLabel(hint)
        hint_lbl.setObjectName("dim")
        hint_lbl.setWordWrap(True)
        text_block.addWidget(hint_lbl)
        row.addLayout(text_block, stretch=1)
        btn = QPushButton("Открыть")
        # Pin enough width that the QSS padding doesn't truncate the label
        # ("Открыт" instead of "Открыть") — Qt's sizeHint doesn't account
        # for QSS padding.
        btn.setMinimumWidth(96)
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

    def _on_kill_switch_changed(self, checked: bool) -> None:
        self._manager.update_settings(kill_switch=checked)
        self.settings_changed.emit()

    def _on_ipv6_leak_changed(self, checked: bool) -> None:
        self._manager.update_settings(ipv6_leak_protection=checked)
        self.settings_changed.emit()

    def _on_webrtc_leak_changed(self, checked: bool) -> None:
        self._manager.update_settings(webrtc_leak_protection=checked)
        self.settings_changed.emit()

    def _on_ip_probe_changed(self, checked: bool) -> None:
        self._manager.update_settings(public_ip_probe=checked)
        self.settings_changed.emit()

    def _on_dns_option_changed(self, key: str, checked: bool) -> None:
        # Both old and new radios fire toggled() — we only care about the
        # one being newly selected (checked=True). Saved immediately;
        # applied at the next connect (xray needs a restart to re-read
        # the dns block and the TUN adapter's resolver gets re-pinned in
        # _connect_tun).
        if not checked:
            return
        self._manager.update_settings(dns_option=key)
        # Show YouTube-ads helper only when AdGuard is active — under
        # the other options it'd be confusing noise.
        if self._ublock_helper is not None:
            self._ublock_helper.setVisible(key == "adguard")
        self.settings_changed.emit()

    def _on_theme_changed(self, _index: int) -> None:
        """Persist theme + apply it live to the running app.

        v1.13.0 saved the choice but required a restart to see it.
        User feedback: "выбрал светлую — никакой белезны нет, тёмная
        осталась". That's correct because we just stored to settings.
        v1.13.1 also calls `setStyleSheet` on the QApplication so the
        global QSS picks up the new palette immediately. Custom-painted
        widgets (Sparkline, CircleConnectButton) that read module-level
        DARK constants still need a restart to fully refresh — the hint
        below tells the user that's a known limitation.
        """
        new_theme = self.theme_combo.currentData()
        self._manager.update_settings(theme=new_theme)
        # Live re-style. QApplication is a singleton — instance() returns
        # ours. Setting the stylesheet recomputes layout for every widget
        # currently using that sheet, so the whole window re-paints with
        # the new palette in one frame.
        from PySide6.QtWidgets import QApplication
        from .styles import get_qss
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(get_qss(str(new_theme)))
        # Notify MainWindow so it can update() custom-painted widgets
        # (WorldMapWidget — Sparkline / CircleConnectButton are next).
        # Stylesheet change does not trigger paintEvent on QPainter-
        # drawn widgets, so we have to push it manually.
        self.settings_changed.emit()

    def _on_language_changed(self, _index: int) -> None:
        """Persist language choice. Takes effect on next launch — we don't
        rebuild the UI in-place because that means re-translating every
        widget that was constructed at startup (settings labels, tray
        menu items, etc.) and chasing every label is fragile. Restart
        is one extra click for a change users make ~once per install.
        """
        new_lang = self.lang_combo.currentData()
        self._manager.update_settings(language=new_lang)

    def _on_check_updates_clicked(self) -> None:
        """Forwarded to MainWindow which owns the worker thread."""
        self.check_updates_requested.emit()

    # --- update banner UI -------------------------------------------------

    def set_update_status(self, text: str, accent: bool = False) -> None:
        """Update the dim line next to the 'Проверить обновления' button."""
        color = "#f59e0b" if accent else "#71717a"
        weight = "600" if accent else "400"
        self.update_status_label.setText(
            f"<span style='color:{color}; font-weight:{weight}'>{text}</span>"
        )
        self.update_status_label.setTextFormat(Qt.RichText)

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
            self.failed.emit(f"Неожиданная ошибка: {type(e).__name__}: {e}")


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
            info = ip_probe.fetch_public_ip(
                socks_proxy=self._socks_proxy,
                locale=self._locale,
                debug=self.diag.emit,
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
        self.setMinimumSize(480, 780)
        saved_size = self.manager.settings.get("window_size", [480, 870])
        try:
            w, h = int(saved_size[0]), int(saved_size[1])
        except (TypeError, ValueError, IndexError):
            w, h = 480, 870
        # Clamp restored size to the minimum so a corrupted settings
        # file can't shrink us below usable.
        self.resize(max(w, 480), max(h, 780))
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
        self._tray_pinger: Optional[object] = None  # _PingerThread instance
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
        # World-map needs to know the current theme on every paint —
        # hand it a closure that reads settings live, so a theme switch
        # immediately reflects in the next map paint (no explicit
        # refresh wiring per widget).
        # Custom-painted widgets (read palette per paint) need a getter
        # that returns the live theme setting. One closure, multiple
        # widgets — keeps theme propagation one-line per widget.
        _theme = lambda: str(self.manager.settings.get("theme", "auto"))
        self.home_page.world_map.set_theme_getter(_theme)
        self.home_page.sparkline.set_theme_getter(_theme)
        self.settings_page = SettingsPage(self.manager)
        self.logs_page = LogsPage()
        self.add_page = AddConfigPage()
        self.onboarding_page = OnboardingPage()
        # v1.15.0: 24-hour bandwidth chart page. Same theme-getter so
        # the chart's colors track live theme switches.
        self.stats_page = StatsPage()
        self.stats_page.set_theme_getter(_theme)
        self.stack.addWidget(self.home_page)        # index 0
        self.stack.addWidget(self.settings_page)    # index 1
        self.stack.addWidget(self.logs_page)        # index 2
        self.stack.addWidget(self.add_page)         # index 3
        self.stack.addWidget(self.onboarding_page)  # index 4
        self.stack.addWidget(self.stats_page)       # index 5
        root.addWidget(self.stack, stretch=1)

        nav_sep = QFrame()
        nav_sep.setFrameShape(QFrame.HLine)
        root.addWidget(nav_sep)

        self.nav = NavBar()
        root.addWidget(self.nav)

        # v1.16.1: QSizeGrip fallback for macOS / Linux frameless resize.
        # On Windows we already get all 8 edges via WM_NCHITTEST so this
        # branch is skipped — the grip would otherwise sit awkwardly
        # under the nav-bar row competing with native edge-drag.
        if sys.platform != "win32":
            grip = window_resize.install_size_grip(self)
            grip.setParent(self)
            grip.raise_()  # stay on top of the nav-bar
            # Anchor to bottom-right via a moveEvent shim — addWidget'ing
            # into the existing nav-bar row would shift the bottom nav
            # icons. Reposition in resizeEvent below.
            self._size_grip = grip
        else:
            self._size_grip = None

        # System tray (gracefully no-op if user's DE doesn't expose one)
        self.tray = TrayManager(self)
        self.tray.show()

        self._wire_signals()
        self._refresh_home()
        # If this is a clean install (no saved configs), open onboarding
        # instead of the empty Home card — _goto("home") detects this.
        # First-time UX: user sees Welcome + 3 big actions, not a sad
        # "Конфиг не выбран" panel.
        if not self.configs:
            self._goto("home")  # routes to onboarding via empty-state hijack
        self.nav.set_active("home")

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
        self.home_page.card_clicked.connect(self._on_open_picker)
        self.settings_page.sites_clicked.connect(self._on_edit_sites)
        self.settings_page.logs_clicked.connect(lambda: self._goto("logs"))
        self.settings_page.subscription_clicked.connect(self._on_import_subscription)
        # v1.14.0: nudge custom-painted widgets to repaint after any
        # setting flip (esp. theme — stylesheet change doesn't trigger
        # paintEvent on QPainter-drawn widgets, only on QSS-styled ones).
        self.settings_page.settings_changed.connect(self._on_settings_changed)
        self.settings_page.check_updates_requested.connect(
            lambda: self._start_update_check(interactive=True)
        )
        self.logs_page.back_clicked.connect(lambda: self._goto("settings"))
        self.add_page.back_clicked.connect(lambda: self._goto("home"))
        self.add_page.config_ready.connect(self._on_add_page_saved)
        self.add_page.subscription_clicked.connect(self._on_import_subscription)
        # Onboarding: the two action buttons reuse the existing import-
        # subscription and add-config-page paths. Same destinations as
        # the bottom-nav "+" and Settings → Subscription import, just
        # exposed earlier to brand-new users with zero saved configs.
        self.onboarding_page.subscription_clicked.connect(self._on_import_subscription)
        self.onboarding_page.add_config_clicked.connect(self._on_open_add_page)
        self.nav.home_clicked.connect(lambda: self._goto("home"))
        self.nav.stats_clicked.connect(lambda: self._goto("stats"))
        self.nav.settings_clicked.connect(lambda: self._goto("settings"))
        self.nav.add_clicked.connect(self._on_open_add_page)
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
        # Empty-state hijack: any "go home" request when the user has
        # zero saved configs lands on the onboarding screen instead.
        # The bottom-nav highlight still says "home" so the spatial
        # model is consistent — onboarding is "what Home looks like
        # when you haven't done anything yet".
        if name == "home" and not self.configs:
            self._fade_to(4)  # onboarding stack index
            self.nav.set_active("home")
            return
        target_index, nav_key = {
            "home":     (0, "home"),
            "settings": (1, "settings"),
            "logs":     (2, None),     # no nav highlight for logs
            "add":      (3, "add"),
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
            return
        from .configs_picker import _PingerThread

        # Stop any previous pinger before starting a new one — avoids
        # racing two pingers for the same configs.
        if self._tray_pinger is not None:
            try:
                self._tray_pinger.quit()
            except Exception:
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

        self._tray_pinger = _PingerThread(list(self.configs), parent=self)
        self._tray_pinger.pinged.connect(on_pinged)
        self._tray_pinger.finished.connect(on_finished)
        self._tray_pinger.start()

    def _refresh_home(self) -> None:
        # Don't fight the connect worker for the button state — the
        # "connecting" pulse keeps animating until the worker finishes
        # and we get the success/failure signal.
        if self._connecting:
            self.tray.set_state("connecting", self._active_config.name if self._active_config else "")
            return

        # Detect external crash
        if self.manager._active is not None and not self.manager.process.is_running():
            kill_switch = bool(self.manager.settings.get("kill_switch", False))
            mode_is_tun = self.manager.current_mode() == MODE_TUN
            rc = self.manager.process.returncode()

            if kill_switch and mode_is_tun and self.manager.tun_process.is_running():
                # Kill-switch holds: leave TUN up so foreign traffic gets
                # dropped instead of leaking out via the real interface.
                # The user manually reconnects when ready.
                if not self._crash_notified:
                    self.logs_page.append(
                        f"[!] Xray-core упал (код {rc}). "
                        f"Kill-switch активен — туннель удерживается, "
                        f"иностранный трафик блокируется."
                    )
                    show_toast(
                        self,
                        "VPN упал. Kill-switch блокирует утечку. "
                        "Нажми «ВКЛЮЧИТЬ» для переподключения.",
                        kind="error",
                        duration_ms=10000,
                    )
                    self._crash_notified = True
            else:
                # Plain HTTP-mode crash (or TUN without kill-switch): try
                # to auto-reconnect a few times before giving up. Most
                # crashes are transient — a brief network blip or xray
                # config-reload glitch.
                if (self._reconnect_attempts < self._reconnect_max
                        and not self._reconnect_timer.isActive()):
                    delay = self._reconnect_backoff[self._reconnect_attempts]
                    self._reconnect_attempts += 1
                    self.logs_page.append(
                        f"[!] Xray-core упал (код {rc}). "
                        f"Авто-переподключение #{self._reconnect_attempts}/"
                        f"{self._reconnect_max} через {delay} с…"
                    )
                    show_toast(
                        self,
                        f"VPN упал. Переподключение #{self._reconnect_attempts}…",
                        kind="info", duration_ms=delay * 1000,
                    )
                    # Tear down xray/proxy state but DON'T clear self._active —
                    # the timer will reuse it for the reconnect attempt.
                    saved = self._active_config
                    self.manager.disconnect()
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
                            f"VPN не поднимается ({self._reconnect_max} попыток). "
                            f"Переподключи вручную.",
                            kind="error", duration_ms=10000,
                        )
                        self._crash_notified = True
                    self.manager.disconnect()
                    self._connected_at = 0.0
        else:
            # Reset crash-notified flag when state is healthy
            self._crash_notified = False
            # Successful steady-state — reset the auto-reconnect counter
            # so the NEXT crash gets a fresh 3-attempt budget instead of
            # immediately giving up.
            if self.manager.is_connected() and self._reconnect_attempts > 0:
                self._reconnect_attempts = 0

        active_name = self._active_config.name if self._active_config else ""
        if self.manager.is_connected():
            elapsed = int(time.time() - self._connected_at) if self._connected_at else 0
            mm, ss = divmod(elapsed, 60)
            hh, mm = divmod(mm, 60)
            timer = f"{hh:d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
            mode_tag = "TUN" if self.manager.current_mode() == MODE_TUN else "HTTP"
            self.home_page.set_state("connected", f"{timer} · {mode_tag}")
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
            self.home_page.set_state("idle")
            self.tray.set_state("idle", active_name)
            self._prev_traffic = None  # reset session counter when disconnected
            # Reset live-block on Stats: sparkline empties + badge greys.
            # Cheap no-op when already disconnected (page tracks its own flag).
            self.stats_page.set_live_connected(False)

        self.home_page.set_config(self._active_config)
        self.tray.set_configs(self.configs, active_name, self._tray_pings)

    def _poll_traffic(self) -> None:
        """Pull the latest cumulative byte counters and feed rates to HomePage.

        v1.15.4: source switched per mode. TUN mode → read kernel byte
        counters from the named TUN interface via psutil (rock-solid,
        zero subprocess overhead). HTTP-proxy mode → fall back to the
        old `xray api stats` subprocess (the only way to get per-outbound
        traffic when packets aren't isolated on a dedicated interface).

        The `xray api stats` route had been silently returning None
        in some user setups (v1.15.2/.3 user report) leaving the Stats
        page rates stuck on "0 Б/с" forever — psutil makes that case
        impossible because the TUN device counters always exist as
        soon as tun2socks brings the interface up.
        """
        if self.manager.current_mode() == MODE_TUN:
            from ..core.tun2socks_process import TUN_DEVICE_NAME
            sample = xray_stats.query_tun_iface_stats(TUN_DEVICE_NAME)
        else:
            sample = xray_stats.query_stats()
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
        # Successful connect ⇒ wipe the auto-reconnect counter so the
        # next crash gets its own full 3-attempt budget.
        self._reconnect_attempts = 0
        self.manager.update_settings(last_config_name=self._active_config.name)
        self._connected_at = time.time()
        mode_tag = "TUN" if self.manager.current_mode() == MODE_TUN else "HTTP"
        self.logs_page.append(
            f"[*] Подключено к «{self._active_config.name}» ({mode_tag})"
        )
        show_toast(self, f"Подключено к «{self._active_config.name}»", kind="success")
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
            QTimer.singleShot(2000, self._kick_ip_probe)

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

    def _kick_ip_probe(self) -> None:
        """Start the async public-IP fetch. Routes through SOCKS5 in
        HTTP-proxy mode (otherwise the probe would see the local IP);
        in TUN mode the system route table already tunnels everything,
        no proxy override needed.
        """
        # If the user disconnected in the 2s between connect-success
        # and this firing, bail — showing the IP for a now-dead session
        # would be misleading.
        if not self.manager.is_connected():
            return
        socks_proxy = None
        if self.manager.current_mode() == MODE_HTTP_PROXY:
            host = str(self.manager.settings.get("listen_host", "127.0.0.1"))
            port = int(self.manager.settings.get("listen_port", 2080))
            socks_proxy = f"{host}:{port + 1}"  # SOCKS5 inbound is port+1
        from ..core.i18n import current_locale
        self._ip_probe = _IpProbeWorker(socks_proxy, current_locale(), parent=self)
        self._ip_probe.resolved.connect(self._on_ip_probe_resolved)
        # Pipe probe diagnostics into the Logs page so a silent failure
        # (no IP shown, no obvious reason) becomes a one-glance debug.
        self._ip_probe.diag.connect(self.logs_page.append)
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
        # If this was triggered by auto-reconnect (we're mid-attempts),
        # silently let the timer try again instead of popping a modal
        # — the user would be furious to OK 3 dialogs in 30 seconds.
        if self._reconnect_attempts > 0:
            self.logs_page.append(
                f"[!] Попытка #{self._reconnect_attempts} не удалась: {msg}"
            )
            return
        QMessageBox.critical(self, "Не удалось подключиться", msg)

    def _do_auto_reconnect(self) -> None:
        """Fired by self._reconnect_timer after backoff elapses.

        Re-runs _do_connect with the same active config. _do_connect
        spawns its own worker and routes the result through
        _on_connect_success / _on_connect_failed — those handlers know
        about self._reconnect_attempts and continue the chain.
        """
        if self._active_config is None or self._connecting:
            return
        self.logs_page.append(
            f"[*] Авто-переподключение: попытка #{self._reconnect_attempts}…"
        )
        self._do_connect()

    def _do_disconnect(self) -> None:
        # User asked for it — kill the auto-reconnect chain too so we
        # don't bring the VPN back up against their wishes.
        self._reconnect_timer.stop()
        self._reconnect_attempts = 0
        self.manager.disconnect()
        self._connected_at = 0.0
        self.logs_page.append("[*] Отключено, системный прокси восстановлен")
        show_toast(self, "Отключено", kind="info")
        self._refresh_home()

    # --- update checking --------------------------------------------------

    def _start_update_check(self, interactive: bool = False) -> None:
        if self._update_worker is not None and self._update_worker.isRunning():
            return
        if interactive:
            self.settings_page.set_update_status("Проверяю…", accent=False)
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
        msg = f"Доступна KaproVPN v{info.version}"
        self.settings_page.set_update_status(
            f"{msg} — клик чтобы обновить", accent=True,
        )
        # Hook the Settings "Update" button to the in-app updater dialog.
        try:
            self.settings_page.check_updates_btn.clicked.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.settings_page.check_updates_btn.setText(f"Обновить до v{info.version}")
        self.settings_page.check_updates_btn.clicked.connect(
            lambda _checked=False, i=info: self._open_updater(i)
        )
        # Toast nudge — only on background check; if user explicitly
        # asked, the Settings banner is already telling them.
        if not interactive:
            show_toast(
                self,
                f"{msg} — открой Настройки чтобы обновить одним кликом",
                kind="info",
                duration_ms=8000,
            )

    def _on_no_update(self, interactive: bool) -> None:
        if interactive:
            self.settings_page.set_update_status(
                f"У тебя последняя версия (v{__version__})", accent=False,
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
        # Config list may have changed (add/remove) — re-rank for tray
        # quick-connect block.
        self._refresh_tray_pings()

    def _on_open_add_page(self) -> None:
        """Nav-bar '+' switches to the inline AddConfigPage."""
        self.add_page.reset()
        self._goto("add")

    def _on_add_page_saved(self, new_cfg: ProxyConfig) -> None:
        """User filled out AddConfigPage and clicked Save."""
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
            f"Импорт: +{added} новых, ↻{replaced} обновлено · "
            f"пингую серверы…",
            kind="success",
            duration_ms=4000,
        )
        # Auto-pick the fastest server out of what we just imported.
        # Done in background — toast appears when the sweep finishes.
        self._auto_pick_fastest(imported)

    def _auto_pick_fastest(self, candidates: list[ProxyConfig]) -> None:
        """TCP-ping each candidate; once all results in, switch to min-latency."""
        if not candidates:
            return
        from .configs_picker import _PingerThread
        results: dict[str, Optional[int]] = {}

        def on_pinged(name: str, ms) -> None:
            results[name] = ms

        def on_finished() -> None:
            valid = [(n, ms) for n, ms in results.items() if ms is not None]
            if not valid:
                show_toast(
                    self, "Ни один из импортированных серверов не отвечает",
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
                    f"Самый быстрый: {fastest_name[:40]} ({fastest_ms} мс). "
                    f"Сменишь сам.",
                    kind="info", duration_ms=6000,
                )
                return
            self._active_config = cfg
            self.manager.update_settings(last_config_name=cfg.name)
            self._refresh_home()
            short = fastest_name[:40] + ("…" if len(fastest_name) > 40 else "")
            show_toast(
                self,
                f"Выбран самый быстрый: {short} ({fastest_ms} мс)",
                kind="success", duration_ms=6000,
            )

        self._autopick_pinger = _PingerThread(candidates, parent=self)
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
        self.home_page.world_map.update()
        self.home_page.sparkline.update()
        self.home_page.circle.update()

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

    def _on_sub_autorefresh_added(self, count: int) -> None:
        """Subscription auto-refresh found N new servers — reload from
        storage so the picker, tray quick-connect, and home stay current.
        Toast the count so the user knows their list grew.
        """
        self.configs = storage.load_configs()
        self._refresh_home()
        self._refresh_tray_pings()
        show_toast(
            self,
            f"Подписка обновилась: +{count} нов{'ый' if count == 1 else 'ых'} сервер{'' if count == 1 else 'ов' if count > 4 else 'а'}",
            kind="success", duration_ms=5000,
        )

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
            self._do_disconnect()
        self._do_connect()
        self._refresh_home()

    # --- frameless resize support (v1.16.1) -------------------------------

    def nativeEvent(self, eventType, message):  # noqa: N802
        """Forward WM_NCHITTEST to window_resize so Windows treats the 6 px
        border around our client area as a native resize zone.

        Returns the Qt-required (handled: bool, result: int) tuple. For
        non-Windows / non-NCHITTEST events the helper says "not handled"
        and we fall through to the base implementation (which routes
        the message to the regular event filter chain).
        """
        handled, result = window_resize.handle_native_event(self, eventType, message)
        if handled:
            return True, result
        return super().nativeEvent(eventType, message)

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Persist the new size to settings on every resize + reposition
        the macOS/Linux size grip.

        Cheap — just writes a small JSON file. The manager.update_settings
        call ends up at storage.save_settings which atomic-writes a temp
        file + rename, so a crash mid-resize can't corrupt the file.
        Throttling could be added later if it shows up in profiles, but
        even fast drag-resize fires the event ~30 Hz max which is fine.
        """
        super().resizeEvent(event)
        sz = event.size()
        self.manager.update_settings(window_size=[sz.width(), sz.height()])
        # macOS/Linux QSizeGrip needs manual repositioning since we
        # don't put it in a layout (would shift nav-bar icons).
        if self._size_grip is not None:
            g = self._size_grip
            g.move(sz.width() - g.width(), sz.height() - g.height())

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
