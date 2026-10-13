"""The windows a user sees before the main one: the start-up splash and its
hand-over to the main window.

The splash is not decoration. Starting up does real work — undoing what a
crashed session left behind (firewall rules, routes, a system proxy pointing
at nothing), loading the servers, building the window — and one of those
steps can take seconds. A picture with "Запуск…" under it says nothing about
which step, how far, or whether the program has hung. So the splash shows the
four real steps on one bar, says so when a step takes unusually long, and —
if start-up fails — says why, in place.

BootProgress is the bar of this family of windows (splash, installer, silent
update): 4 px, a known share or "no idea how long", optionally cut into
steps.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (QByteArray, QEasingCurve, QPointF, QPropertyAnimation, QRect,
                            QRectF, QSequentialAnimationGroup, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel, QVBoxLayout,
                               QWidget)

from ..core.i18n import tr
from . import icons_v2, kit, tokens

STEP_COUNT = 4


def ease_out() -> QEasingCurve:
    """cubic-bezier(0, 0, 0.2, 1) — things arriving."""
    curve = QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.0, 0.0), QPointF(0.2, 1.0), QPointF(1.0, 1.0))
    return curve


def ease_in_out() -> QEasingCurve:
    """cubic-bezier(0.4, 0, 0.2, 1) — things moving from one place to another."""
    curve = QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.4, 0.0), QPointF(0.2, 1.0), QPointF(1.0, 1.0))
    return curve


class Mark(QWidget):
    """The KaproTUN mark: a ring and a "K" of three strokes. Drawn from the
    vector at whatever size the widget has, so it can be animated."""

    def __init__(self, size: int = tokens.MARK_LG, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFixedSize(size, size)
        self._renderer: Optional[QSvgRenderer] = None
        self._color = ""

    def paintEvent(self, _event) -> None:  # noqa: N802
        color = tokens.colors().accent
        if self._renderer is None or color != self._color:
            self._color = color
            self._renderer = QSvgRenderer(QByteArray(icons_v2.mark_svg(color)))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        self._renderer.render(p, QRectF(self.rect()))
        p.end()


class BootProgress(QWidget):
    """The 4 px bar. `set_value` for a known share (it glides there in
    160 ms), `set_indeterminate` for "working, no idea how long". With
    `segments` the bar is cut into equal steps, and `set_stalled(i)` marks
    step i as the one that is taking long: it lights up and a glint runs
    inside it, so a bar that has stopped moving does not read as a hang."""

    GLIDE_MS = 160
    GLINT_MS = 1200

    def __init__(self, segments: int = 1, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(tokens.PROGRESS_H)
        self._segments = max(1, int(segments))
        self._value = 0.0
        self._target = 0.0
        self._indeterminate = False
        self._error = False
        self._stalled: Optional[int] = None
        self._glint = 0.0

        self._glide = QVariantAnimation(self)
        self._glide.setDuration(self.GLIDE_MS)
        self._glide.setEasingCurve(ease_out())
        self._glide.valueChanged.connect(self._on_glide)

        self._glint_anim = QVariantAnimation(self)
        self._glint_anim.setStartValue(0.0)
        self._glint_anim.setEndValue(1.0)
        self._glint_anim.setDuration(self.GLINT_MS)
        self._glint_anim.setLoopCount(-1)
        self._glint_anim.valueChanged.connect(self._on_glint)

    # --- state ---------------------------------------------------------------

    def value(self) -> float:
        """The share the bar is heading for, 0..1."""
        return self._target

    def set_value(self, fraction: float, animate: bool = True) -> None:
        self._target = max(0.0, min(1.0, float(fraction)))
        self._glide.stop()
        if animate and self.isVisible() and abs(self._target - self._value) > 1e-6:
            self._glide.setStartValue(self._value)
            self._glide.setEndValue(self._target)
            self._glide.start()
        else:
            self._value = self._target
            self.update()

    def set_indeterminate(self, on: bool) -> None:
        self._indeterminate = bool(on)
        self._sync_glint()

    def is_indeterminate(self) -> bool:
        return self._indeterminate

    def set_error(self, on: bool) -> None:
        """The bar stops where it is and turns red."""
        self._error = bool(on)
        if on:
            self._glide.stop()
            self._indeterminate = False
            self._stalled = None
        self._sync_glint()

    def set_stalled(self, segment: Optional[int]) -> None:
        self._stalled = None if segment is None else max(0, min(self._segments - 1, int(segment)))
        self._sync_glint()

    def stalled(self) -> Optional[int]:
        return self._stalled

    # --- animation plumbing ----------------------------------------------------

    def _on_glide(self, value) -> None:
        self._value = float(value)
        self.update()

    def _on_glint(self, value) -> None:
        self._glint = float(value)
        self.update()

    def _sync_glint(self) -> None:
        moving = (self._indeterminate or self._stalled is not None) and self.isVisible()
        if moving and self._glint_anim.state() != QVariantAnimation.Running:
            self._glint_anim.start()
        elif not moving:
            self._glint_anim.stop()
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_glint()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._glint_anim.stop()
        super().hideEvent(event)

    # --- drawing -----------------------------------------------------------------

    def _glint_rect(self, x: float, width: float) -> QRectF:
        """A light 40 % of the track wide, entering from the left and leaving
        to the right."""
        w = width * 0.4
        return QRectF(x + (-0.4 + 1.4 * self._glint) * width, 0, w, self.height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = tokens.colors()
        w, h = float(self.width()), float(self.height())
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        track = QPainterPath()
        track.addRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        p.setClipPath(track)
        p.fillRect(QRectF(0, 0, w, h), QColor(c.surface_hover))
        fill = QColor(c.danger if self._error else c.accent)
        if self._indeterminate:
            p.fillRect(self._glint_rect(0, w), fill)
        else:
            if self._stalled is not None:
                seg_w = w / self._segments
                seg_x = seg_w * self._stalled
                p.fillRect(QRectF(seg_x, 0, seg_w, h), QColor(c.accent_line))
                p.save()
                p.setClipRect(QRectF(seg_x, 0, seg_w, h), Qt.IntersectClip)
                p.fillRect(self._glint_rect(seg_x, seg_w), fill)
                p.restore()
            p.fillRect(QRectF(0, 0, w * self._value, h), fill)
        # The gaps between steps: the window's own background showing through.
        for k in range(1, self._segments):
            p.fillRect(QRectF(w * k / self._segments - 1, 0, 2, h), QColor(c.bg))
        p.end()


class StepLine(QWidget):
    """Under (or over) a bar: what is being done, and how far — "3 / 4",
    "22 %", "7 с"."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(tokens.SP_2)
        self.dot = _Dot()
        self.dot.setVisible(False)
        row.addWidget(self.dot, 0, Qt.AlignVCenter)
        self.text = kit.ElidedLabel()
        self.text.setObjectName("ktStepText")
        row.addWidget(self.text, 1)
        self.meta = QLabel()
        self.meta.setObjectName("ktStepMeta")
        row.addWidget(self.meta, 0)
        self.setFixedHeight(16)

    def set(self, text: str, meta: str = "") -> None:
        self.text.setText(text)
        self.meta.setText(meta)

    def set_tone(self, tone: str) -> None:
        self.text.setProperty("tone", tone)
        self.text.style().unpolish(self.text)
        self.text.style().polish(self.text)


class _Dot(QWidget):
    """A small accent dot that breathes: "still at it"."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(6, 6)
        self._alpha = 1.0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1400)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_tick)

    def _on_tick(self, value) -> None:
        t = float(value)
        self._alpha = 0.35 + 0.65 * abs(1 - 2 * t)      # 1 → 0.35 → 1
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._anim.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._anim.stop()
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        color = QColor(tokens.colors().accent)
        color.setAlphaF(self._alpha)
        p.setBrush(color)
        p.drawEllipse(QRectF(self.rect()))
        p.end()


class _Card(QWidget):
    """The rounded panel every window of this family is: the window's
    background, a 1 px border, no shadow."""

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = tokens.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(c.line_strong), 1))
        p.setBrush(QColor(c.bg))
        p.drawRoundedRect(rect, tokens.R_WINDOW, tokens.R_WINDOW)
        p.end()


class SplashWindow(QWidget):
    """The start-up splash: the mark, the name, the version, and the four
    steps of starting up on one bar.

    It is a frameless, translucent window as large as the main window will
    be, with the 380 x 300 card in its middle — so that the hand-over
    (run_transition) can grow the card into the main window's frame without
    resizing a top-level window frame by frame.
    """

    log_requested = Signal()
    close_requested = Signal()
    finished = Signal()             # the hand-over has ended; the splash is gone

    STALL_AFTER_MS = 5000

    def __init__(self, version: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktSplashWindow")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setWindowTitle("KaproTUN")

        area = self._screen_area()
        height = tokens.WIN_H if area is None else max(tokens.SPLASH_H,
                                                      min(tokens.WIN_H, area.height() - 16))
        self.resize(tokens.WIN_W, height)
        if area is not None:
            self.move(area.center().x() - self.width() // 2,
                      area.center().y() - self.height() // 2)

        self._step = 0
        self._stalled_for = 0
        self._failed = False
        self._handover: Optional[QSequentialAnimationGroup] = None

        self.card = _Card(self)
        self.card.setGeometry(self._card_rect())

        self.content = QWidget(self.card)
        self.content.setObjectName("ktBootContent")
        self.content.setGeometry(0, 0, tokens.SPLASH_W, tokens.SPLASH_H)
        col = QVBoxLayout(self.content)
        col.setContentsMargins(tokens.SP_8, tokens.SP_8 + tokens.MARK_LG + tokens.SP_3,
                               tokens.SP_8, tokens.SP_6)
        col.setSpacing(0)
        self.name = QLabel("KaproTUN")
        self.name.setObjectName("ktSplashName")
        self.name.setAlignment(Qt.AlignHCenter)
        col.addWidget(self.name)
        self.version = QLabel(f"v{version}" if version else "")
        self.version.setObjectName("ktSplashVer")
        self.version.setAlignment(Qt.AlignHCenter)
        col.addWidget(self.version)
        col.addStretch(1)

        # While it runs: the bar, the step, and — if the step drags — a note.
        self.foot = QWidget()
        foot = QVBoxLayout(self.foot)
        foot.setContentsMargins(0, 0, 0, 0)
        foot.setSpacing(tokens.SP_2H)
        self.progress = BootProgress(segments=STEP_COUNT)
        foot.addWidget(self.progress)
        self.stepline = StepLine()
        foot.addWidget(self.stepline)
        self.note = QLabel()
        self.note.setObjectName("ktStepNote")
        self.note.setWordWrap(True)
        self.note.setVisible(False)
        foot.addWidget(self.note)
        col.addWidget(self.foot)

        # If it fails: what, why, and two ways on.
        self.error = QWidget()
        err = QVBoxLayout(self.error)
        err.setContentsMargins(0, 0, 0, 0)
        err.setSpacing(tokens.SP_1H)
        title_row = QHBoxLayout()
        title_row.setSpacing(tokens.SP_2)
        title_row.addWidget(icons_v2.IconLabel("x-circle", tokens.ICON_MD, "danger_text"))
        self.error_title = QLabel(tr("boot.error_title"))
        self.error_title.setObjectName("ktBootErrTitle")
        title_row.addWidget(self.error_title, 1)
        err.addLayout(title_row)
        self.error_text = QLabel()
        self.error_text.setObjectName("ktBootErrText")
        self.error_text.setWordWrap(True)
        self.error_text.setTextFormat(Qt.PlainText)
        self.error_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.error_text.setMaximumHeight(34)
        err.addWidget(self.error_text)
        err.addSpacing(tokens.SP_1)
        buttons = QHBoxLayout()
        buttons.setSpacing(tokens.SP_2)
        self.log_btn = kit.Button(tr("boot.show_log"), "secondary", icon="file")
        self.log_btn.clicked.connect(self.log_requested)
        buttons.addWidget(self.log_btn, 1)
        self.close_btn = kit.Button(tr("boot.close"), "primary")
        self.close_btn.clicked.connect(self.close_requested)
        buttons.addWidget(self.close_btn, 1)
        err.addLayout(buttons)
        self.error.setVisible(False)
        col.addWidget(self.error)

        self.mark = Mark(tokens.MARK_LG, self)
        self.mark.move(self._mark_home().topLeft())
        self.mark.raise_()

        self._fade = QGraphicsOpacityEffect(self.content)
        self._fade.setOpacity(1.0)
        self.content.setGraphicsEffect(self._fade)

        self._stall_timer = QTimer(self)
        self._stall_timer.setInterval(1000)
        self._stall_timer.timeout.connect(self._on_stall_tick)
        self._since_step = 0

    # --- geometry ------------------------------------------------------------------

    @staticmethod
    def _screen_area() -> Optional[QRect]:
        try:
            screen = QGuiApplication.primaryScreen()
            return screen.availableGeometry() if screen is not None else None
        except Exception:
            return None

    def _card_rect(self) -> QRect:
        return QRect((self.width() - tokens.SPLASH_W) // 2, (self.height() - tokens.SPLASH_H) // 2,
                     tokens.SPLASH_W, tokens.SPLASH_H)

    def _mark_home(self) -> QRect:
        card = self._card_rect()
        return QRect(card.x() + (card.width() - tokens.MARK_LG) // 2, card.y() + tokens.SP_8,
                     tokens.MARK_LG, tokens.MARK_LG)

    def window_rect_for(self, size) -> QRect:
        """Where on the screen a window of `size` goes so that its centre is
        the splash's — the hand-over grows the card into exactly that."""
        centre = self.geometry().center()
        return QRect(centre.x() - size.width() // 2 + (1 if size.width() % 2 == 0 else 0),
                     centre.y() - size.height() // 2 + (1 if size.height() % 2 == 0 else 0),
                     size.width(), size.height())

    # --- the steps --------------------------------------------------------------------

    def step(self) -> int:
        return self._step

    def set_step(self, k: int) -> None:
        """Step k of four has begun: everything before it is done."""
        if self._failed:
            return
        self._step = max(1, min(STEP_COUNT, int(k)))
        self._since_step = 0
        self.progress.set_stalled(None)
        self.progress.set_value((self._step - 1) / STEP_COUNT)
        self.stepline.dot.setVisible(False)
        self.note.setVisible(False)
        self.stepline.set(tr(f"boot.step{self._step}"), f"{self._step} / {STEP_COUNT}")
        self._stall_timer.start()

    def _on_stall_tick(self) -> None:
        self._since_step += 1
        if self._since_step * 1000 < self.STALL_AFTER_MS:
            return
        # Longer than usual. Say so, keep something moving, count the seconds:
        # a bar that just stands still looks like a hang.
        self.progress.set_stalled(self._step - 1)
        self.stepline.dot.setVisible(True)
        self.stepline.set(tr(f"boot.step{self._step}"), tr("boot.seconds", n=self._since_step))
        self.note.setText(tr("boot.long_cleanup" if self._step == 2 else "boot.long"))
        self.note.setVisible(True)

    def show_error(self, reason: str) -> None:
        """Start-up failed: the bar gives way to what happened and what to do."""
        self._failed = True
        self._stall_timer.stop()
        self.foot.setVisible(False)
        self.error_text.setText(" ".join(str(reason).split())[:300])
        self.error.setVisible(True)
        self.close_btn.setFocus()

    def failed(self) -> bool:
        return self._failed

    # --- the hand-over to the main window -----------------------------------------------

    def run_transition(self, window: QWidget) -> None:
        """Turn into `window` in 400 ms: the text fades (120), the card grows
        into the window's frame while the mark shrinks into its title bar
        (180), and the window itself shows through (100). `window` must not
        be visible yet; it is shown, at the splash's centre, as part of this.
        Emits `finished` when the splash is gone."""
        self._stall_timer.stop()
        self.progress.set_stalled(None)
        self.progress.set_value(1.0)

        target_on_screen = self.window_rect_for(window.size())
        window.move(target_on_screen.topLeft())
        target = QRect(self.mapFromGlobal(target_on_screen.topLeft()), target_on_screen.size())
        target = target.intersected(self.rect())
        self._from_card, self._to_card = self._card_rect(), target
        self._from_mark = self._mark_home()
        self._to_mark = QRect(target.x() + tokens.SP_3H,
                              target.y() + (tokens.TITLEBAR_H - tokens.MARK_SM) // 2,
                              tokens.MARK_SM, tokens.MARK_SM)
        self._window = window

        group = QSequentialAnimationGroup(self)
        fade = QPropertyAnimation(self._fade, b"opacity", self)
        fade.setDuration(tokens.DUR_OUT)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(ease_out())
        fade.finished.connect(self._after_fade)
        group.addAnimation(fade)

        grow = QVariantAnimation(self)
        grow.setDuration(tokens.DUR_GROW)
        grow.setStartValue(0.0)
        grow.setEndValue(1.0)
        grow.setEasingCurve(ease_in_out())
        grow.valueChanged.connect(self._on_grow)
        grow.finished.connect(self._after_grow)
        group.addAnimation(grow)

        reveal = QPropertyAnimation(self, b"windowOpacity", self)
        reveal.setDuration(tokens.DUR_IN)
        reveal.setStartValue(1.0)
        reveal.setEndValue(0.0)
        reveal.setEasingCurve(ease_out())
        group.addAnimation(reveal)

        group.finished.connect(self._after_handover)
        self._handover = group
        # The last stretch of the bar first (160 ms), then the hand-over.
        QTimer.singleShot(BootProgress.GLIDE_MS if self.isVisible() else 0, group.start)

    def _after_fade(self) -> None:
        self.content.setVisible(False)

    def _on_grow(self, value) -> None:
        t = float(value)
        a, b = self._from_card, self._to_card
        self.card.setGeometry(round(a.x() + (b.x() - a.x()) * t), round(a.y() + (b.y() - a.y()) * t),
                              round(a.width() + (b.width() - a.width()) * t),
                              round(a.height() + (b.height() - a.height()) * t))
        # The mark travels on an arc (a cubic through two points of the
        # design's path), shrinking from 64 to 20 px on the way.
        start, end = QPointF(self._from_mark.center()), QPointF(self._to_mark.center())
        c1 = QPointF(start.x() + (end.x() - start.x()) * 0.15, start.y() + (end.y() - start.y()) * 0.29)
        c2 = QPointF(start.x() + (end.x() - start.x()) * 0.54, start.y() + (end.y() - start.y()) * 0.53)
        u = 1 - t
        x = u ** 3 * start.x() + 3 * u * u * t * c1.x() + 3 * u * t * t * c2.x() + t ** 3 * end.x()
        y = u ** 3 * start.y() + 3 * u * u * t * c1.y() + 3 * u * t * t * c2.y() + t ** 3 * end.y()
        size = round(tokens.MARK_LG + (tokens.MARK_SM - tokens.MARK_LG) * t)
        self.mark.setFixedSize(size, size)
        self.mark.move(round(x - size / 2), round(y - size / 2))

    def _after_grow(self) -> None:
        # The card now covers exactly where the window goes: bring the window
        # up underneath, then let the splash dissolve over it.
        self._on_grow(1.0)
        self._window.show()
        self.raise_()

    def _after_handover(self) -> None:
        self.hide()
        try:
            self._window.raise_()
            self._window.activateWindow()
        except Exception:
            pass
        self.finished.emit()
        self.deleteLater()
