"""Reusable Qt widgets for KaproTUN GUI."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.parser import ProxyConfig
from . import flags, styles


class CircleConnectButton(QPushButton):
    """Large circular toggle button with three animated states.

    Each state transition starts with a one-shot "burst": the glow radius
    snaps up to 130 and eases back to the target — gives the user the
    tactile feedback of a button being hit. After the burst settles, the
    long-form animation for the new state takes over:

    - idle:       glow at 0 (no halo)
    - connecting: looping pulse 30 → 90 → 30 every 1.4 s
    - connected:  steady amber halo at 80

    Glow is driven by a Qt-Property (glow_radius) so a single
    QPropertyAnimation can drive easing curves the user can see, and the
    burst/pulse chain via QPropertyAnimation.finished hand-off.
    """

    # v1.14.2: blur radii cut by ~3× from v1.14.1 values (130/90/80/30
    # → 60/40/30/12). QGraphicsDropShadowEffect renders the blur into
    # the widget's parent QPainter pass, so a large radius extended the
    # halo 80-130 px in every direction — through the "Подключено" /
    # "Ваш IP" lines BELOW the button and over the world-map's top edge.
    # The user saw a diagonal "ray" through the map: that's actually the
    # circular outer edge of the giant blur intersecting the rectangular
    # map area, looking like a tangent line. Shrinking the radii keeps
    # the attention-grabbing pulse animation but stops it from bleeding
    # into neighbouring widgets.
    BURST_PEAK = 60.0
    BURST_DURATION_MS = 400
    PULSE_LOW = 12.0
    PULSE_HIGH = 40.0
    CONNECTED_GLOW = 30.0

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("ВКЛЮЧИТЬ", parent)
        self.setObjectName("circleBtn")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setBlurRadius(0)
        self._glow.setOffset(0, 0)
        self._glow.setColor(QColor(styles.ACCENT))
        self.setGraphicsEffect(self._glow)

        # The looping pulse used while in "connecting"
        self._pulse = QPropertyAnimation(self, b"glow_radius", self)
        self._pulse.setDuration(1400)
        self._pulse.setStartValue(self.PULSE_LOW)
        self._pulse.setKeyValueAt(0.5, self.PULSE_HIGH)
        self._pulse.setEndValue(self.PULSE_LOW)
        self._pulse.setEasingCurve(QEasingCurve.InOutSine)
        self._pulse.setLoopCount(-1)

        # One-shot burst played on every state change
        self._burst = QPropertyAnimation(self, b"glow_radius", self)
        self._burst.setDuration(self.BURST_DURATION_MS)
        self._burst.setEasingCurve(QEasingCurve.OutQuad)
        self._burst_chain_target = None  # callable to invoke when burst finishes

        self._state = "idle"

    # --- animatable Qt property ------------------------------------------

    def _get_glow_radius(self) -> float:
        return float(self._glow.blurRadius())

    def _set_glow_radius(self, value: float) -> None:
        self._glow.setBlurRadius(value)

    glow_radius = Property(float, _get_glow_radius, _set_glow_radius)

    # --- state machine ---------------------------------------------------

    def set_state(self, state: str) -> None:
        """Accepts any canonical OR legacy state (see gui.connection_state);
        maps it to the three VISUAL states this button animates
        (idle / connecting / connected) and sets the caption from the state's
        spec. Caption always updates; the animation only re-fires when the
        visual actually changes (so e.g. error vs disconnected — both 'idle'
        visually — don't re-burst)."""
        from . import connection_state as cs
        from ..core.i18n import tr  # lazy import — avoid circular

        sp = cs.spec(state)
        self.setText(tr(sp.button_text_key))
        self.setEnabled(sp.button_enabled)

        vis = sp.circle_state  # 'idle' | 'connecting' | 'connected'
        if vis == self._state:
            return
        self._state = vis

        if vis == "connected":
            self.setProperty("state", "connected")
            self._start_burst(settle_to=self.CONNECTED_GLOW, then=None)
        elif vis == "connecting":
            self.setProperty("state", "connecting")
            self._start_burst(settle_to=self.PULSE_LOW, then=self._pulse.start)
        else:
            self.setProperty("state", "idle")
            self._start_burst(settle_to=0.0, then=None)

        # Re-polish so QSS property selectors update (border colors, etc.)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_compact(self, compact: bool) -> None:
        """Toggle the compact-preset size (smaller hero circle). Driven by a
        QSS property selector, so just flip the property + re-polish."""
        self.setProperty("compact", "true" if compact else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def _start_burst(self, settle_to: float, then) -> None:
        """Quick attention-grabbing pulse, then optionally chain another anim.

        `then` is a zero-arg callable invoked when the burst's finished
        signal fires — used to start the looping pulse after the burst
        settles, so the two animations don't fight over glow_radius.
        """
        self._pulse.stop()
        self._burst.stop()
        # Connect-once: hold a single chain handler in a slot so we can
        # disconnect cleanly without RuntimeWarnings about "no connection".
        if not hasattr(self, "_burst_chain_connected"):
            self._burst.finished.connect(self._on_burst_finished)
            self._burst_chain_connected = True
        self._burst_chain_target = then

        self._burst.setStartValue(self._glow.blurRadius())
        self._burst.setKeyValueAt(0.3, self.BURST_PEAK)
        self._burst.setEndValue(settle_to)
        self._burst.start()

    def _on_burst_finished(self) -> None:
        target, self._burst_chain_target = self._burst_chain_target, None
        if target is not None:
            target()


class ConfigCard(QFrame):
    """Bottom card on the home screen showing the active/selected config.

    Click anywhere on the card to open the configs picker.
    """

    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("configCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(6)

        self.title = QLabel("Конфиг не выбран")
        self.title.setObjectName("cardTitle")
        self.title.setWordWrap(True)
        outer.addWidget(self.title)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        self.badge = QLabel("—")
        self.badge.setObjectName("cardBadge")
        self.sub = QLabel("Нажми, чтобы выбрать или добавить конфиг")
        self.sub.setObjectName("cardSub")
        bottom_row.addWidget(self.badge)
        bottom_row.addWidget(self.sub, stretch=1)
        chevron = QLabel("▾")
        chevron.setObjectName("dim")
        bottom_row.addWidget(chevron)
        outer.addLayout(bottom_row)

    def set_config(self, cfg: Optional[ProxyConfig]) -> None:
        if cfg is None:
            self.title.setText("Конфиг не выбран")
            self.badge.setText("—")
            self.sub.setText("Нажми, чтобы добавить конфиг")
            return
        self.title.setText(flags.prefix_with_flag(cfg))
        self.badge.setText(cfg.protocol.upper())
        server = cfg.outbound.get("server", "?")
        port = cfg.outbound.get("server_port", "?")
        self.sub.setText(f"{server}:{port}")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class IconButton(QPushButton):
    """Square text-icon button used in the bottom nav bar."""

    def __init__(self, glyph: str, tooltip: str = "", parent: Optional[QWidget] = None):
        super().__init__(glyph, parent)
        self.setObjectName("iconBtn")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        if tooltip:
            self.setToolTip(tooltip)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class NavBar(QWidget):
    """Bottom navigation: Home / Stats / Settings / Add.

    All glyphs use the U+FE0E text-style variation selector so Windows
    doesn't render them as color emoji (the gear was coming through
    bold and purple-grey, the others as thin outlines).

    v1.15.0 added the Stats button (chart glyph) between Home and
    Settings — that's the natural left-to-right reading order: home
    state → look at history → tweak settings → add new config.
    """

    home_clicked = Signal()
    stats_clicked = Signal()
    settings_clicked = Signal()
    add_clicked = Signal()

    # v1.15.1 — switched STATS glyph from 📊 (emoji, rendered colored
    # and at a different metrics by Segoe UI Emoji regardless of the
    # U+FE0E text-variation selector) to ▤ U+25A4 (SQUARE WITH
    # HORIZONTAL FILL — a box-drawing char from the same family as
    # ⌂ and ⚙, rendered monochrome and at matching cap-height by
    # Segoe UI Symbol). Visually reads as a horizontal bar-chart,
    # which fits the "Статистика" tab semantics.
    HOME_GLYPH = "⌂"      # U+2302 HOUSE
    STATS_GLYPH = "▤"     # U+25A4 SQUARE WITH HORIZONTAL FILL — bar-chart-ish
    SETTINGS_GLYPH = "⚙"  # U+2699 GEAR
    ADD_GLYPH = "+"       # plain ASCII plus, scales better than ＋

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(0)

        self.btn_home = IconButton(self.HOME_GLYPH, "Главная")
        self.btn_stats = IconButton(self.STATS_GLYPH, "Статистика")
        self.btn_settings = IconButton(self.SETTINGS_GLYPH, "Настройки")
        self.btn_add = IconButton(self.ADD_GLYPH, "Добавить конфиг")

        self.btn_home.clicked.connect(self.home_clicked)
        self.btn_stats.clicked.connect(self.stats_clicked)
        self.btn_settings.clicked.connect(self.settings_clicked)
        self.btn_add.clicked.connect(self.add_clicked)

        # Four equal columns — perfect visual alignment regardless of
        # window width. Stretch wrappers under each button centre them.
        for btn in (self.btn_home, self.btn_stats, self.btn_settings, self.btn_add):
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.addStretch(1)
            cell_layout.addWidget(btn)
            cell_layout.addStretch(1)
            layout.addWidget(cell, stretch=1)

    def set_active(self, name: str) -> None:
        """name ∈ {'home', 'stats', 'settings', 'add'}"""
        self.btn_home.set_active(name == "home")
        self.btn_stats.set_active(name == "stats")
        self.btn_settings.set_active(name == "settings")
        self.btn_add.set_active(name == "add")


class StatusLabel(QLabel):
    """Status text under the connect button. Color reflects connection state."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Не подключено", parent)
        self.setAlignment(Qt.AlignCenter)
        self.setObjectName("muted")

    def set_state(self, state: str, detail: str = "") -> None:
        """Drive text + colour from the single connection_state spec, so the
        status line is never out of sync with the button or tray. Prepends a
        per-state indicator glyph (○ ◌ ● ✕ ■). `detail` is the session timer
        for CONNECTED, or a short error reason otherwise."""
        from . import connection_state as cs
        sp = cs.spec(state)
        if detail:
            sep = "·" if sp.state == cs.CONNECTED else "—"
            text = f"{sp.glyph}  {sp.label} {sep} {detail}"
        else:
            text = f"{sp.glyph}  {sp.label}"
        self.setText(text)
        color = getattr(styles, sp.accent, styles.TEXT_MUTED)
        weight = 600 if sp.state in (cs.CONNECTED, cs.ERROR, cs.KILLSWITCH_ACTIVE) else 400
        self.setStyleSheet(f"color: {color}; font-size: 10pt; font-weight: {weight};")


class TrafficLegend(QWidget):
    """Live up/down rates with colour-matched arrows (doubles as the home
    sparkline's legend) + a session-total caption.

    The value labels have a fixed minimum width and are left-aligned next to
    their arrow, so the numbers don't shift the layout as they change — that's
    the 'no width jitter' requirement. Arrows are coloured via QSS
    (#graphUp / #graphDown) to match the two graph lines."""

    _VALUE_W = 92  # reserves room for the widest realistic rate ("1023.9 КБ/с")

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addStretch(1)
        self.down_arrow = QLabel("↓")
        self.down_arrow.setObjectName("graphDown")
        self.down_value = QLabel("—")
        self.down_value.setObjectName("graphValue")
        self.down_value.setMinimumWidth(self._VALUE_W)
        self.down_value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.up_arrow = QLabel("↑")
        self.up_arrow.setObjectName("graphUp")
        self.up_value = QLabel("—")
        self.up_value.setObjectName("graphValue")
        self.up_value.setMinimumWidth(self._VALUE_W)
        self.up_value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for wdg in (self.down_arrow, self.down_value, self.up_arrow, self.up_value):
            row.addWidget(wdg)
        row.addStretch(1)
        v.addLayout(row)

        self.caption = QLabel("")
        self.caption.setObjectName("caption")
        self.caption.setAlignment(Qt.AlignCenter)
        v.addWidget(self.caption)

    def set_values(self, up_rate: float, down_rate: float,
                   up_total: int, down_total: int) -> None:
        from ..core.xray_stats import format_bytes, format_rate
        self.down_value.setText(format_rate(down_rate))
        self.up_value.setText(format_rate(up_rate))
        self.caption.setText(
            f"за сессию: ↓ {format_bytes(down_total)}  ·  ↑ {format_bytes(up_total)}"
        )

    def clear(self) -> None:
        self.down_value.setText("—")
        self.up_value.setText("—")
        self.caption.setText("")
