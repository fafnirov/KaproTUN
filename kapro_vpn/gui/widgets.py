"""Reusable Qt widgets for KaproVPN GUI."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.parser import ProxyConfig
from . import styles


class CircleConnectButton(QPushButton):
    """Large circular toggle button. Three visual states: idle, connecting, connected.

    The amber glow when connected uses a drop-shadow effect that is enabled/disabled
    rather than added/removed to keep size hints stable.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("ВКЛЮЧИТЬ", parent)
        self.setObjectName("circleBtn")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setBlurRadius(80)
        self._glow.setOffset(0, 0)
        self._glow.setColor(QColor(styles.ACCENT))
        self.setGraphicsEffect(self._glow)
        self._glow.setEnabled(False)

        self._state = "idle"

    def set_state(self, state: str) -> None:
        """state ∈ {'idle', 'connecting', 'connected'}"""
        if state == self._state:
            return
        self._state = state
        if state == "connected":
            self.setText("ПОДКЛЮЧЕНО")
            self._glow.setEnabled(True)
            self.setProperty("state", "connected")
        elif state == "connecting":
            self.setText("ПОДКЛЮЧЕНИЕ…")
            self._glow.setEnabled(False)
            self.setProperty("state", "connecting")
        else:
            self.setText("ВКЛЮЧИТЬ")
            self._glow.setEnabled(False)
            self.setProperty("state", "idle")
        # Force QSS re-evaluation for the property selector
        self.style().unpolish(self)
        self.style().polish(self)


class ConfigCard(QFrame):
    """Bottom card on the home screen showing the active/selected config.

    Click anywhere on the card to open the configs picker.
    """

    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("configCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(6)

        self.title = QLabel("Конфиг не выбран")
        self.title.setObjectName("cardTitle")
        self.title.setWordWrap(True)
        outer.addWidget(self.title)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        self.badge = QLabel("—")
        self.badge.setObjectName("cardBadge")
        self.sub = QLabel("Нажми, чтобы выбрать или добавить конфиг")
        self.sub.setObjectName("cardSub")
        bottom_row.addWidget(self.badge)
        bottom_row.addWidget(self.sub, stretch=1)
        chevron = QLabel("▾")
        chevron.setObjectName("dim")
        bottom_row.addWidget(chevron)
        outer.addLayout(bottom_row)

    def set_config(self, cfg: Optional[ProxyConfig]) -> None:
        if cfg is None:
            self.title.setText("Конфиг не выбран")
            self.badge.setText("—")
            self.sub.setText("Нажми, чтобы добавить конфиг")
            return
        self.title.setText(cfg.name)
        self.badge.setText(cfg.protocol.upper())
        server = cfg.outbound.get("server", "?")
        port = cfg.outbound.get("server_port", "?")
        self.sub.setText(f"{server}:{port}")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class IconButton(QPushButton):
    """Square text-icon button used in the bottom nav bar."""

    def __init__(self, glyph: str, tooltip: str = "", parent: Optional[QWidget] = None):
        super().__init__(glyph, parent)
        self.setObjectName("iconBtn")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        if tooltip:
            self.setToolTip(tooltip)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class NavBar(QWidget):
    """Bottom navigation: Home / Settings / Add."""

    home_clicked = Signal()
    settings_clicked = Signal()
    add_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)

        self.btn_home = IconButton("⌂", "Главная")
        self.btn_settings = IconButton("⚙", "Настройки")
        self.btn_add = IconButton("＋", "Добавить конфиг")

        self.btn_home.clicked.connect(self.home_clicked)
        self.btn_settings.clicked.connect(self.settings_clicked)
        self.btn_add.clicked.connect(self.add_clicked)

        layout.addStretch(1)
        layout.addWidget(self.btn_home)
        layout.addStretch(1)
        layout.addWidget(self.btn_settings)
        layout.addStretch(1)
        layout.addWidget(self.btn_add)
        layout.addStretch(1)

    def set_active(self, name: str) -> None:
        """name ∈ {'home', 'settings'}"""
        self.btn_home.set_active(name == "home")
        self.btn_settings.set_active(name == "settings")


class StatusLabel(QLabel):
    """Status text under the connect button. Color reflects connection state."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Не подключено", parent)
        self.setAlignment(Qt.AlignCenter)
        self.setObjectName("muted")

    def set_state(self, state: str, detail: str = "") -> None:
        if state == "connected":
            self.setText(f"Подключено · {detail}" if detail else "Подключено")
            self.setStyleSheet(f"color: {styles.ACCENT}; font-size: 10pt; font-weight: 500;")
        elif state == "connecting":
            self.setText("Подключение…")
            self.setStyleSheet(f"color: {styles.TEXT_MUTED}; font-size: 10pt;")
        else:
            self.setText(detail or "Не подключено")
            self.setStyleSheet(f"color: {styles.TEXT_MUTED}; font-size: 10pt;")
