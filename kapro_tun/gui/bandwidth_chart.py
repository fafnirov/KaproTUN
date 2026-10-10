"""The 24-hour traffic chart: one pair of bars per hour — downloaded (amber)
and, narrower on top of it, uploaded (grey). Drawn with QPainter, no plotting
library.

Reads core.bandwidth_history (one sample a minute, kept for 24 hours) and
sums it by hour. An hour without bars is an hour the VPN was off: traffic is
only counted while the tunnel is up.

Colours come from the theme tokens at paint time, so a theme switch shows on
the next repaint.
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core import bandwidth_history
from ..core.i18n import tr
from . import tokens

HOURS = 24
_PLOT_H = 88          # bars and grid
_LABELS_H = 20        # the time axis under them
_BAR_DOWN, _BAR_UP = 10, 4


def window(now: int) -> tuple[int, int]:
    """(start, end) of the 24 clock hours shown: the last one is the hour
    in progress, so the axis labels fall on whole hours."""
    local = time.localtime(now)
    end = now - (local.tm_min * 60 + local.tm_sec) + 3600
    return end - HOURS * 3600, end


def hourly(samples: list, now: int) -> list[tuple[int, int]]:
    """(down, up) bytes for each of the 24 clock hours up to and including
    the current one, oldest first."""
    start, _end = window(now)
    buckets = [[0, 0] for _ in range(HOURS)]
    for s in samples:
        i = (int(s.ts) - start) // 3600
        if 0 <= i < HOURS:
            buckets[i][0] += max(0, int(s.down_bytes))
            buckets[i][1] += max(0, int(s.up_bytes))
    return [(d, u) for d, u in buckets]


class BandwidthChartWidget(QWidget):
    """Call refresh() when the page is shown or new samples may have landed;
    the widget does not poll."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(_PLOT_H + _LABELS_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._samples: list[bandwidth_history.Sample] = []
        self._now = int(time.time())

    def set_theme_getter(self, _getter) -> None:
        """Kept for callers from before the tokens; the theme is read from
        them now."""

    def refresh(self) -> None:
        self._samples = bandwidth_history.recent_24h()
        self._now = int(time.time())
        self.update()

    def has_data(self) -> bool:
        return any(d or u for d, u in hourly(self._samples, self._now))

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.StyleChange:
            self.update()
        super().changeEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = tokens.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = float(self.width())
        base = float(_PLOT_H) - 0.5

        grid = QPen(QColor(c.line), 1)
        grid.setDashPattern([2, 3])
        p.setPen(grid)
        for frac in (0.0, 0.5, 1.0):
            y = round(base - frac * (_PLOT_H - 5)) + 0.5
            p.drawLine(0, y, w, y)

        buckets = hourly(self._samples, self._now)
        peak = max((max(d, u) for d, u in buckets), default=0)
        slot = w / HOURS
        if peak > 0:
            p.setPen(Qt.NoPen)
            for i, (down, up) in enumerate(buckets):
                cx = slot * (i + 0.5)
                for value, width, colour, radius in ((down, _BAR_DOWN, c.accent, 2),
                                                     (up, _BAR_UP, c.chart_up, 1)):
                    if value <= 0:
                        continue
                    # At least 2 px: an hour with a little traffic is still
                    # an hour the VPN was on.
                    h = max(2.0, value / peak * (_PLOT_H - 5))
                    p.setBrush(QColor(colour))
                    p.drawRoundedRect(QRectF(cx - width / 2, base - h, width, h), radius, radius)

        font = QFont(self.font())
        font.setPixelSize(tokens.FS_XS)
        p.setFont(font)
        p.setPen(QColor(c.text_tertiary))
        start, _end = window(self._now)
        labels_y = QRectF(0, _PLOT_H + 2, w, _LABELS_H - 2)
        for hours, align in ((0, Qt.AlignLeft), (6, Qt.AlignHCenter), (12, Qt.AlignHCenter),
                             (18, Qt.AlignHCenter), (24, Qt.AlignRight)):
            text = tr("stats.now") if hours == 24 else time.strftime(
                "%H:00", time.localtime(start + hours * 3600))
            x = w * hours / HOURS
            rect = labels_y if hours in (0, 24) else QRectF(x - 40, labels_y.y(), 80, labels_y.height())
            p.drawText(rect, align | Qt.AlignVCenter, text)
        p.end()


def format_bytes(n: int) -> str:
    """Totals on the statistics page — "8.4 ГБ" style."""
    if n >= 1024 ** 3:
        return tr("bw.size_gb", val=f"{n / 1024 ** 3:.1f}")
    if n >= 1024 ** 2:
        return tr("bw.size_mb", val=f"{n / 1024 ** 2:.1f}")
    if n >= 1024:
        return tr("bw.size_kb", val=f"{n / 1024:.0f}")
    return tr("bw.size_b", val=n)
