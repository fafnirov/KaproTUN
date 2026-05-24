"""Custom dark title bar for the frameless main window.

Replaces the native Windows chrome with the dark amber-accent design that
matches the rest of the app. Provides minimize and close buttons (close
actually hides to tray; real quit goes through the tray menu).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from . import icons


class TitleBar(QFrame):
    minimize_clicked = Signal()
    close_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setFixedHeight(36)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(8)

        # App icon mini
        self.icon_label = QLabel()
        self.icon_label.setPixmap(icons.app_icon().pixmap(18, 18))
        self.icon_label.setFixedWidth(20)
        layout.addWidget(self.icon_label)

        # Title text
        self.title_label = QLabel("KaproVPN")
        self.title_label.setObjectName("titleBarText")
        layout.addWidget(self.title_label)
        layout.addStretch(1)

        # Window controls
        self.btn_min = QPushButton("—")
        self.btn_min.setObjectName("titleBarBtn")
        self.btn_min.setFixedSize(44, 36)
        self.btn_min.setFocusPolicy(Qt.NoFocus)
        self.btn_min.clicked.connect(self.minimize_clicked)
        layout.addWidget(self.btn_min)

        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("titleBarCloseBtn")
        self.btn_close.setFixedSize(44, 36)
        self.btn_close.setFocusPolicy(Qt.NoFocus)
        self.btn_close.clicked.connect(self.close_clicked)
        layout.addWidget(self.btn_close)

        self._drag_offset: Optional[QPoint] = None

    # --- drag-to-move -----------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            # Distance from window top-left to mouse, in screen coords —
            # so we can apply it as an offset during move.
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.window().frameGeometry().topLeft()
            )
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)
