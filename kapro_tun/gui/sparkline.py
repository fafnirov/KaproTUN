"""Tiny inline bandwidth graph rendered below the home-page speed numbers.

Holds the last ~60 samples (1 Hz → ~1 minute) for upload and download. Two
contrast lines on a shared, smoothly auto-scaled Y axis with soft horizontal
guide lines, so a brief burst is visible and a calm idle reads as a near-flat
baseline.

v2.1.0 readability pass:
  - download = brand amber (solid + faint fill, the headline series);
    upload = muted grey (solid, thinner — secondary, less aggressive).
    Colours come from the active palette (theme-aware).
  - soft horizontal grid (3 guide lines) so peaks have something to read against.
  - Y auto-scale over the visible window with HYSTERESIS: the scale EASES
    toward the target peak instead of snapping, and shrinks slower than it
    grows, so the lines don't jump on every sample.
  - light 3-point moving-average smoothing for display — softens 1-sample
    jitter while a real spike (sustained ≥2 samples) still shows.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from . import styles


def _smooth(values: list[float], window: int = 3) -> list[float]:
    """Centered moving average, `window` points. Edges use the shorter
    available span. Light enough to keep a real (multi-sample) spike."""
    n = len(values)
    if n < 2 or window < 2:
        return list(values)
    half = window // 2
    out: list[float] = []
    for i in range(n):
        seg = values[max(0, i - half):min(n, i + half + 1)]
        out.append(sum(seg) / len(seg))
    return out


class TrafficSparkline(QWidget):
    HISTORY = 60  # samples (1 Hz polling = 1 minute of history)
    MIN_SCALE = 32 * 1024  # 32 KB/s floor so a quiet line doesn't fill the chart
    GRID_LINES = 3  # soft horizontal guides between baseline and top

    # Hysteresis: how fast the Y-scale chases the target peak. Grow quickly so
    # a new burst is visible within ~3 samples; shrink slowly so the lines
    # don't visibly "rescale" the instant a burst ends.
    _SCALE_UP = 0.45
    _SCALE_DOWN = 0.08

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._h = 48
        self.setFixedHeight(self._h)
        self.setMinimumWidth(180)
        self._up: deque[float] = deque(maxlen=self.HISTORY)
        self._down: deque[float] = deque(maxlen=self.HISTORY)
        self._scale = float(self.MIN_SCALE)  # current (eased) Y-axis top
        self._theme_getter = lambda: "auto"

    def set_theme_getter(self, getter) -> None:
        self._theme_getter = getter

    def set_compact(self, compact: bool) -> None:
        """Shorter graph for the compact window preset."""
        self._h = 36 if compact else 48
        self.setFixedHeight(self._h)

    def add_sample(self, up_bps: float, down_bps: float) -> None:
        self._up.append(max(0.0, up_bps))
        self._down.append(max(0.0, down_bps))
        self._ease_scale()
        self.update()

    def reset(self) -> None:
        self._up.clear()
        self._down.clear()
        self._scale = float(self.MIN_SCALE)
        self.update()

    def _ease_scale(self) -> None:
        target = max(max(self._up, default=0.0), max(self._down, default=0.0),
                     float(self.MIN_SCALE))
        rate = self._SCALE_UP if target > self._scale else self._SCALE_DOWN
        self._scale += (target - self._scale) * rate

    def paintEvent(self, _event) -> None:
        if not self._down and not self._up:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        top = 4.0
        bottom = h - 4.0
        usable = max(bottom - top, 1.0)
        peak = max(self._scale, float(self.MIN_SCALE))

        palette = styles.get_active_palette(self._theme_getter())

        # Soft horizontal grid — gives peaks a reference without competing.
        p.setPen(QPen(QColor(palette.BORDER), 1.0, Qt.DotLine))
        for i in range(1, self.GRID_LINES + 1):
            y = bottom - (i / (self.GRID_LINES + 1)) * usable
            p.drawLine(QPointF(0.0, y), QPointF(float(w), y))

        def build(samples: deque[float]) -> QPainterPath:
            path = QPainterPath()
            if not samples:
                return path
            pad = self.HISTORY - len(samples)
            vals = _smooth([0.0] * pad + list(samples), 3)
            step = w / max(self.HISTORY - 1, 1)
            for i, v in enumerate(vals):
                y = bottom - min(v / peak, 1.0) * usable
                pt = QPointF(i * step, y)
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            return path

        down_path = build(self._down)
        up_path = build(self._up)

        # Download — primary amber with a faint fill so it reads as headline.
        accent = QColor(palette.ACCENT)
        fill = QPainterPath(down_path)
        fill.lineTo(QPointF(float(w), bottom))
        fill.lineTo(QPointF(0.0, bottom))
        fill.closeSubpath()
        fill_color = QColor(accent)
        fill_color.setAlpha(38)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(fill_color))
        p.drawPath(fill)

        pen_down = QPen(accent, 1.8)
        pen_down.setJoinStyle(Qt.RoundJoin)
        p.setBrush(Qt.NoBrush)
        p.setPen(pen_down)
        p.drawPath(down_path)

        # Upload — calmer muted grey, solid but thinner. Distinct from the
        # amber download AND brighter than the dotted grid, so it's legible
        # without competing for attention.
        pen_up = QPen(QColor(palette.TEXT_MUTED), 1.3)
        pen_up.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen_up)
        p.drawPath(up_path)
