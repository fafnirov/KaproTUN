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

from ..core.i18n import tr
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
        self.tray.setToolTip(tr("tray.tooltip_idle"))
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
            tip = tr("tray.tooltip_connected")
            if active_name:
                tip = f"{tip} · {active_name}"
            self.tray.setToolTip(tip)
            self.action_toggle.setText(tr("tray.menu_disconnect"))
        elif state == "connecting":
            self.tray.setIcon(icons.tray_connecting())
            self.tray.setToolTip(tr("tray.tooltip_connecting"))
            self.action_toggle.setText(tr("tray.menu_cancel_connect"))
        else:
            self.tray.setIcon(icons.tray_idle())
            self.tray.setToolTip(tr("tray.tooltip_idle"))
            self.action_toggle.setText(tr("tray.menu_connect"))

    def set_configs(
        self,
        configs: list[ProxyConfig],
        active_name: str = "",
        pings: Optional[dict[str, Optional[int]]] = None,
    ) -> None:
        """Rebuild both the quick-connect top-strip and the full submenu.

        `pings` maps config name → latency in ms (None = unreachable,
        -1 = UDP-only / skipped). Used to surface the 3 lowest-ping
        configs at the top level so the user can switch in one click
        without opening the main window.
        """
        pings = pings or {}

        # --- Top-level quick-connect actions ---
        # We rebuild these dynamically; remove old ones first by tag.
        for act in list(self._quick_actions):
            self.menu.removeAction(act)
        self._quick_actions.clear()
        if self._quick_separator is not None:
            self.menu.removeAction(self._quick_separator)
            self._quick_separator = None

        # Pick top-3 by ping. Skip configs without a usable ping value
        # (pending or unreachable) — quick-connect should ONLY surface
        # servers that are definitely up.
        rankable = []
        for cfg in configs:
            ms = pings.get(cfg.name)
            if isinstance(ms, int) and ms >= 0:  # excludes None, -1
                rankable.append((ms, cfg))
        rankable.sort(key=lambda x: x[0])
        top3 = rankable[:3]

        if top3:
            for ms, cfg in top3:
                # Format: "⚡ NL BMV2 · 123 ms"
                marker = "▶" if cfg.name == active_name else "⚡"
                act = QAction(f"{marker} {cfg.name}  ·  {ms} мс", self.menu)
                act.triggered.connect(
                    lambda _checked=False, c=cfg: self.config_selected.emit(c)
                )
                # Insert BEFORE the toggle action so quick-connect is the
                # very first thing in the menu.
                self.menu.insertAction(self.action_toggle, act)
                self._quick_actions.append(act)
            sep = self.menu.insertSeparator(self.action_toggle)
            self._quick_separator = sep

        # --- Full configs submenu (everything saved, not just top-3) ---
        self.configs_menu.clear()
        if not configs:
            no_configs = QAction(tr("tray.menu_no_configs"), self.configs_menu)
            no_configs.setEnabled(False)
            self.configs_menu.addAction(no_configs)
            return
        for cfg in configs:
            ms = pings.get(cfg.name)
            if isinstance(ms, int) and ms >= 0:
                label = f"{cfg.name}  ·  {tr('picker.ping_ms', ms=ms)}"
            elif ms == -1:
                label = f"{cfg.name}  ·  {tr('picker.ping_udp')}"
            elif ms is None:
                label = f"{cfg.name}  ·  ?"
            else:
                label = cfg.name
            act = QAction(label, self.configs_menu)
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
        # Quick-connect top-3 entries (rebuilt on every set_configs).
        # Tracked here so we can remove them cleanly without nuking the
        # rest of the menu.
        self._quick_actions: list[QAction] = []
        self._quick_separator: Optional[QAction] = None

        self.action_toggle = QAction(tr("tray.menu_connect"), self.menu)
        self.action_toggle.triggered.connect(self.toggle_clicked)
        self.menu.addAction(self.action_toggle)

        self.menu.addSeparator()

        self.configs_menu = QMenu(tr("tray.menu_configs"), self.menu)
        self.menu.addMenu(self.configs_menu)
        # populated later via set_configs()
        no_configs = QAction(tr("tray.menu_no_configs"), self.configs_menu)
        no_configs.setEnabled(False)
        self.configs_menu.addAction(no_configs)

        self.menu.addSeparator()

        self.action_show = QAction(tr("tray.menu_show"), self.menu)
        self.action_show.triggered.connect(self.show_window_clicked)
        self.menu.addAction(self.action_show)

        self.menu.addSeparator()

        self.action_quit = QAction(tr("tray.menu_quit"), self.menu)
        self.action_quit.triggered.connect(self.quit_clicked)
        self.menu.addAction(self.action_quit)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Single left-click toggles main-window visibility.
        # Right-click already shows the context menu via Qt itself.
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window_clicked.emit()
