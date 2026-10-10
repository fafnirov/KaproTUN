"""Shared building blocks of the v2 interface: buttons with themed icons,
inputs, the select, the segmented switch, badges, and the dialog that opens
over the window instead of as a separate one.

Looks come from QSS generated out of tokens (styles._build_qss_v2); these
classes only pick an object name and keep their icon in step with the theme.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEasingCurve, QPoint, QRectF, QSize, Qt, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QAbstractButton,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.safe_text import http_url

from . import icons_v2, tokens
from .icons_v2 import IconLabel

_BTN_OBJECT = {"primary": "ktBtnPrimary", "secondary": "ktBtnSecondary",
               "ghost": "ktBtnGhost", "danger": "ktBtnDanger"}
_BTN_ICON_TOKEN = {"primary": "on_accent", "secondary": "text", "ghost": "text_secondary",
                   "danger": "danger_text"}


class Button(QPushButton):
    """A v2 button. `variant`: primary / secondary / ghost / danger;
    `size`: sm / md / lg; `icon`: a name from icons_v2, redrawn on theme change.
    With an icon and no text it is a square icon button."""

    def __init__(self, text: str = "", variant: str = "secondary", icon: str = "",
                 size: str = "md", tooltip: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self._variant, self._icon_name = variant, icon
        self.setObjectName(_BTN_OBJECT.get(variant, "ktBtnSecondary"))
        self.setProperty("sz", size)
        self.setProperty("ico", "true" if (icon and not text) else "false")
        self.setCursor(Qt.PointingHandCursor)
        # Focus by keyboard only: the focus ring then marks where Tab is, and
        # does not stay behind on every button that was clicked.
        self.setFocusPolicy(Qt.TabFocus)
        if tooltip:
            self.setToolTip(tooltip)
        self._render_icon()

    def set_icon(self, name: str) -> None:
        self._icon_name = name
        self._render_icon()

    def _render_icon(self) -> None:
        if not self._icon_name:
            return
        token = _BTN_ICON_TOKEN.get(self._variant, "text")
        if not self.isEnabled():
            token = "text_disabled"
        self.setIcon(icons_v2.icon(self._icon_name, tokens.ICON_SM, getattr(tokens.colors(), token)))
        self.setIconSize(QSize(tokens.ICON_SM, tokens.ICON_SM))

    def changeEvent(self, event) -> None:  # noqa: N802 — Qt override
        if event.type() in (event.Type.StyleChange, event.Type.EnabledChange):
            self._render_icon()
        super().changeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Enter presses the button that has the focus ring. Qt's own rule
        # (only Space does, Enter goes to the dialog's default button or on up
        # to the page) makes Tab-then-Enter act on something else.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)


class ElidedLabel(QLabel):
    """A single-line label that ends in "…" instead of pushing the layout."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._full = ""
        self.setTextFormat(Qt.PlainText)        # server names are untrusted text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 — Qt override
        self._full = str(text)
        self._apply()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        width = max(0, self.width())
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, width) if width else self._full
        super().setText(shown)


class Input(QLineEdit):
    """A single-line field, optionally with a leading icon."""

    def __init__(self, placeholder: str = "", icon: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktInput")
        self.setPlaceholderText(placeholder)
        self._icon_name = icon
        self._action: Optional[QAction] = None
        self._render_icon()

    def set_error(self, error: bool) -> None:
        self.setProperty("error", "true" if error else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def _render_icon(self) -> None:
        if not self._icon_name:
            return
        icon = icons_v2.icon(self._icon_name, tokens.ICON_SM, tokens.colors().text_tertiary)
        if self._action is None:
            self._action = self.addAction(icon, QLineEdit.LeadingPosition)
        else:
            self._action.setIcon(icon)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.StyleChange:
            self._render_icon()
        super().changeEvent(event)


class Select(QPushButton):
    """A drop-down drawn by us — not the native combo box, which ignores the
    theme. Shows the chosen option; opens a menu of all of them."""

    changed = Signal(int)

    def __init__(self, options: list[str], current: int = 0, icon: str = "",
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktSelect")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self._options, self._current, self._icon_name = list(options), current, icon
        row = QHBoxLayout(self)
        row.setContentsMargins(tokens.SP_3, 0, tokens.SP_2H, 0)
        row.setSpacing(tokens.SP_2)
        if icon:
            row.addWidget(IconLabel(icon, tokens.ICON_SM, "text_tertiary"))
        self._value = QLabel()
        self._value.setObjectName("ktSelectValue")
        self._value.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row.addWidget(self._value, stretch=1)
        row.addWidget(IconLabel("chevron-down", tokens.ICON_SM, "text_tertiary"))
        self.clicked.connect(self._open)
        self._sync()

    def current_index(self) -> int:
        return self._current

    def set_current(self, index: int) -> None:
        if 0 <= index < len(self._options) and index != self._current:
            self._current = index
            self._sync()
            self.changed.emit(index)

    def _on_menu_action(self, action) -> None:
        self.set_current(int(action.data()))

    def _sync(self) -> None:
        self._value.setText(self._options[self._current] if self._options else "")
        # As wide as the chosen option: next to a search field every pixel
        # the select does not need is the field's.
        self.setFixedWidth(self.sizeHint().width())

    def sizeHint(self) -> QSize:  # noqa: N802
        font = QFont(self.font())
        font.setPixelSize(tokens.FS_MD)
        text = self._options[self._current] if self._options else ""
        extra = tokens.SP_3 + tokens.SP_2H + tokens.ICON_SM + tokens.SP_2
        if self._icon_name:
            extra += tokens.ICON_SM + tokens.SP_2
        return QSize(QFontMetrics(font).horizontalAdvance(text) + extra + 6, tokens.CONTROL_H)

    def _open(self) -> None:
        menu = QMenu(self)
        menu.setObjectName("ktMenu")
        for i, label in enumerate(self._options):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(i == self._current)
            action.setData(i)
        menu.triggered.connect(self._on_menu_action)
        menu.setMinimumWidth(max(self.width(), 160))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()) + QPoint(0, tokens.SP_1))
        menu.deleteLater()


class Segmented(QFrame):
    """Two or more mutually exclusive choices in one control."""

    changed = Signal(int)

    def __init__(self, items: list[tuple[str, str]], parent: Optional[QWidget] = None):
        """`items`: (label, icon name) pairs."""
        super().__init__(parent)
        self.setObjectName("ktSeg")
        row = QHBoxLayout(self)
        row.setContentsMargins(tokens.SP_HALF, tokens.SP_HALF, tokens.SP_HALF, tokens.SP_HALF)
        row.setSpacing(tokens.SP_HALF)
        self._buttons: list[_SegItem] = []
        for i, (label, icon) in enumerate(items):
            btn = _SegItem(label, icon)
            btn.index = i
            btn.clicked.connect(self._on_item_clicked)
            row.addWidget(btn, stretch=1)
            self._buttons.append(btn)
        self._current = -1
        self.set_current(0, emit=False)

    def current_index(self) -> int:
        return self._current

    def _on_item_clicked(self) -> None:
        # Slots are bound methods, never closures over self: a closure is
        # held by the connection, holds the widget, and so keeps a widget
        # without a parent alive until the interpreter exits — where taking
        # it apart crashed on Linux.
        self.set_current(self.sender().index)

    def set_current(self, index: int, emit: bool = True) -> None:
        changed = index != self._current
        self._current = index
        for i, btn in enumerate(self._buttons):
            btn.set_selected(i == index)
        if changed and emit:
            self.changed.emit(index)


class _SegItem(QPushButton):
    def __init__(self, label: str, icon: str):
        super().__init__(label)
        self.setObjectName("ktSegItem")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self._icon_name, self._selected = icon, False
        self._render()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self._render()

    def _render(self) -> None:
        if self._icon_name:
            c = tokens.colors()
            self.setIcon(icons_v2.icon(self._icon_name, tokens.ICON_SM,
                                       c.text if self._selected else c.text_secondary))
            self.setIconSize(QSize(tokens.ICON_SM, tokens.ICON_SM))

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.StyleChange:
            self._render()
        super().changeEvent(event)


def badge(text: str, kind: str = "", dot: bool = False) -> QLabel:
    """A small pill. `kind`: "" / accent / danger / success."""
    label = QLabel(("● " if dot else "") + text)
    label.setObjectName("ktBadge")
    label.setProperty("kind", kind)
    label.setTextFormat(Qt.PlainText)
    label.setFixedHeight(tokens.BADGE_H)
    return label


def label(text: str, role: str, wrap: bool = False) -> QLabel:
    """A text label in one of the v2 roles: h1, h2, text, textSm, caption,
    label, hint, count, section."""
    w = QLabel(text)
    w.setObjectName("kt" + role[:1].upper() + role[1:])
    w.setTextFormat(Qt.PlainText)
    w.setWordWrap(wrap)
    return w


class TextArea(QPlainTextEdit):
    """A multi-line field for links and pasted subscription bodies."""

    def __init__(self, placeholder: str = "", height: int = 112,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktArea")
        self.setPlaceholderText(placeholder)
        self.setFixedHeight(height)
        self.setTabChangesFocus(True)
        self.setFrameShape(QFrame.NoFrame)


class Progress(QWidget):
    """A thin bar. Until set_fraction() is called it means "working on it, no
    idea how long"; after, it shows how far along."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self._pos = 0.0
        self._fraction: Optional[float] = None
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1200)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_tick)

    def _on_tick(self, value) -> None:
        self._pos = float(value)
        self.update()

    def set_fraction(self, fraction: Optional[float]) -> None:
        """0..1 for a known share done; None for "unknown"."""
        self._fraction = None if fraction is None else max(0.0, min(1.0, float(fraction)))
        if self._fraction is None:
            if self.isVisible():
                self._anim.start()
        else:
            self._anim.stop()
        self.update()

    def fraction(self) -> Optional[float]:
        return self._fraction

    def showEvent(self, event) -> None:  # noqa: N802
        if self._fraction is None:
            self._anim.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._anim.stop()
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = tokens.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        r = self.height() / 2
        track = QPainterPath()
        track.addRoundedRect(QRectF(self.rect()), r, r)
        p.fillPath(track, QColor(c.surface_hover))
        p.setClipPath(track)
        p.setBrush(QColor(c.accent))
        if self._fraction is None:
            bar_w = self.width() * 0.4
            x = -bar_w + (self.width() + bar_w) * self._pos
            p.drawRoundedRect(QRectF(x, 0, bar_w, self.height()), r, r)
        elif self._fraction > 0:
            p.drawRoundedRect(QRectF(0, 0, max(self.height(), self.width() * self._fraction),
                                     self.height()), r, r)
        p.end()


class LinkButton(QPushButton):
    """Text that acts: an in-app action, or — given `url` — a web address
    opened in the browser. Only plain http(s) addresses are ever opened; the
    address is shown in the tooltip, so the label cannot hide where it leads."""

    def __init__(self, text: str, url: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setObjectName("ktLink")
        self.setCursor(Qt.PointingHandCursor)
        self._url = http_url(url)
        if url:
            self.setEnabled(bool(self._url))
            self.setToolTip(self._url)
            self.clicked.connect(self._open)

    def url(self) -> str:
        return self._url

    def _open(self) -> None:
        if self._url:
            QDesktopServices.openUrl(QUrl(self._url))


class Notice(QFrame):
    """An inline message block: an icon, a bold line, then any number of
    lines, quotes and links. `kind`: neutral / warning / danger / success.
    Everything shown is plain text."""

    _ICON = {"warning": ("alert-triangle", "accent_text"), "danger": ("x-circle", "danger_text"),
             "success": ("check-circle", "success_text"), "neutral": ("info", "text_secondary")}

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktBanner")
        row = QHBoxLayout(self)
        row.setContentsMargins(tokens.SP_3, tokens.SP_2H, tokens.SP_3, tokens.SP_2H)
        row.setSpacing(tokens.SP_2H)
        self.icon = IconLabel("info", tokens.ICON_SM, "text_secondary")
        row.addWidget(self.icon, 0, Qt.AlignTop)
        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(tokens.SP_1)
        row.addLayout(self._body, stretch=1)
        self.kind = "neutral"
        self._labels: list[QLabel] = []

    def clear(self) -> None:
        self._labels = []
        while self._body.count():
            item = self._body.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(None)
            elif item.layout() is not None:
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget() is not None:
                        sub.widget().setParent(None)

    def set_content(self, kind: str, title: str, lines: tuple = ()) -> None:
        self.clear()
        self.kind = kind
        self.setProperty("kind", kind)
        name, token = self._ICON.get(kind, self._ICON["neutral"])
        self.icon.set_icon(name, token)
        self.add_line(title, "bannerTitle")
        for line in lines:
            if line:
                self.add_line(line)
        self.style().unpolish(self)
        self.style().polish(self)

    def add_line(self, text: str, role: str = "bannerText") -> QLabel:
        w = label(text, role, wrap=True)
        self._body.addWidget(w)
        self._labels.append(w)
        return w

    def add_links(self, links: list) -> list:
        """`links`: (label, url) pairs; those without a usable address are
        left out. Returns the buttons made."""
        row = QHBoxLayout()
        row.setSpacing(tokens.SP_4)
        made = [LinkButton(text, url) for text, url in links if http_url(url)]
        for btn in made:
            row.addWidget(btn)
        row.addStretch(1)
        self._body.addLayout(row)
        return made

    def texts(self) -> list[str]:
        return [w.text() for w in self._labels]


class OverlayDialog(QDialog):
    """A dialog shown over the main window: the window dims, a card sits in
    the middle. No second OS window with its own title bar, no native look.

    Build it with head(), add_widget()/add_text(), add_actions(); exec() it.
    Escape and a click on the dimmed area reject.
    """

    def __init__(self, parent: Optional[QWidget] = None, wide: bool = False):
        host = parent.window() if parent is not None else None
        if isinstance(host, OverlayDialog):
            # A question asked from inside a dialog covers the same window.
            host = host._host or host
        super().__init__(host)
        self.setObjectName("ktOverlay")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self._host = host
        self._covering = False
        self.result_key = ""
        # A click on the dimmed window around the card dismisses a question.
        # A dialog holding something the user typed sets this to False: an
        # accidental click must not throw that away.
        self.scrim_dismisses = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(tokens.SP_6, tokens.SP_6, tokens.SP_6, tokens.SP_6)
        outer.addStretch(1)
        self.card = QFrame()
        self.card.setObjectName("ktDialog")
        self.card.setFixedWidth(412 if wide else 380)
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(*tokens.colors().shadow))
        self.card.setGraphicsEffect(shadow)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

        self.body = QVBoxLayout(self.card)
        self.body.setContentsMargins(tokens.SP_5, tokens.SP_5, tokens.SP_5, tokens.SP_5)
        self.body.setSpacing(tokens.SP_4)
        self.buttons: dict[str, Button] = {}
        self._handlers: dict = {}

    # --- building ---------------------------------------------------------

    def head(self, icon: str, title: str, text: str = "", tone: str = "") -> None:
        """`tone`: "" (neutral) / accent / danger."""
        row = QHBoxLayout()
        row.setSpacing(tokens.SP_3)
        chip = QLabel()
        chip.setObjectName("ktDialogIcon")
        chip.setProperty("tone", tone)
        chip.setFixedSize(36, 36)
        token = {"accent": "accent_text", "danger": "danger_text"}.get(tone, "text_secondary")
        glyph = IconLabel(icon, tokens.ICON_MD, token, chip)
        glyph.move(8, 8)
        row.addWidget(chip, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(tokens.SP_1)
        self.head_title = label(title, "dialogTitle", wrap=True)
        col.addWidget(self.head_title)
        self.head_text = label(text, "dialogText", wrap=True)
        self.head_text.setVisible(bool(text))
        col.addWidget(self.head_text)
        row.addLayout(col, stretch=1)
        self.body.addLayout(row)

    def set_head_text(self, text: str) -> None:
        self.head_text.setText(text)
        self.head_text.setVisible(bool(text))

    def add_long_text(self, text: str, max_height: int = 220, role: str = "dialogText") -> QLabel:
        """Text that may run to many lines (an error from the engine, manual
        steps): it scrolls inside the card instead of pushing the buttons off
        the window. Selectable, so it can be copied."""
        from PySide6.QtWidgets import QScrollArea
        w = label(text, role, wrap=True)
        w.setTextInteractionFlags(Qt.TextSelectableByMouse)
        width = self.card.width() - 2 * tokens.SP_5 - tokens.SP_3
        w.setFixedWidth(width)
        # Polish first: until then the label has the application's default
        # font, not the sheet's, and its height comes out lines short.
        w.ensurePolished()
        needed = w.heightForWidth(width) + tokens.SP_1
        w.setFixedHeight(needed)
        area = QScrollArea()
        area.setObjectName("ktTextScroll")
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(w)
        area.setFixedHeight(min(needed, max_height))
        self.body.addWidget(area)
        return w

    def add_text(self, text: str) -> QLabel:
        w = label(text, "dialogText", wrap=True)
        self.body.addWidget(w)
        return w

    def add_widget(self, widget: QWidget) -> None:
        self.body.addWidget(widget)

    def add_actions(self, actions: list[tuple[str, str, str]], default: str = "",
                    icons: Optional[dict[str, str]] = None, left: tuple = (),
                    handlers: Optional[dict] = None) -> None:
        """`actions`: (key, label, variant) left to right, at the right edge;
        `left`: the same, at the left edge (a side action such as "reset").
        `default` is the key that Enter triggers and that has focus when the
        dialog opens. A button closes the dialog with its key as the answer —
        unless `handlers` has a function for the key, which is called instead
        and decides for itself."""
        row = QHBoxLayout()
        row.setSpacing(tokens.SP_2)

        self._handlers.update(handlers or {})

        def make(key: str, text: str, variant: str, size: str = "md") -> None:
            btn = Button(text, variant, icon=(icons or {}).get(key, ""), size=size)
            btn.setAutoDefault(key == default)
            btn.setDefault(key == default)
            btn.action_key = key
            btn.clicked.connect(self._on_dialog_button)
            row.addWidget(btn)
            self.buttons[key] = btn

        for key, text, variant in left:
            make(key, text, variant, size="sm")
        row.addStretch(1)
        for key, text, variant in actions:
            make(key, text, variant)
        self.body.addLayout(row)
        if default in self.buttons:
            self.buttons[default].setFocus()

    def _on_dialog_button(self) -> None:
        key = self.sender().action_key
        handler = self._handlers.get(key)
        if handler is not None:
            handler()
        else:
            self.finish(key)

    def finish(self, key: str) -> None:
        self.result_key = key
        self.accept()

    def ask(self) -> str:
        """Show modally; returns the key of the button pressed, "" if the
        dialog was dismissed."""
        self.result_key = ""
        self.exec()
        return self.result_key

    def exec(self) -> int:  # noqa: A003 — QDialog's name
        self._cover_host()
        code = super().exec()
        # One question, one dialog: do not leave it (and whatever was typed
        # or pasted into it) hanging off the window for the rest of the
        # session. What the caller reads right after exec() is still there —
        # the deletion happens when control is back in the event loop.
        self.deleteLater()
        return code

    # --- behaviour --------------------------------------------------------

    def _cover_host(self) -> None:
        # Everything under the title bar: the window can still be recognised
        # (and its close button seen) while the dialog is up. A window that is
        # not on screen (started minimized to the tray) has nothing to cover:
        # the card then stands alone, centred by the system.
        self._covering = self._host is not None and self._host.isVisible()
        if self._covering:
            area = self._host.geometry()
            area.setTop(area.top() + tokens.TITLEBAR_H)
            # In a short window give the card the room the margins would take.
            edge = tokens.SP_6 if area.height() >= 620 else tokens.SP_2
            self.layout().setContentsMargins(tokens.SP_6, edge, tokens.SP_6, edge)
            self.setGeometry(area)

    def showEvent(self, event) -> None:  # noqa: N802
        self._cover_host()
        super().showEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._covering and self.scrim_dismisses \
                and not self.card.geometry().contains(event.position().toPoint()):
            self.reject()
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        if not self._covering:
            return      # on its own (no window on screen to dim): just the card
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # Square on top (it meets the title bar), rounded where the window is.
        r = tokens.R_WINDOW
        path = QPainterPath()
        path.setFillRule(Qt.WindingFill)
        path.addRoundedRect(QRectF(self.rect()), r, r)
        path.addRect(QRectF(0, 0, self.width(), r))
        p.fillPath(path.simplified(), QColor(*tokens.colors().scrim))
        p.end()


def notify(parent: Optional[QWidget], icon: str, title: str, text: str, ok_label: str,
           tone: str = "") -> None:
    """Something the user has to see and acknowledge: one button."""
    dlg = OverlayDialog(parent)
    dlg.head(icon, title, "", tone=tone)
    if text:
        dlg.add_long_text(text)
    dlg.add_actions([("ok", ok_label, "primary")], default="ok")
    dlg.ask()


def confirm(parent: Optional[QWidget], icon: str, title: str, text: str,
            ok_label: str, cancel_label: str, danger: bool = False) -> bool:
    """A yes/no question over the window. Cancel is the default answer."""
    dlg = OverlayDialog(parent)
    dlg.head(icon, title, text, tone="danger" if danger else "accent")
    dlg.add_actions([("cancel", cancel_label, "secondary"),
                     ("ok", ok_label, "danger" if danger else "primary")], default="cancel")
    return dlg.ask() == "ok"



class Switch(QAbstractButton):
    """An on/off switch. Same surface as a check box where the settings code
    cares: isChecked / setChecked / toggled."""

    W, H, KNOB, PAD = 36, 20, 14, 3

    def __init__(self, checked: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self._pos = 1.0 if checked else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(tokens.DUR_FAST)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_slide)
        self.toggled.connect(self._slide_to)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.W, self.H)

    def _on_slide(self, value) -> None:
        self._pos = float(value)
        self.update()

    def _slide_to(self, checked: bool) -> None:
        self._anim.stop()
        if not self.isVisible():
            self._pos = 1.0 if checked else 0.0
            self.update()
            return
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() in (event.Type.StyleChange, event.Type.EnabledChange):
            self.update()
        super().changeEvent(event)

    def _colors(self) -> tuple[str, str]:
        """(track, knob) for the current state."""
        c = tokens.colors()
        on, hover, down = self.isChecked(), self.underMouse(), self.isDown()
        if not self.isEnabled():
            return (c.accent_line, c.surface) if on else (c.surface_hover, c.text_disabled)
        if on:
            return (c.accent_pressed if down else c.accent_hover if hover else c.accent), c.switch_knob
        return (c.switch_off_hover if hover else c.switch_off), (c.surface_hover if down else c.switch_knob)

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self._anim.state() != QVariantAnimation.Running:
            # The knob is where the state says, whatever route the state took
            # (a change made with signals blocked never reaches _slide_to).
            self._pos = 1.0 if self.isChecked() else 0.0
        track, knob = self._colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(track))
        p.drawRoundedRect(QRectF(self.rect()), self.H / 2, self.H / 2)
        travel = self.W - 2 * self.PAD - self.KNOB
        p.setBrush(QColor(knob))
        p.drawEllipse(QRectF(self.PAD + travel * self._pos, self.PAD, self.KNOB, self.KNOB))
        if self.hasFocus():
            pen = QColor(tokens.colors().focus)
            p.setBrush(Qt.NoBrush)
            p.setPen(pen)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                              self.H / 2, self.H / 2)
        p.end()


class SettingRow(QFrame):
    """One line of the settings: an icon, a title with a one-line hint, the
    control on the right. A row with a longer explanation unfolds it in
    place; a "link" row is a button as a whole."""

    clicked = Signal()            # link rows
    expanded_changed = Signal(bool)

    def __init__(self, icon: str, title: str, hint: str = "", full: str = "",
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktSetting")
        self.setAttribute(Qt.WA_Hover, True)
        self._is_link = False
        self._full_text = full
        self._more_icon = "chevron-down"

        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.SP_4, tokens.SP_3, tokens.SP_3, tokens.SP_3)
        col.setSpacing(0)
        main = QHBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(tokens.SP_3)
        self.icon = IconLabel(icon, tokens.ICON_MD, "text_tertiary")
        main.addWidget(self.icon)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(tokens.SP_HALF)
        self.title = ElidedLabel(title)
        self.title.setObjectName("ktSettingTitle")
        self.hint = ElidedLabel(hint)
        self.hint.setObjectName("ktSettingHint")
        self.hint.setVisible(bool(hint))
        text.addStretch(1)
        text.addWidget(self.title)
        text.addWidget(self.hint)
        text.addStretch(1)
        main.addLayout(text, stretch=1)
        self._controls = QHBoxLayout()
        self._controls.setContentsMargins(0, 0, 0, 0)
        self._controls.setSpacing(tokens.SP_1)
        main.addLayout(self._controls)
        holder = QWidget()
        holder.setLayout(main)
        holder.setMinimumHeight(tokens.CONTROL_H)
        holder.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        col.addWidget(holder)

        indent = tokens.ICON_MD + tokens.SP_3
        self.full = label(full, "settingFull", wrap=True)
        self.full.setContentsMargins(indent, tokens.SP_2, tokens.SP_8, 0)
        self.full.setVisible(False)
        col.addWidget(self.full)
        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(indent, tokens.SP_2H, 0, 0)
        self._actions.setSpacing(tokens.SP_2)
        self._actions_box = QWidget()
        self._actions_box.setLayout(self._actions)
        self._actions_box.setVisible(False)
        col.addWidget(self._actions_box)
        self._has_actions = False

        self.more: Optional[Button] = None
        if full:
            self.setCursor(Qt.PointingHandCursor)

    # --- controls -----------------------------------------------------------

    def add_control(self, widget: QWidget) -> QWidget:
        self._controls.addWidget(widget)
        return widget

    def add_switch(self, checked: bool) -> Switch:
        return self.add_control(Switch(checked))

    def add_button(self, text: str, variant: str = "secondary") -> Button:
        return self.add_control(Button(text, variant, size="sm"))

    def add_select(self, options: list, current: int) -> Select:
        return self.add_control(Select(options, current))

    def add_value(self, text: str) -> QLabel:
        return self.add_control(label(text, "settingValue"))

    def add_more(self, icon: str = "chevron-down") -> Button:
        """The button that unfolds the full explanation. `icon`: what it
        shows while folded (an "info" mark for rows that are only a note)."""
        self._more_icon = icon
        self.more = Button("", "ghost", icon=icon, size="sm")
        self.more.clicked.connect(self._toggle_expanded)
        return self.add_control(self.more)

    def add_expanded_action(self, text: str) -> Button:
        btn = Button(text, "secondary", size="sm")
        self._actions.addWidget(btn)
        self._actions.addStretch(1)
        self._has_actions = True
        return btn

    def make_link(self, external: bool = False) -> None:
        """The whole row acts: it opens something."""
        self._is_link = True
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.add_control(IconLabel("external" if external else "chevron-right",
                                   tokens.ICON_SM, "text_tertiary"))

    # --- state --------------------------------------------------------------

    def set_hint(self, text: str, tone: str = "") -> None:
        self.hint.setText(text)
        self.hint.setVisible(bool(text))
        if self.hint.property("tone") != tone:
            self.hint.setProperty("tone", tone)
            self.hint.style().unpolish(self.hint)
            self.hint.style().polish(self.hint)

    def set_full(self, text: str) -> None:
        self._full_text = text
        self.full.setText(text)
        self._fit_full()

    def _fit_full(self) -> None:
        """Give the explanation the height its text needs at this width. A
        wrapping label inside nested layouts is otherwise sized for a width
        it does not have, and loses its last lines."""
        if self.full.isHidden():
            return
        m = self.layout().contentsMargins()
        width = self.width() - m.left() - m.right()
        if width > 0:
            # Drop the previous fixed height first: heightForWidth() never
            # answers below the label's own minimum, so it could only grow.
            self.full.setMinimumHeight(0)
            self.full.setMaximumHeight(16777215)
            self.full.setFixedHeight(self.full.heightForWidth(width))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_full()

    def set_tone(self, tone: str, icon: str = "") -> None:
        """`tone`: "" / success / warning — the icon colour, and for a
        warning the row's background."""
        token = {"success": "success_text", "warning": "accent_text"}.get(tone, "text_tertiary")
        self.icon.set_icon(icon or None, token)
        self.setProperty("warning", "true" if tone == "warning" else "false")
        self._restyle()

    def set_position(self, pos: str) -> None:
        """first / middle / last / only — which corners of the card it owns.
        (The property is "slot": "pos" is taken — it is every widget's position.)"""
        self.setProperty("slot", pos)
        self._restyle()

    def is_expanded(self) -> bool:
        return not self.full.isHidden()

    def _toggle_expanded(self) -> None:
        self.set_expanded(not self.is_expanded())

    def set_expanded(self, expanded: bool) -> None:
        if not self._full_text or expanded == self.is_expanded():
            return
        self.full.setVisible(expanded)
        self._fit_full()
        self._actions_box.setVisible(expanded and self._has_actions)
        self.setProperty("expanded", "true" if expanded else "false")
        if self.more is not None:
            self.more.set_icon("chevron-up" if expanded else self._more_icon)
        self._restyle()
        self.expanded_changed.emit(expanded)

    def _restyle(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)

    def _activate(self) -> None:
        if self._is_link:
            self.clicked.emit()
        elif self._full_text:
            self.set_expanded(not self.is_expanded())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self._activate()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._is_link and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class Group(QWidget):
    """A titled card of setting rows."""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(tokens.SP_2)
        head = label(title, "sectionTitle")
        head.setContentsMargins(tokens.SP_1, 0, tokens.SP_1, 0)
        col.addWidget(head)
        self.card = QFrame()
        self.card.setObjectName("ktGroupCard")
        self.card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._rows_layout = QVBoxLayout(self.card)
        self._rows_layout.setContentsMargins(1, 1, 1, 1)
        self._rows_layout.setSpacing(0)
        col.addWidget(self.card)
        self.rows: list[SettingRow] = []

    def add(self, row: SettingRow) -> SettingRow:
        self.rows.append(row)
        self._rows_layout.addWidget(row)
        last = len(self.rows) - 1
        for i, r in enumerate(self.rows):
            r.set_position("only" if last == 0 else "first" if i == 0 else
                           "last" if i == last else "middle")
        return row


class ResultLine(QFrame):
    """One line of a check: a verdict icon, what was checked, what came of
    it. The outcome text is whatever a remote service or the system said —
    plain text."""

    _VERDICT = {"ok": ("check-circle", "success_text"), "warn": ("alert-triangle", "accent_text"),
                "fail": ("x-circle", "danger_text"), "wait": ("clock", "text_tertiary")}

    def __init__(self, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktResult")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, tokens.SP_2H, 0, tokens.SP_2H)
        row.setSpacing(tokens.SP_2H)
        self.icon = IconLabel("clock", tokens.ICON_SM, "text_tertiary")
        row.addWidget(self.icon, 0, Qt.AlignTop)
        self.name = label(name, "resultName")
        self.name.setFixedWidth(64)
        row.addWidget(self.name, 0, Qt.AlignTop)
        self.text = label("…", "resultText", wrap=True)
        row.addWidget(self.text, stretch=1)
        self.verdict = "wait"

    def set_result(self, verdict: str, text: str) -> None:
        """`verdict`: ok / warn / fail / wait."""
        self.verdict = verdict
        name, token = self._VERDICT.get(verdict, self._VERDICT["wait"])
        self.icon.set_icon(name, token)
        self.text.setText(text)
        self.text.setProperty("tone", "fail" if verdict == "fail" else "")
        self.text.style().unpolish(self.text)
        self.text.style().polish(self.text)


class Report(QPlainTextEdit):
    """Read-only monospaced text: a diagnostics dump, a list of resolvers."""

    def __init__(self, height: int = 320, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktReport")
        self.setReadOnly(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFixedHeight(height)
