"""Short notices over the bottom of the window: they fade in, stay a few
seconds and fade out. For things that need no answer ("list updated",
"connected to X"); a failure that must be acknowledged gets a dialog.

One at a time per window: a new one replaces the one on screen.
"""
from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QWidget

from . import tokens
from .icons_v2 import IconLabel

ToastKind = Literal["info", "success", "error"]

_ICON = {"info": ("info", "accent_text"), "success": ("check-circle", "success_text"),
         "error": ("x-circle", "danger_text")}


class Toast(QFrame):
    """A single notice shown over its parent window."""

    BOTTOM_OFFSET = tokens.TOAST_BOTTOM   # above the navigation bar
    FADE_MS = 220
    MAX_TEXT_W = 300

    def __init__(self, parent: QWidget, text: str, kind: ToastKind = "info",
                 duration_ms: int = 3500):
        super().__init__(parent)
        self.setObjectName("ktToast")
        self.kind = kind if kind in _ICON else "info"
        # Clicks go through to what is underneath: a notice must not block
        # the connect button while it is up.
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        row = QHBoxLayout(self)
        row.setContentsMargins(tokens.SP_3, tokens.SP_2H, tokens.SP_3H, tokens.SP_2H)
        row.setSpacing(tokens.SP_2H)
        name, token = _ICON[self.kind]
        row.addWidget(IconLabel(name, tokens.ICON_SM, token), 0, Qt.AlignTop)
        self.label = QLabel(text)
        self.label.setObjectName("ktToastText")
        # Notices quote server names; never let one be read as markup.
        self.label.setTextFormat(Qt.PlainText)
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(self.MAX_TEXT_W)
        row.addWidget(self.label)

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._enter = QPropertyAnimation(self._opacity, b"opacity", self)
        self._enter.setDuration(self.FADE_MS)
        self._enter.setStartValue(0.0)
        self._enter.setEndValue(1.0)
        self._enter.setEasingCurve(QEasingCurve.OutCubic)
        self._exit = QPropertyAnimation(self._opacity, b"opacity", self)
        self._exit.setDuration(self.FADE_MS)
        self._exit.setStartValue(1.0)
        self._exit.setEndValue(0.0)
        self._exit.setEasingCurve(QEasingCurve.InCubic)
        self._exit.finished.connect(self.deleteLater)
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.setInterval(duration_ms)
        self._dismiss_timer.timeout.connect(self.dismiss)

    def show_at_bottom(self) -> None:
        parent = self.parent()
        if not isinstance(parent, QWidget):
            return
        self.adjustSize()
        self.move((parent.width() - self.width()) // 2,
                  parent.height() - self.height() - self.BOTTOM_OFFSET)
        self.raise_()
        self.show()
        self._enter.start()
        self._dismiss_timer.start()

    def dismiss(self) -> None:
        self._dismiss_timer.stop()
        self._enter.stop()
        self._exit.start()


def show_toast(parent: QWidget, text: str, kind: ToastKind = "info",
               duration_ms: int = 3500) -> None:
    """Show a notice on `parent`, replacing the one on screen."""
    for existing in parent.findChildren(Toast):
        existing.dismiss()
    Toast(parent, text, kind, duration_ms).show_at_bottom()
