"""24-hour bandwidth chart drawn in QPainter — no plotting library deps.

Pulls samples from core.bandwidth_history and renders:
  - Light grid (4 horizontal lines, 5 vertical = every 6 hours)
  - X-axis labels: 00:00, 06:00, 12:00, 18:00, now
  - Y-axis labels: 0, ¼, ½, ¾, peak (auto-scaled to data peak)
  - Two filled-area lines:
      DOWNLOAD — amber (brand color, the headline number)
      UPLOAD   — dim/muted (secondary; up is usually <<down for browsing)

Theme-aware via styles.get_active_palette() — colors recompute on each
paint event so a runtime theme switch reflects immediately.

Intentionally simple visualisation: one chart, one scale (KB/s), no
toggleable legends or per-protocol breakdown. The point is "did I use
much, when?" — not a NetFlow analyser.
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..core import bandwidth_history
from ..core.i18n import tr
from . import styles


# Chart canvas dimensions. 24-hour window fits nicely as a wide-aspect
# rectangle; height balances readability vs. fitting into the Stats page.
_CHART_W = 420
_CHART_H = 200

# Inset margins inside the widget — leave room for axis labels.
_MARGIN_L = 48   # Y-axis labels ("128 KB/s" needs ~40 px)
_MARGIN_R = 12
_MARGIN_T = 16
_MARGIN_B = 24   # X-axis time labels


def _format_rate(bytes_per_sec: float) -> str:
    """Same byte-rate formatter as xray_stats but inline so the chart
    has no cross-module pull at paint time. KB/s up to 1024, then MB/s.
    """
    if bytes_per_sec >= 1024 * 1024:
        return tr("bw.rate_mbps", val=f"{bytes_per_sec / (1024 * 1024):.1f}")
    if bytes_per_sec >= 1024:
        return tr("bw.rate_kbps", val=f"{bytes_per_sec / 1024:.0f}")
    return tr("bw.rate_bps", val=int(bytes_per_sec))


class BandwidthChartWidget(QWidget):
    """Draws the 24-hour rolling bandwidth history.

    Call refresh() after a stat-record event (or whenever the page
    becomes visible) to re-query the db and repaint. The widget does
    NOT poll on a timer itself — that'd waste cycles when the user is
    on the Home page.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(_CHART_W, _CHART_H)
        self._samples: list[bandwidth_history.Sample] = []
        self._theme_getter = lambda: "auto"

    def set_theme_getter(self, getter) -> None:
        self._theme_getter = getter

    def refresh(self) -> None:
        """Re-query the db and trigger a repaint."""
        self._samples = bandwidth_history.recent_24h()
        self.update()

    # ----- painting --------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802
        palette = styles.get_active_palette(self._theme_getter())

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Card background — same chrome style as WorldMapWidget so the
        # two visual panels in the app feel like a set.
        bg_rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QBrush(QColor(palette.SURFACE)))
        p.setPen(QPen(QColor(palette.BORDER), 1.0))
        p.drawRoundedRect(bg_rect, 10, 10)

        # Plot area — inset by axis-label margins.
        plot = QRectF(
            _MARGIN_L,
            _MARGIN_T,
            _CHART_W - _MARGIN_L - _MARGIN_R,
            _CHART_H - _MARGIN_T - _MARGIN_B,
        )

        # Empty state — when there's no data yet, show a centered note.
        # Without this the chart is just a blank card and the user wonders
        # if it broke. Triggers on fresh install / cleared history /
        # before the first 60-second sample is recorded.
        if not self._samples:
            p.setPen(QPen(QColor(palette.TEXT_DIM)))
            font = QFont()
            font.setPointSize(10)
            p.setFont(font)
            p.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                tr("bw.no_data_24h"),
            )
            p.end()
            return

        # Find the y-axis scale. Peak bytes-per-second across both up and
        # down — chart heights are normalised to this. 1 KB/s floor so a
        # quiet idle doesn't render as wild noise filling the chart.
        per_minute_peak = 0
        for s in self._samples:
            per_minute_peak = max(per_minute_peak, s.up_bytes, s.down_bytes)
        # samples are bytes-per-minute, convert to bytes-per-second for
        # the y-label (most familiar unit). Floor at 1 KB/s.
        peak_bps = max(per_minute_peak / 60.0, 1024.0)

        # Time window — chart spans [now-24h, now], not [oldest, latest].
        # Empty space on the right means "we just started recording";
        # empty on the left means "history fell off the rolling window".
        now = int(time.time())
        start = now - 24 * 60 * 60

        self._draw_grid(p, plot, palette, peak_bps, start, now)
        self._draw_series(
            p, plot, palette, start, now, peak_bps,
            attr="down_bytes",
            color=QColor(palette.ACCENT),
            alpha_fill=70,
        )
        self._draw_series(
            p, plot, palette, start, now, peak_bps,
            attr="up_bytes",
            color=QColor(palette.TEXT_DIM),
            alpha_fill=40,
        )

        p.end()

    def _draw_grid(
        self, p: QPainter, plot: QRectF, palette,
        peak_bps: float, start: int, now: int,
    ) -> None:
        grid_pen = QPen(QColor(palette.BORDER), 1.0, Qt.PenStyle.DotLine)
        p.setPen(grid_pen)

        # Y-axis: 5 horizontal lines including baseline + label each
        font = QFont()
        font.setPointSize(7)
        p.setFont(font)

        for i in range(5):
            frac = i / 4.0  # 0, 0.25, 0.5, 0.75, 1.0
            y = plot.bottom() - frac * plot.height()
            p.setPen(grid_pen)
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

            # Y-label on the left, right-aligned to plot.left()
            label = _format_rate(peak_bps * frac)
            p.setPen(QPen(QColor(palette.TEXT_DIM)))
            p.drawText(
                QRectF(0, y - 6, _MARGIN_L - 4, 12),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                label,
            )

        # X-axis: vertical grid lines every 6 hours + time labels.
        # 24h span → 4 ticks at 6h intervals (24, 18, 12, 6 hours ago).
        for hours_ago in (24, 18, 12, 6, 0):
            ts = now - hours_ago * 3600
            frac = (ts - start) / (now - start)
            x = plot.left() + frac * plot.width()

            p.setPen(grid_pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

            tm = time.localtime(ts)
            label = f"{tm.tm_hour:02d}:{tm.tm_min:02d}"
            p.setPen(QPen(QColor(palette.TEXT_DIM)))
            p.drawText(
                QRectF(x - 24, plot.bottom() + 4, 48, 14),
                int(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignTop),
                label,
            )

    def _draw_series(
        self, p: QPainter, plot: QRectF, palette,
        start: int, now: int, peak_bps: float,
        attr: str, color: QColor, alpha_fill: int,
    ) -> None:
        """Plot one of up_bytes / down_bytes as a filled area chart."""
        line_path = QPainterPath()
        fill_path = QPainterPath()

        first = True
        for s in self._samples:
            if s.ts < start or s.ts > now:
                continue
            x = plot.left() + (s.ts - start) / (now - start) * plot.width()
            bps = getattr(s, attr) / 60.0  # bytes/min → bytes/sec
            y = plot.bottom() - min(bps / peak_bps, 1.0) * plot.height()
            pt = QPointF(x, y)
            if first:
                line_path.moveTo(pt)
                fill_path.moveTo(QPointF(x, plot.bottom()))
                fill_path.lineTo(pt)
                first = False
            else:
                line_path.lineTo(pt)
                fill_path.lineTo(pt)

        if first:  # no samples in the window
            return

        # Close the fill path back to baseline so it forms a shaded area.
        last_pt = line_path.currentPosition()
        fill_path.lineTo(QPointF(last_pt.x(), plot.bottom()))
        fill_path.closeSubpath()

        # Translucent fill underneath
        fill_color = QColor(color)
        fill_color.setAlpha(alpha_fill)
        p.setBrush(QBrush(fill_color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(fill_path)

        # Solid line on top
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(color, 1.5))
        p.drawPath(line_path)


def format_bytes(n: int) -> str:
    """Stats-page totals formatter — 8.4 ГБ style. Inline here so stats_page
    doesn't have to pull xray_stats just for one helper.
    """
    if n >= 1024 ** 3:
        return tr("bw.size_gb", val=f"{n / 1024 ** 3:.1f}")
    if n >= 1024 ** 2:
        return tr("bw.size_mb", val=f"{n / 1024 ** 2:.1f}")
    if n >= 1024:
        return tr("bw.size_kb", val=f"{n / 1024:.0f}")
    return tr("bw.size_b", val=n)
