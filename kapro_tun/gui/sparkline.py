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
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
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

    def _points(self, samples: deque[float], w: float, bottom: float,
                usable: float, peak: float) -> tuple[list[QPointF], list[float]]:
        """Padded + smoothed sample points mapped to widget coordinates, plus
        the smoothed values (for the peak marker)."""
        if not samples:
            return [], []
        pad = self.HISTORY - len(samples)
        vals = _smooth([0.0] * pad + list(samples), 3)
        step = w / max(self.HISTORY - 1, 1)
        pts = [QPointF(i * step, bottom - min(v / peak, 1.0) * usable)
               for i, v in enumerate(vals)]
        return pts, vals

    @staticmethod
    def _spline(pts: list[QPointF]) -> QPainterPath:
        """A smooth Catmull-Rom curve through the points (converted to cubic
        Beziers) — reads as a soft area chart instead of a jagged polyline."""
        path = QPainterPath()
        n = len(pts)
        if n == 0:
            return path
        path.moveTo(pts[0])
        if n < 3:
            for pt in pts[1:]:
                path.lineTo(pt)
            return path
        for i in range(n - 1):
            p0 = pts[i - 1] if i > 0 else pts[0]
            p1, p2 = pts[i], pts[i + 1]
            p3 = pts[i + 2] if i + 2 < n else pts[-1]
            c1 = QPointF(p1.x() + (p2.x() - p0.x()) / 6.0,
                         p1.y() + (p2.y() - p0.y()) / 6.0)
            c2 = QPointF(p2.x() - (p3.x() - p1.x()) / 6.0,
                         p2.y() - (p3.y() - p1.y()) / 6.0)
            path.cubicTo(c1, c2, p2)
        return path

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

        down_pts, down_vals = self._points(self._down, w, bottom, usable, peak)
        up_pts, _up_vals = self._points(self._up, w, bottom, usable, peak)
        down_path = self._spline(down_pts)
        up_path = self._spline(up_pts)

        def area(path: QPainterPath) -> QPainterPath:
            fill = QPainterPath(path)
            fill.lineTo(QPointF(float(w), bottom))
            fill.lineTo(QPointF(0.0, bottom))
            fill.closeSubpath()
            return fill

        def grad(color: QColor, top_alpha: int) -> QLinearGradient:
            g = QLinearGradient(0.0, top, 0.0, bottom)
            c0 = QColor(color); c0.setAlpha(top_alpha)
            c1 = QColor(color); c1.setAlpha(0)
            g.setColorAt(0.0, c0)
            g.setColorAt(1.0, c1)
            return g

        p.setPen(Qt.NoPen)

        # Upload — drawn first (behind): calm muted grey, subtle gradient area.
        if up_pts:
            p.setBrush(QBrush(grad(QColor(palette.TEXT_MUTED), 34)))
            p.drawPath(area(up_path))

        # Download — headline amber with a richer gradient fill so a burst
        # reads as a glowing swell rather than a thin line.
        accent = QColor(palette.ACCENT)
        if down_pts:
            p.setBrush(QBrush(grad(accent, 96)))
            p.drawPath(area(down_path))

        # Lines on top of the fills.
        pen_up = QPen(QColor(palette.TEXT_MUTED), 1.3)
        pen_up.setJoinStyle(Qt.RoundJoin)
        p.setBrush(Qt.NoBrush)
        p.setPen(pen_up)
        p.drawPath(up_path)

        pen_down = QPen(accent, 2.0)
        pen_down.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen_down)
        p.drawPath(down_path)

        # Peak marker — a small glowing dot at the highest download sample, so
        # the eye lands on the burst. Skipped when the line is essentially flat
        # (idle) to avoid a distracting dot pinned to the baseline.
        if down_vals and max(down_vals) > self.MIN_SCALE * 0.5:
            idx = max(range(len(down_vals)), key=down_vals.__getitem__)
            pt = down_pts[idx]
            halo = QColor(accent); halo.setAlpha(70)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(pt, 5.0, 5.0)
            p.setBrush(QBrush(accent))
            p.drawEllipse(pt, 2.4, 2.4)
