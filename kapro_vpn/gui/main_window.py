"""Main application window — compact, mobile-app-style single-screen layout."""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import storage, xray_installer
from ..core.controller import ConnectionError as VPNConnectionError
from ..core.controller import ConnectionManager
from ..core.parser import ProxyConfig
from .config_dialog import AddConfigDialog
from .configs_picker import ConfigsPickerDialog
from .installer_dialog import ensure_xray_installed
from .sites_dialog import SitesDialog
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
    settings_changed = Signal()

    def __init__(self, manager: ConnectionManager, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        self._manager = manager

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 16)
        outer.setSpacing(14)

        title = QLabel("Настройки")
        title.setObjectName("h1")
        outer.addWidget(title)

        # --- Port ---
        port_block = QVBoxLayout()
        port_block.setSpacing(4)
        port_label = QLabel("Порт локального прокси")
        port_block.addWidget(port_label)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(manager.settings.get("listen_port", 2080)))
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

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        outer.addWidget(sep)

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
        about = QLabel(
            f"<div style='color:#fafafa; font-weight:600'>KaproVPN v{__version__}</div>"
            f"<div style='color:#71717a; font-size:9pt'>Xray-core: {engine_version}</div>"
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
        self.setFixedSize(420, 740)

        self.manager = ConnectionManager(on_log=self.log_received.emit)
        self.configs: list[ProxyConfig] = storage.load_configs()
        self._active_config: Optional[ProxyConfig] = self._restore_last_config()
        self._connected_at: float = 0.0

        # --- Layout: stacked pages + nav bar ---
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

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
        self.logs_page.back_clicked.connect(lambda: self._goto("settings"))
        self.nav.home_clicked.connect(lambda: self._goto("home"))
        self.nav.settings_clicked.connect(lambda: self._goto("settings"))
        self.nav.add_clicked.connect(self._on_add_config)
        self.log_received.connect(self.logs_page.append)

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
        # Detect external crash
        if self.manager._active is not None and not self.manager.process.is_running():
            self.logs_page.append(
                f"[!] Xray-core завершился неожиданно "
                f"(код {self.manager.process.returncode()}). Отключаюсь."
            )
            self.manager.disconnect()
            self._connected_at = 0.0

        if self.manager.is_connected():
            elapsed = int(time.time() - self._connected_at) if self._connected_at else 0
            mm, ss = divmod(elapsed, 60)
            hh, mm = divmod(mm, 60)
            timer = f"{hh:d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
            self.home_page.set_state("connected", timer)
        else:
            self.home_page.set_state("idle")

        self.home_page.set_config(self._active_config)

    # --- actions ----------------------------------------------------------

    def _on_connect_click(self) -> None:
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
        self.home_page.set_state("connecting")
        sites = storage.load_sites()
        try:
            self.manager.connect(self._active_config, sites)
        except VPNConnectionError as e:
            QMessageBox.critical(self, "Не удалось подключиться", str(e))
            self.home_page.set_state("idle")
            return
        self.manager.update_settings(last_config_name=self._active_config.name)
        self._connected_at = time.time()
        self.logs_page.append(f"[*] Подключено к «{self._active_config.name}»")
        self._refresh_home()

    def _do_disconnect(self) -> None:
        self.manager.disconnect()
        self._connected_at = 0.0
        self.logs_page.append("[*] Отключено, системный прокси восстановлен")
        self._refresh_home()

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

    def _on_edit_sites(self) -> None:
        dlg = SitesDialog(self)
        if dlg.exec() != SitesDialog.Accepted:
            return
        self.home_page.refresh_sites_count()
        self.settings_page.refresh_sites_count()
        if self.manager.is_connected():
            QMessageBox.information(
                self, "Список обновлён",
                "Изменения применятся при следующем подключении.",
            )

    # --- shutdown ---------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self.manager.is_connected():
            self.manager.disconnect()
        event.accept()
