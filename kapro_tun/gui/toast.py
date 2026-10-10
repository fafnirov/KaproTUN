"""Non-modal toast notifications: slide-in from the bottom, auto-dismiss.

Used for incidental feedback ("Список обновлён", "Подключено к X") where
a modal dialog would be obnoxious. Critical errors (failed connect, etc.)
still go through QMessageBox because they need explicit acknowledgement.

One toast at a time per window — a new one preempts the current one with
a quick fade swap, so we don't end up with a tower of stacked widgets.
"""
from __future__ import annotations

from typing import Literal, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QWidget,
)

ToastKind = Literal["info", "success", "error"]


class Toast(QFrame):
    """A single transient notification shown over the parent window."""

    BOTTOM_OFFSET = 80   # px above the parent's bottom edge (above the nav bar)
    SLIDE_DISTANCE = 18  # px to translate during slide-in
    SLIDE_DURATION = 220

    def __init__(self, parent: QWidget, text: str, kind: ToastKind = "info",
                 duration_ms: int = 3500):
        super().__init__(parent)
        # Default kind maps to the QSS object name (one per visual variant)
        self.setObjectName({
            "info":    "toastInfo",
            "success": "toastSuccess",
            "error":   "toastError",
        }.get(kind, "toastInfo"))

        # Pointer events: let clicks pass through to the underlying widgets
        # rather than blocking the connect button while a toast is up.
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)

        glyph = {"info": "ℹ", "success": "✓", "error": "✕"}.get(kind, "ℹ")
        icon = QLabel(glyph)
        icon.setObjectName("toastIcon")
        layout.addWidget(icon)

        label = QLabel(text)
        # Toasts quote server names; never let one be read as markup.
        label.setTextFormat(Qt.PlainText)
        label.setObjectName("toastText")
        label.setWordWrap(True)
        label.setMaximumWidth(280)
        label.setFont(QFont("Segoe UI", 9))
        layout.addWidget(label)

        # Opacity effect drives the fade animations
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)

        self._enter = QPropertyAnimation(self._opacity, b"opacity", self)
        self._enter.setDuration(self.SLIDE_DURATION)
        self._enter.setStartValue(0.0)
        self._enter.setEndValue(1.0)
        self._enter.setEasingCurve(QEasingCurve.OutCubic)

        self._exit = QPropertyAnimation(self._opacity, b"opacity", self)
        self._exit.setDuration(self.SLIDE_DURATION)
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
        x = (parent.width() - self.width()) // 2
        y = parent.height() - self.height() - self.BOTTOM_OFFSET
        self.move(x, y)
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
    """Display a toast on `parent`, replacing any currently-shown one."""
    # Find and kill any in-flight toasts that are siblings of this one
    for existing in parent.findChildren(Toast):
        existing.dismiss()
    toast = Toast(parent, text, kind, duration_ms)
    toast.show_at_bottom()
