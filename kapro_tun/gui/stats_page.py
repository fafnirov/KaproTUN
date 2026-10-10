"""The Statistics tab: what the tunnel is carrying right now, and how much it
carried over the last 24 hours.

Two cards.

"Now": a connected / not connected badge, the current download and upload
rates, a graph of the last minute, and the totals of this session. Fed by the
main window once a second — the badge by set_live_connected(), the numbers
by on_live_sample(). The two are separate on purpose: the badge must follow
the tunnel even while the first traffic sample has not arrived yet.

"24 hours": totals and an hour-by-hour chart read from core.bandwidth_history,
refreshed when the page is shown and once a minute while it stays open.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import bandwidth_history
from ..core.i18n import tr
from ..core.xray_stats import format_bytes as format_bytes_session
from ..core.xray_stats import format_rate
from . import kit, tokens
from .bandwidth_chart import BandwidthChartWidget, format_bytes
from .icons_v2 import IconLabel
from .sparkline import TrafficSparkline

_SPARK_H = 56


class _LegendDot(QWidget):
    """A small coloured dot for the chart legend."""

    def __init__(self, token: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._token = token
        self.setFixedSize(tokens.DOT_SM, tokens.DOT_SM)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(getattr(tokens.colors(), self._token)))
        p.drawEllipse(self.rect())
        p.end()


class StatsPage(QWidget):
    cleared = Signal()   # after the user cleared the history

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self._live_connected = False

        wrapper = QVBoxLayout(self)
        wrapper.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("ktPageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrapper.addWidget(scroll)
        content = QWidget()
        content.setObjectName("page")
        scroll.setWidget(content)
        col = QVBoxLayout(content)
        col.setContentsMargins(tokens.PAGE_PAD_X, tokens.PAGE_PAD_Y,
                               tokens.PAGE_PAD_X, tokens.PAGE_PAD_Y)
        col.setSpacing(tokens.SP_3)
        col.addWidget(kit.label(tr("stats.title"), "h1"))
        col.addWidget(self._build_live_card())
        col.addWidget(self._build_day_card())
        col.addStretch(1)

        self._apply_disconnected()

        # Once a minute while visible — the cadence new samples are recorded at.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(60_000)
        self._refresh_timer.timeout.connect(self.refresh)

    # --- building -----------------------------------------------------------

    @staticmethod
    def _card() -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("ktCard")
        col = QVBoxLayout(card)
        col.setContentsMargins(tokens.SP_4, tokens.SP_4, tokens.SP_4, tokens.SP_4)
        col.setSpacing(tokens.SP_3)
        return card, col

    def _metric(self, icon: str, icon_token: str, caption: str) -> tuple[QVBoxLayout, QLabel]:
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(tokens.SP_HALF)
        head = QHBoxLayout()
        head.setSpacing(tokens.SP_1)
        head.addWidget(IconLabel(icon, tokens.ICON_XS, icon_token))
        cap = QLabel(caption)
        cap.setObjectName("ktCellLabel")
        head.addWidget(cap)
        head.addStretch(1)
        box.addLayout(head)
        value = QLabel("—")
        value.setObjectName("ktMetric")
        value.setTextFormat(Qt.PlainText)
        box.addWidget(value)
        return box, value

    def _build_live_card(self) -> QFrame:
        card, col = self._card()
        head = QHBoxLayout()
        head.addWidget(kit.label(tr("stats.live_head"), "h2"), stretch=1)
        self._status_label = kit.badge("")
        head.addWidget(self._status_label)
        col.addLayout(head)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(tokens.SP_4)
        down, self._down_rate_label = self._metric("arrow-down", "accent_text", tr("stats.download"))
        up, self._up_rate_label = self._metric("arrow-up", "text_tertiary", tr("stats.upload"))
        grid.addLayout(down, 0, 0)
        grid.addLayout(up, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        col.addLayout(grid)

        self.live_sparkline = TrafficSparkline()
        self.live_sparkline.setFixedHeight(_SPARK_H)
        col.addWidget(self.live_sparkline)
        self._spark_placeholder = kit.label(tr("stats.spark_empty"), "placeholder")
        self._spark_placeholder.setAlignment(Qt.AlignCenter)
        self._spark_placeholder.setFixedHeight(_SPARK_H)
        col.addWidget(self._spark_placeholder)

        self._session_label = kit.label(tr("stats.session_empty"), "caption")
        col.addWidget(self._session_label)
        return card

    def _build_day_card(self) -> QFrame:
        card, col = self._card()
        head = QHBoxLayout()
        head.addWidget(kit.label(tr("stats.h24_short"), "h2"), stretch=1)
        self.clear_btn = kit.Button(tr("stats.clear"), "ghost", icon="trash", size="sm")
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        head.addWidget(self.clear_btn)
        col.addLayout(head)

        totals = QHBoxLayout()
        totals.setSpacing(tokens.SP_1H)
        totals.addWidget(kit.label(tr("stats.down_total"), "textSm"))
        self.down_label = kit.label(format_bytes(0), "strong")
        totals.addWidget(self.down_label)
        totals.addSpacing(tokens.SP_5)
        totals.addWidget(kit.label(tr("stats.up_total"), "textSm"))
        self.up_label = kit.label(format_bytes(0), "strong")
        totals.addWidget(self.up_label)
        totals.addStretch(1)
        col.addLayout(totals)

        self.chart = BandwidthChartWidget()
        col.addWidget(self.chart)
        self._legend = QWidget()
        legend = QHBoxLayout(self._legend)
        legend.setContentsMargins(0, 0, 0, 0)
        legend.setSpacing(tokens.SP_1)
        legend.addWidget(_LegendDot("accent"))
        legend.addWidget(kit.label(tr("stats.legend_down"), "caption"))
        legend.addSpacing(tokens.SP_3)
        legend.addWidget(_LegendDot("chart_up"))
        legend.addWidget(kit.label(tr("stats.legend_up"), "caption"))
        legend.addStretch(1)
        legend.addWidget(kit.label(tr("stats.legend_gap"), "caption"))
        col.addWidget(self._legend)

        self._empty = QWidget()
        empty = QVBoxLayout(self._empty)
        empty.setContentsMargins(tokens.SP_4, tokens.SP_4, tokens.SP_4, tokens.SP_2)
        empty.setSpacing(tokens.SP_2)
        mark = QLabel()
        mark.setObjectName("ktEmptyIcon")
        mark.setFixedSize(56, 56)
        IconLabel("chart", tokens.ICON_LG, "text_secondary", mark).move(16, 16)
        empty.addWidget(mark, 0, Qt.AlignHCenter)
        title = kit.label(tr("stats.empty_title"), "emptyTitle")
        title.setAlignment(Qt.AlignCenter)
        empty.addWidget(title)
        text = kit.label(tr("stats.empty_text"), "emptyText", wrap=True)
        text.setAlignment(Qt.AlignCenter)
        empty.addWidget(text)
        col.addWidget(self._empty)
        return card

    # --- "Now": pushed by the main window ----------------------------------

    def _set_badge(self, connected: bool) -> None:
        self._status_label.setText(("● " if connected else "○ ")
                                   + tr("stats.connected" if connected else "stats.disconnected"))
        self._status_label.setProperty("kind", "accent" if connected else "")
        for w in (self._status_label, self._down_rate_label, self._up_rate_label):
            w.style().unpolish(w)
            w.style().polish(w)

    def _apply_disconnected(self) -> None:
        # A dash, not the last value: nothing is flowing, and a stale number
        # would say otherwise.
        for value in (self._down_rate_label, self._up_rate_label):
            value.setText("—")
            value.setProperty("muted", "true")
        self._session_label.setText(tr("stats.session_empty"))
        self.live_sparkline.setVisible(False)
        self._spark_placeholder.setVisible(True)
        self._set_badge(False)

    def set_live_connected(self, connected: bool) -> None:
        """The badge and the base look of the numbers. Called every second
        with the tunnel's real state; does nothing when that has not changed."""
        if connected == self._live_connected:
            return
        self._live_connected = connected
        if not connected:
            self._apply_disconnected()
            self.live_sparkline.reset()
            return
        # Zeroes rather than dashes until the first sample: the tunnel is
        # up, there is just nothing measured yet.
        for value in (self._down_rate_label, self._up_rate_label):
            value.setText(format_rate(0))
            value.setProperty("muted", "false")
        self._session_label.setText(tr("stats.session_counting"))
        self._spark_placeholder.setVisible(False)
        self.live_sparkline.setVisible(True)
        self._set_badge(True)

    def on_live_sample(self, up_bps: float, down_bps: float,
                       up_total: int, down_total: int) -> None:
        """One per-second traffic sample."""
        if not self._live_connected:
            self.set_live_connected(True)     # traffic is itself proof of a tunnel
        self._down_rate_label.setText(format_rate(down_bps))
        self._up_rate_label.setText(format_rate(up_bps))
        self._session_label.setText(tr("stats.session_totals",
                                       down=format_bytes_session(down_total),
                                       up=format_bytes_session(up_total)))
        self.live_sparkline.add_sample(up_bps, down_bps)

    def on_live_disconnected(self) -> None:
        self.set_live_connected(False)

    # --- "24 hours" ---------------------------------------------------------

    def set_theme_getter(self, _getter) -> None:
        """Kept for callers from before the tokens; the theme is read from
        them now."""

    def refresh(self) -> None:
        up_bytes, down_bytes = bandwidth_history.totals_24h()
        self.down_label.setText(format_bytes(down_bytes))
        self.up_label.setText(format_bytes(up_bytes))
        self.chart.refresh()
        has = self.chart.has_data()
        self.chart.setVisible(has)
        self._legend.setVisible(has)
        self._empty.setVisible(not has)
        # From the totals, not from the chart: rows the chart does not draw
        # (dated ahead of a clock that was set back) can still be cleared.
        self.clear_btn.setEnabled(has or bool(up_bytes or down_bytes))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()
        self._refresh_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._refresh_timer.stop()

    def _on_clear_clicked(self) -> None:
        if not kit.confirm(self, "trash", tr("stats.clear_title"), tr("stats.clear_dialog_body"),
                           ok_label=tr("stats.clear"), cancel_label=tr("srv.cancel"), danger=True):
            return
        bandwidth_history.clear()
        self.refresh()
        self.cleared.emit()
