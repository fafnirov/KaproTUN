"""Tiny inline bandwidth graph rendered below the home-page speed numbers.

Holds the last ~60 samples (one per second → ~1 minute of history) for
upload and download separately. Two lines drawn on the same axes; the
y-axis auto-scales so a brief burst becomes visible and a calm idle
shows as a near-flat line. No labels, no grid — minimal eye-candy that
gives a feel for what the connection is doing.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import styles


class TrafficSparkline(QWidget):
    HISTORY = 60  # samples (1 Hz polling = 1 minute of history)
    MIN_SCALE = 32 * 1024  # 32 KB/s floor so a quiet line doesn't fill the chart

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(44)
        self.setMinimumWidth(180)
        self._up: deque[float] = deque(maxlen=self.HISTORY)
        self._down: deque[float] = deque(maxlen=self.HISTORY)
        # v1.14.0: theme-aware. Main window sets a getter that reads
        # settings.theme live; default 'auto' until then so the widget
        # still paints sanely if used in isolation.
        self._theme_getter = lambda: "auto"

    def set_theme_getter(self, getter) -> None:
        self._theme_getter = getter

    def add_sample(self, up_bps: float, down_bps: float) -> None:
        self._up.append(max(0.0, up_bps))
        self._down.append(max(0.0, down_bps))
        self.update()

    def reset(self) -> None:
        self._up.clear()
        self._down.clear()
        self.update()

    def paintEvent(self, _event) -> None:
        if not self._down and not self._up:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        n = max(len(self._up), len(self._down), 2)
        peak = max(max(self._up, default=0.0), max(self._down, default=0.0),
                   self.MIN_SCALE)

        def line(samples: deque[float], color: QColor) -> QPainterPath:
            path = QPainterPath()
            if not samples:
                return path
            # Pad short histories with zeros so the line still slides
            # in from the right edge instead of jumping mid-chart.
            pad = self.HISTORY - len(samples)
            values = [0.0] * pad + list(samples)
            step = w / max(self.HISTORY - 1, 1)
            for i, v in enumerate(values):
                y = h - 4 - (v / peak) * (h - 8)
                pt = QPointF(i * step, y)
                if i == 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
            return path

        # v1.14.0: read palette per paint so theme switch immediately
        # picks up new colors. TEXT_DIM differs between dark (#71717a)
        # and light (#78716c) — the latter sits better on the warm
        # off-white background.
        palette = styles.get_active_palette(self._theme_getter())

        # Download line — primary amber (same in both themes — brand)
        accent = QColor(palette.ACCENT)
        pen_down = QPen(accent, 1.6)
        pen_down.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen_down)
        p.drawPath(line(self._down, accent))

        # Upload line — dimmer / muted, theme-tinted
        text_dim = QColor(palette.TEXT_DIM)
        pen_up = QPen(text_dim, 1.2)
        pen_up.setStyle(Qt.DashLine)
        p.setPen(pen_up)
        p.drawPath(line(self._up, text_dim))
