"""Statistics page — live current-rate block + 24h bandwidth history.

Lives at index 5 in MainWindow's QStackedWidget, reachable via the
chart-glyph button in the bottom nav. The page combines two views:

    1. LIVE block (top, v1.15.2):
       - "● Подключено / ○ Не подключено" status badge
       - Two big numbers — current ↓ / ↑ rate
       - Live mini-sparkline (last ~60 seconds, same buffer length as
         the home-page one — chart slides in from the right)
       - Session totals — bytes since this xray process started

       Driven by on_live_sample() / on_live_disconnected() called
       from MainWindow._poll_traffic at 1 Hz.

    2. 24h block (bottom):
       - Totals (Скачано / Отправлено) over the rolling 24h window
       - Filled-area chart reading from core/bandwidth_history
       - Refreshes on showEvent and every 60s while visible

The two blocks share a single "Очистить историю" button at the bottom
which wipes the bandwidth_history db (the live block keeps its own
in-memory sparkline buffer separate from the db).

Out of scope (still, post v1.15.2):
  - Time-range selectors (last hour / last week)
  - CSV/JSON export
  - Per-domain / per-app breakdown (would need DPI or WFP)
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import bandwidth_history
from ..core.xray_stats import format_bytes as format_bytes_session
from ..core.xray_stats import format_rate
from . import styles
from .bandwidth_chart import BandwidthChartWidget, format_bytes
from .sparkline import TrafficSparkline


# Tall enough that the line shape reads at a glance — taller than the
# home-page (44 px) sparkline because here the chart is the centerpiece
# of the live block, not a hint below text.
_LIVE_SPARKLINE_HEIGHT = 72


class StatsPage(QWidget):
    """Live + 24h stats. Header → live block → divider → 24h block → clear."""

    cleared = Signal()  # emitted after the user clears history (testability)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 16)
        outer.setSpacing(12)

        # ============ Page title =========================================

        title = QLabel("Статистика")
        title.setObjectName("h1")
        outer.addWidget(title)

        # ============ LIVE block =========================================
        # Section heading + status badge in one row — heading on the left,
        # connection state on the right (right-aligned mirrors the
        # "this is a status report" reading direction).
        live_head_row = QHBoxLayout()
        live_head_row.setContentsMargins(0, 0, 0, 0)
        live_head_row.setSpacing(8)
        live_head = QLabel("Сейчас")
        live_head.setObjectName("h2")
        live_head_row.addWidget(live_head)
        live_head_row.addStretch(1)

        # Status badge — bullet glyph + label. Color flips between
        # ACCENT (connected) and TEXT_MUTED (idle) via setStyleSheet
        # at runtime. Single QLabel, not two, so the layout doesn't
        # shift on state change.
        self._status_label = QLabel("○ Не подключено")
        self._status_label.setObjectName("liveStatus")
        live_head_row.addWidget(self._status_label)
        outer.addLayout(live_head_row)

        # Big rates row — two columns, label on top, big number below.
        # Centered glance-target: this is the headline "what's happening
        # right now" data. Empty state shows "—" so the layout doesn't
        # jump when the first sample lands.
        rates_row = QHBoxLayout()
        rates_row.setSpacing(24)
        rates_row.setContentsMargins(0, 4, 0, 0)
        rates_row.addWidget(self._build_rate_block("↓ Скачивание", "down"))
        rates_row.addWidget(self._build_rate_block("↑ Отправка", "up"))
        rates_row.addStretch(1)
        outer.addLayout(rates_row)

        # Live sparkline — same widget the home page uses but taller.
        # add_sample() is called from MainWindow._poll_traffic; reset()
        # on disconnect so the chart visibly empties.
        self.live_sparkline = TrafficSparkline()
        self.live_sparkline.setFixedHeight(_LIVE_SPARKLINE_HEIGHT)
        outer.addWidget(self.live_sparkline)

        # Session totals — small dim line. "Session" = since the last
        # connect (xray's cumulative counters reset on every spawn).
        self._session_label = QLabel("За сессию: —")
        self._session_label.setObjectName("dim")
        outer.addWidget(self._session_label)

        # ============ Divider ============================================
        # Visual separation between live "right now" and historical "24h"
        # — a 1px hairline in the BORDER color reads as a section break.
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        divider.setObjectName("sectionDivider")
        outer.addWidget(divider)

        # ============ 24h block ==========================================
        h24_head = QLabel("За последние 24 часа")
        h24_head.setObjectName("h2")
        outer.addWidget(h24_head)

        # Totals row — two big numbers side-by-side. The chart underneath
        # explains "when", these explain "how much".
        totals_row = QHBoxLayout()
        totals_row.setSpacing(24)
        self.down_label = QLabel("Скачано: —")
        self.down_label.setTextFormat(Qt.RichText)
        self.up_label = QLabel("Отправлено: —")
        self.up_label.setTextFormat(Qt.RichText)
        totals_row.addWidget(self.down_label)
        totals_row.addWidget(self.up_label)
        totals_row.addStretch(1)
        outer.addLayout(totals_row)

        # Chart centered horizontally
        chart_row = QHBoxLayout()
        chart_row.addStretch(1)
        self.chart = BandwidthChartWidget()
        chart_row.addWidget(self.chart)
        chart_row.addStretch(1)
        outer.addLayout(chart_row)

        # Note about gaps in the chart — preempts "why is there empty
        # space in the middle?" support questions.
        note = QLabel(
            "Пустые промежутки на графике — это время, когда VPN был "
            "отключён или приложение закрыто. Мы измеряем трафик только "
            "пока туннель активен."
        )
        note.setObjectName("dim")
        note.setWordWrap(True)
        outer.addWidget(note)

        outer.addStretch(1)

        # Clear-history button — destructive, danger-styled, with a
        # confirm dialog. Useful for shared/loaned laptops or privacy-
        # paranoid users who don't want to dig for the sqlite file.
        clear_row = QHBoxLayout()
        clear_row.addStretch(1)
        self.clear_btn = QPushButton("Очистить историю")
        self.clear_btn.setObjectName("danger")
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        clear_row.addWidget(self.clear_btn)
        outer.addLayout(clear_row)

        # Track live-connected state so on_live_disconnected() can no-op
        # when called repeatedly (it fires every second while idle).
        # Without this flag we'd thrash the sparkline.reset() and re-set
        # the same labels at 1 Hz for no reason.
        self._live_connected = False
        self._apply_disconnected_styling()  # initial state

        # Auto-refresh while page is visible. 60-second tick matches our
        # 24h recording cadence — no point polling faster than new data
        # lands. The timer starts in showEvent and stops in hideEvent
        # to avoid waking the db on every Home/Settings interaction.
        # The LIVE block is updated separately, via on_live_sample()
        # pushed from MainWindow._poll_traffic — it doesn't need this
        # timer.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(60_000)
        self._refresh_timer.timeout.connect(self.refresh)

    # ----- helpers ---------------------------------------------------------

    def _build_rate_block(self, caption: str, kind: str) -> QWidget:
        """One stacked label-over-big-number column for the live rates row.

        `kind` ∈ {'down', 'up'} — stored as the attribute name so
        on_live_sample / on_live_disconnected can target the right one.
        """
        col = QWidget()
        col_layout = QVBoxLayout(col)
        col_layout.setContentsMargins(0, 0, 0, 0)
        col_layout.setSpacing(2)

        cap = QLabel(caption)
        cap.setObjectName("dim")
        col_layout.addWidget(cap)

        # Big number — initially "—". Use RichText so the unit (Б/с,
        # КБ/с, МБ/с) can be dimmed relative to the digits if we want
        # to later — for v1.15.2 we just style the whole label uniformly.
        big = QLabel("—")
        big.setObjectName("liveRate")
        col_layout.addWidget(big)

        setattr(self, f"_{kind}_rate_label", big)
        return col

    def _apply_connected_styling(self) -> None:
        """Status badge → amber bullet + 'Подключено'."""
        self._status_label.setText("● Подключено")
        self._status_label.setStyleSheet(
            f"color: {styles.ACCENT}; font-size: 10pt; font-weight: 500;"
        )

    def _apply_disconnected_styling(self) -> None:
        """Status badge → muted bullet + 'Не подключено'."""
        self._status_label.setText("○ Не подключено")
        self._status_label.setStyleSheet(
            f"color: {styles.TEXT_MUTED}; font-size: 10pt;"
        )
        # Big numbers go to em-dash so the UI doesn't lie about a stale
        # last-known value when the tunnel is down.
        self._down_rate_label.setText("—")
        self._up_rate_label.setText("—")
        self._down_rate_label.setStyleSheet(
            f"color: {styles.TEXT_MUTED}; font-size: 22pt; font-weight: 600;"
        )
        self._up_rate_label.setStyleSheet(
            f"color: {styles.TEXT_MUTED}; font-size: 22pt; font-weight: 600;"
        )
        self._session_label.setText("За сессию: —")

    # ----- LIVE feed (pushed from MainWindow._poll_traffic) ----------------

    def on_live_sample(
        self,
        up_bps: float,
        down_bps: float,
        up_total: int,
        down_total: int,
    ) -> None:
        """Receive one per-second traffic sample from the connection poller.

        Idempotent — called every second while connected. Updates the
        big numbers, the sparkline buffer, and the session-totals line.
        """
        if not self._live_connected:
            self._live_connected = True
            self._apply_connected_styling()
            # Headline numbers come back at full text color (themed) once
            # we have real data to put in them.
            self._down_rate_label.setStyleSheet(
                "color: #fafafa; font-size: 22pt; font-weight: 600;"
            )
            self._up_rate_label.setStyleSheet(
                "color: #fafafa; font-size: 22pt; font-weight: 600;"
            )

        self._down_rate_label.setText(format_rate(down_bps))
        self._up_rate_label.setText(format_rate(up_bps))
        self._session_label.setText(
            f"За сессию: ↓ {format_bytes_session(down_total)}  ·  "
            f"↑ {format_bytes_session(up_total)}"
        )
        self.live_sparkline.add_sample(up_bps, down_bps)

    def on_live_disconnected(self) -> None:
        """Reset the live block when the tunnel goes down.

        Called from MainWindow._refresh_home on the disconnected branch.
        Cheap no-op when already in the disconnected state so 1 Hz polling
        doesn't thrash labels.
        """
        if not self._live_connected:
            return
        self._live_connected = False
        self._apply_disconnected_styling()
        self.live_sparkline.reset()

    # ----- 24h block -------------------------------------------------------

    def set_theme_getter(self, getter) -> None:
        """Propagate the live-theme closure to all theme-aware children."""
        self.chart.set_theme_getter(getter)
        self.live_sparkline.set_theme_getter(getter)

    def refresh(self) -> None:
        """Re-read 24h totals and tell the chart to repaint."""
        up_bytes, down_bytes = bandwidth_history.totals_24h()
        self.down_label.setText(
            f"<span style='color:#a1a1aa'>Скачано: </span>"
            f"<span style='font-size:14pt; font-weight:600'>"
            f"{format_bytes(down_bytes)}</span>"
        )
        self.up_label.setText(
            f"<span style='color:#a1a1aa'>Отправлено: </span>"
            f"<span style='font-size:14pt; font-weight:600'>"
            f"{format_bytes(up_bytes)}</span>"
        )
        self.chart.refresh()

    # ----- visibility-aware timer ------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh()
        self._refresh_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._refresh_timer.stop()

    # ----- actions ---------------------------------------------------------

    def _on_clear_clicked(self) -> None:
        reply = QMessageBox.question(
            self,
            "Очистить статистику",
            "Удалить всю историю трафика за последние 24 часа? "
            "Это действие необратимо.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        bandwidth_history.clear()
        self.refresh()
        self.cleared.emit()
