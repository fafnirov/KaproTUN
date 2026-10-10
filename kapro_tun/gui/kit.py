"""Shared building blocks of the v2 interface: buttons with themed icons,
inputs, the select, the segmented switch, badges, and the dialog that opens
over the window instead of as a separate one.

Looks come from QSS generated out of tokens (styles._build_qss_v2); these
classes only pick an object name and keep their icon in step with the theme.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QUrl, QVariantAnimation, Signal
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
    QPlainTextEdit,
    QPushButton,
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
            action.triggered.connect(lambda _checked=False, idx=i: self.set_current(idx))
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
            btn.clicked.connect(lambda _c=False, idx=i: self.set_current(idx))
            row.addWidget(btn, stretch=1)
            self._buttons.append(btn)
        self._current = -1
        self.set_current(0, emit=False)

    def current_index(self) -> int:
        return self._current

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
    """A thin bar for "working on it, no idea how long"."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self._pos = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1200)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_tick)

    def _on_tick(self, value) -> None:
        self._pos = float(value)
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
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
        bar_w = self.width() * 0.4
        x = -bar_w + (self.width() + bar_w) * self._pos
        p.setBrush(QColor(c.accent))
        p.drawRoundedRect(QRectF(x, 0, bar_w, self.height()), r, r)
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
        super().__init__(host)
        self.setObjectName("ktOverlay")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self._host = host
        self.result_key = ""

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
        col.addWidget(label(title, "dialogTitle", wrap=True))
        if text:
            col.addWidget(label(text, "dialogText", wrap=True))
        row.addLayout(col, stretch=1)
        self.body.addLayout(row)

    def add_text(self, text: str) -> QLabel:
        w = label(text, "dialogText", wrap=True)
        self.body.addWidget(w)
        return w

    def add_widget(self, widget: QWidget) -> None:
        self.body.addWidget(widget)

    def add_actions(self, actions: list[tuple[str, str, str]], default: str = "",
                    icons: Optional[dict[str, str]] = None) -> None:
        """`actions`: (key, label, variant) left to right. `default` is the key
        that Enter triggers and that has focus when the dialog opens."""
        row = QHBoxLayout()
        row.setSpacing(tokens.SP_2)
        row.addStretch(1)
        for key, text, variant in actions:
            btn = Button(text, variant, icon=(icons or {}).get(key, ""))
            btn.setAutoDefault(key == default)
            btn.setDefault(key == default)
            btn.clicked.connect(lambda _c=False, k=key: self.finish(k))
            row.addWidget(btn)
            self.buttons[key] = btn
        self.body.addLayout(row)
        if default in self.buttons:
            self.buttons[default].setFocus()

    def finish(self, key: str) -> None:
        self.result_key = key
        self.accept()

    def ask(self) -> str:
        """Show modally; returns the key of the button pressed, "" if the
        dialog was dismissed."""
        self.result_key = ""
        self._cover_host()
        self.exec()
        # One question, one dialog: do not leave it (and, for a paste dialog,
        # the pasted links) hanging off the window for the rest of the session.
        self.deleteLater()
        return self.result_key

    # --- behaviour --------------------------------------------------------

    def _cover_host(self) -> None:
        # Everything under the title bar: the window can still be recognised
        # (and its close button seen) while the dialog is up.
        if self._host is not None:
            area = self._host.geometry()
            area.setTop(area.top() + tokens.TITLEBAR_H)
            self.setGeometry(area)

    def showEvent(self, event) -> None:  # noqa: N802
        self._cover_host()
        super().showEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.card.geometry().contains(event.position().toPoint()):
            self.reject()
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
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


def confirm(parent: Optional[QWidget], icon: str, title: str, text: str,
            ok_label: str, cancel_label: str, danger: bool = False) -> bool:
    """A yes/no question over the window. Cancel is the default answer."""
    dlg = OverlayDialog(parent)
    dlg.head(icon, title, text, tone="danger" if danger else "accent")
    dlg.add_actions([("cancel", cancel_label, "secondary"),
                     ("ok", ok_label, "danger" if danger else "primary")], default="cancel")
    return dlg.ask() == "ok"

