"""Main application window."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core import storage
from ..core.controller import ConnectionError as VPNConnectionError
from ..core.controller import ConnectionManager
from ..core.parser import ProxyConfig
from .config_dialog import AddConfigDialog
from .installer_dialog import ensure_singbox_installed
from .sites_dialog import SitesDialog


class MainWindow(QMainWindow):
    log_received = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Russian VPN")
        self.resize(820, 620)

        self.manager = ConnectionManager(on_log=self.log_received.emit)
        self.configs: list[ProxyConfig] = storage.load_configs()

        self._build_ui()
        self._build_menu()
        self._refresh_config_list()
        self._refresh_status()

        self.log_received.connect(self._append_log_line)

        # Health-check timer: sing-box might crash; reflect that in UI.
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh_status)
        self._poll.start(2000)

    # --- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Status header
        status_row = QHBoxLayout()
        self.status_badge = QLabel("● Отключено")
        self.status_badge.setObjectName("statusBadge")
        self.status_badge.setProperty("class", "off")
        status_row.addWidget(self.status_badge)

        self.status_detail = QLabel("")
        self.status_detail.setObjectName("muted")
        status_row.addWidget(self.status_detail)
        status_row.addStretch(1)
        root.addLayout(status_row)

        # Splitter: configs+actions on top, logs below
        splitter = QSplitter(Qt.Vertical)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel("Конфиги")
        title.setObjectName("sectionTitle")
        top_layout.addWidget(title)

        self.config_list = QListWidget()
        self.config_list.itemDoubleClicked.connect(self._on_connect)
        top_layout.addWidget(self.config_list, stretch=1)

        config_buttons = QHBoxLayout()
        add_btn = QPushButton("+ Добавить")
        add_btn.clicked.connect(self._on_add_config)
        remove_btn = QPushButton("Удалить")
        remove_btn.clicked.connect(self._on_remove_config)
        config_buttons.addWidget(add_btn)
        config_buttons.addWidget(remove_btn)
        config_buttons.addStretch(1)

        sites_btn = QPushButton("Российские сайты...")
        sites_btn.clicked.connect(self._on_edit_sites)
        config_buttons.addWidget(sites_btn)
        top_layout.addLayout(config_buttons)

        actions_row = QHBoxLayout()
        self.connect_btn = QPushButton("Подключить")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._on_connect)

        self.disconnect_btn = QPushButton("Отключить")
        self.disconnect_btn.setObjectName("danger")
        self.disconnect_btn.clicked.connect(self._on_disconnect)
        self.disconnect_btn.setEnabled(False)

        actions_row.addWidget(self.connect_btn)
        actions_row.addWidget(self.disconnect_btn)
        actions_row.addStretch(1)
        top_layout.addLayout(actions_row)

        splitter.addWidget(top)

        # Log panel
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        log_title = QLabel("Логи sing-box")
        log_title.setObjectName("sectionTitle")
        bottom_layout.addWidget(log_title)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        bottom_layout.addWidget(self.log_view, stretch=1)
        splitter.addWidget(bottom)

        splitter.setSizes([380, 180])
        root.addWidget(splitter, stretch=1)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("Файл")

        import_action = QAction("Импорт из файла...", self)
        import_action.triggered.connect(self._on_import_file)
        file_menu.addAction(import_action)

        file_menu.addSeparator()
        exit_action = QAction("Выход", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        help_menu = self.menuBar().addMenu("Помощь")
        about_action = QAction("О программе", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    # --- list helpers -----------------------------------------------------

    def _refresh_config_list(self) -> None:
        self.config_list.clear()
        for cfg in self.configs:
            srv = cfg.outbound.get("server", "?")
            port = cfg.outbound.get("server_port", "?")
            item = QListWidgetItem(f"{cfg.name}   ·   {cfg.protocol}   ·   {srv}:{port}")
            item.setData(Qt.UserRole, cfg)
            self.config_list.addItem(item)

        # Restore last selection if any
        last_name = self.manager.settings.get("last_config_name", "")
        if last_name:
            for i in range(self.config_list.count()):
                if self.configs[i].name == last_name:
                    self.config_list.setCurrentRow(i)
                    break
        if self.config_list.currentRow() < 0 and self.configs:
            self.config_list.setCurrentRow(0)

    def _selected_config(self) -> Optional[ProxyConfig]:
        row = self.config_list.currentRow()
        if row < 0 or row >= len(self.configs):
            return None
        return self.configs[row]

    def _refresh_status(self) -> None:
        active = self.manager.active_config()
        if self.manager.is_connected() and active:
            self.status_badge.setText(f"● Подключено: {active.name}")
            self.status_badge.setObjectName("statusBadgeOn")
            host = self.manager.settings.get("listen_host", "127.0.0.1")
            port = self.manager.settings.get("listen_port", 2080)
            self.status_detail.setText(f"127.0.0.1:{port} · HTTP/SOCKS5")
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
        else:
            # Detect crash: marked active but process died
            if self.manager._active is not None and not self.manager.process.is_running():
                self._append_log_line(
                    "[!] sing-box завершился неожиданно "
                    f"(код {self.manager.process.returncode()}). Отключаюсь."
                )
                self.manager.disconnect()
            self.status_badge.setText("● Отключено")
            self.status_badge.setObjectName("statusBadgeOff")
            self.status_detail.setText("")
            self.connect_btn.setEnabled(bool(self.configs))
            self.disconnect_btn.setEnabled(False)

        # Re-apply stylesheet so the objectName change takes effect
        self.status_badge.style().unpolish(self.status_badge)
        self.status_badge.style().polish(self.status_badge)

    # --- actions ----------------------------------------------------------

    def _on_add_config(self) -> None:
        dlg = AddConfigDialog(self)
        if dlg.exec() == AddConfigDialog.Accepted:
            cfg = dlg.result_config()
            if cfg is None:
                return
            # Replace if name exists
            for i, existing in enumerate(self.configs):
                if existing.name == cfg.name:
                    self.configs[i] = cfg
                    break
            else:
                self.configs.append(cfg)
            storage.save_configs(self.configs)
            self._refresh_config_list()

    def _on_remove_config(self) -> None:
        cfg = self._selected_config()
        if cfg is None:
            return
        confirm = QMessageBox.question(
            self, "Удалить", f"Удалить конфиг «{cfg.name}»?"
        )
        if confirm != QMessageBox.Yes:
            return
        self.configs = [c for c in self.configs if c.name != cfg.name]
        storage.save_configs(self.configs)
        self._refresh_config_list()

    def _on_import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Импорт", "", "Текст (*.txt);;Все файлы (*.*)"
        )
        if not path:
            return
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as e:
            QMessageBox.critical(self, "Ошибка чтения", str(e))
            return
        from ..core.parser import parse, ParseError
        added = 0
        errors: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                cfg = parse(line)
            except ParseError as e:
                errors.append(f"{line[:40]}... — {e}")
                continue
            self.configs.append(cfg)
            added += 1
        storage.save_configs(self.configs)
        self._refresh_config_list()
        msg = f"Импортировано: {added}"
        if errors:
            msg += "\n\nОшибки:\n" + "\n".join(errors[:10])
        QMessageBox.information(self, "Импорт", msg)

    def _on_connect(self) -> None:
        cfg = self._selected_config()
        if cfg is None:
            QMessageBox.information(self, "Конфиг", "Выбери конфиг из списка.")
            return
        if not ensure_singbox_installed(self):
            return
        sites = storage.load_sites()
        try:
            self.manager.connect(cfg, sites)
        except VPNConnectionError as e:
            QMessageBox.critical(self, "Не удалось подключиться", str(e))
            return
        self.manager.update_settings(last_config_name=cfg.name)
        self._append_log_line(f"[*] Подключено к «{cfg.name}»")
        self._refresh_status()

    def _on_disconnect(self) -> None:
        self.manager.disconnect()
        self._append_log_line("[*] Отключено, системный прокси восстановлен")
        self._refresh_status()

    def _on_edit_sites(self) -> None:
        dlg = SitesDialog(self)
        if dlg.exec() == SitesDialog.Accepted and self.manager.is_connected():
            QMessageBox.information(
                self, "Список обновлён",
                "Список применится при следующем подключении. "
                "Переподключись, чтобы изменения вступили в силу.",
            )

    def _on_about(self) -> None:
        QMessageBox.about(
            self, "О программе",
            "Russian VPN\n\n"
            "Клиент на базе sing-box со встроенным split-routing'ом: "
            "российские сайты идут через ваш реальный IP, остальное — через прокси.\n\n"
            "Поддержка: trojan, vless, vmess, shadowsocks, hysteria2.",
        )

    def _append_log_line(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    # --- shutdown ---------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self.manager.is_connected():
            self.manager.disconnect()
        event.accept()
