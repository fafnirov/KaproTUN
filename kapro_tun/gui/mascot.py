"""Kapatun's existing illustration, used only where it helps explain a task."""
import sys
from pathlib import Path

from PySide6.QtCore import QEasingCurve, Qt, QVariantAnimation
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QLabel

from ..core import storage
from ..core.i18n import tr
from . import kit, tokens


def reduced_motion() -> bool:
    if storage.load_settings().get("reduce_motion", False):
        return True
    if sys.platform == "win32":
        import ctypes
        enabled = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return not enabled.value
    return False


class Mascot(QLabel):
    def __init__(self, size=80, parent=None):
        super().__init__(parent)
        self.setAccessibleName(tr("mascot.name"))
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        pix = QPixmap(str(Path(__file__).resolve().parent.parent / "data" / "kapatun.png"))
        scaled = pix.scaled(size * 2, size * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(2)
        self.setPixmap(scaled)
        self._shown = False
        self._opacity = 1.0
        self._fade = QVariantAnimation(self)
        self._fade.valueChanged.connect(self._set_opacity)
        self._fade.setDuration(220)
        self._fade.setStartValue(0.65)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

    def _set_opacity(self, value):
        self._opacity = float(value)
        self.update()

    def paintEvent(self, event):
        # A child graphics effect inside the dialog's shadow misplaces pixmaps
        # on some Qt backends. Paint opacity locally, without a second effect.
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        pixmap = self.pixmap()
        size = pixmap.deviceIndependentSize()
        painter.drawPixmap(round((self.width() - size.width()) / 2),
                           round((self.height() - size.height()) / 2), pixmap)
        painter.end()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._shown and not reduced_motion():
            self._fade.start()
        self._shown = True

    def hideEvent(self, event):
        self._fade.stop()
        self._set_opacity(1)
        super().hideEvent(event)


class WhatsNewDialog(kit.OverlayDialog):
    def __init__(self, parent=None):
        super().__init__(parent, wide=True)
        self.head("info", tr("mascot.whats_new"), tr("mascot.intro"))
        self.body.addWidget(Mascot(tokens.SP_6 * 4), 0, Qt.AlignHCenter)
        for key in ("favorites", "routing", "subscriptions"):
            self.add_widget(kit.label(tr("mascot." + key), "dialogText", wrap=True))
        self.add_actions([("close", tr("mascot.got_it"), "primary")],
                         handlers={"close": self.accept})
        self.enable_scrolling()
