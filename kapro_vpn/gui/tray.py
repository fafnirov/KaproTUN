"""System-tray icon + context menu for KaproVPN.

Lets the user toggle the VPN, switch configs, and show/quit the app from
the Windows tray without ever opening the main window. The icon's color
reflects the current connection state.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..core.parser import ProxyConfig
from . import icons


class TrayManager(QObject):
    """Owns the QSystemTrayIcon and forwards menu actions as signals.

    Signals (consumed by MainWindow):
      toggle_clicked       — user picked Connect/Disconnect from the menu
      show_window_clicked  — user picked "Главное окно" or clicked the icon
      quit_clicked         — user picked Выход (real exit, not just hide)
      config_selected      — user picked a saved config from the submenu;
                             emits ProxyConfig
    """

    toggle_clicked = Signal()
    show_window_clicked = Signal()
    quit_clicked = Signal()
    config_selected = Signal(object)  # ProxyConfig

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)

        self.tray = QSystemTrayIcon(parent)
        self.tray.setIcon(icons.tray_idle())
        self.tray.setToolTip("KaproVPN — не подключено")
        self.tray.activated.connect(self._on_tray_activated)

        self.menu = QMenu()
        self._build_menu_skeleton()
        self.tray.setContextMenu(self.menu)

    # --- public API -------------------------------------------------------

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def hide(self) -> None:
        self.tray.hide()

    def is_available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def set_state(self, state: str, active_name: str = "") -> None:
        """state ∈ {'idle', 'connecting', 'connected'}"""
        if state == "connected":
            self.tray.setIcon(icons.tray_connected())
            self.tray.setToolTip(
                f"KaproVPN — подключено{f' · {active_name}' if active_name else ''}"
            )
            self.action_toggle.setText("Отключить")
        elif state == "connecting":
            self.tray.setIcon(icons.tray_connecting())
            self.tray.setToolTip("KaproVPN — подключение…")
            self.action_toggle.setText("Отменить подключение")
        else:
            self.tray.setIcon(icons.tray_idle())
            self.tray.setToolTip("KaproVPN — не подключено")
            self.action_toggle.setText("Подключить")

    def set_configs(self, configs: list[ProxyConfig], active_name: str = "") -> None:
        """Rebuild the configs submenu with the current saved list."""
        self.configs_menu.clear()
        if not configs:
            no_configs = QAction("(нет конфигов)", self.configs_menu)
            no_configs.setEnabled(False)
            self.configs_menu.addAction(no_configs)
            return
        for cfg in configs:
            act = QAction(cfg.name, self.configs_menu)
            act.setCheckable(True)
            act.setChecked(cfg.name == active_name)
            act.triggered.connect(lambda _checked=False, c=cfg: self.config_selected.emit(c))
            self.configs_menu.addAction(act)

    def show_message(self, title: str, body: str, duration_ms: int = 4000) -> None:
        """Native Windows balloon-tip notification from the tray."""
        if not self.is_available():
            return
        self.tray.showMessage(title, body, icons.app_icon(), duration_ms)

    # --- internal ---------------------------------------------------------

    def _build_menu_skeleton(self) -> None:
        self.action_toggle = QAction("Подключить", self.menu)
        self.action_toggle.triggered.connect(self.toggle_clicked)
        self.menu.addAction(self.action_toggle)

        self.menu.addSeparator()

        self.configs_menu = QMenu("Конфиги", self.menu)
        self.menu.addMenu(self.configs_menu)
        # populated later via set_configs()
        no_configs = QAction("(нет конфигов)", self.configs_menu)
        no_configs.setEnabled(False)
        self.configs_menu.addAction(no_configs)

        self.menu.addSeparator()

        self.action_show = QAction("Главное окно", self.menu)
        self.action_show.triggered.connect(self.show_window_clicked)
        self.menu.addAction(self.action_show)

        self.menu.addSeparator()

        self.action_quit = QAction("Выход", self.menu)
        self.action_quit.triggered.connect(self.quit_clicked)
        self.menu.addAction(self.action_quit)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Single left-click toggles main-window visibility.
        # Right-click already shows the context menu via Qt itself.
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window_clicked.emit()
