"""Home screen of the v2 interface ("Пульт").

Top to bottom: the server you are using (tap to change), the ring that turns
the VPN on and off with the status line under it, and one card with what the
connection is doing — IP, location, speed, direct sites.

HomePage keeps the public surface the main window already drives (set_state,
set_config, set_public_ip, set_traffic, the three signals), so the redesign is
a new face on the same wiring.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import storage
from ..core.i18n import tr
from ..core.parser import ProxyConfig
from . import connection_state as cs
from . import flags, icons_v2, tokens
from .icons_v2 import IconLabel
from .widgets import CircleConnectButton

UDP_ONLY = ("hysteria2", "tuic")


def strip_flag(name: str) -> str:
    """A server name without the leading flag emoji / country-code prefix the
    chip already shows."""
    text = str(name).lstrip()
    while text and (0x1F1E6 <= ord(text[0]) <= 0x1F1FF or text[0] in " ‍️"):
        text = text[1:]
    return text.strip() or str(name).strip()


def chip_code(cfg: ProxyConfig) -> str:
    """Two letters for the country chip. Windows has no flag emoji, so the
    flag a provider puts in a server name is shown as its ISO code instead."""
    letters = "".join(chr(ord(ch) - 0x1F1E6 + ord("A")) for ch in str(cfg.name)
                      if 0x1F1E6 <= ord(ch) <= 0x1F1FF)
    if len(letters) >= 2:
        return letters[:2]
    code = flags.country_code(cfg.name)
    if code:
        return code.upper()[:2]
    plain = "".join(ch for ch in strip_flag(cfg.name) if ch.isalnum())
    return plain[:2].upper() or "··"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def plural_domains(n: int) -> str:
    return tr("home.domains_one" if n % 10 == 1 and n % 100 != 11 else
              "home.domains_few" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else
              "home.domains_many", n=n)


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


class PingLabel(QWidget):
    """A coloured dot and the latency: green under 100 ms, amber under 250,
    red above or unreachable, hollow grey for UDP-only servers and "not
    measured yet"."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._kind, self._text = "pending", ""
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFixedHeight(16)

    def set_ping(self, ms: object, protocol: str = "", known: bool = True) -> None:
        """`ms`: latency in ms, or None for unreachable. `known=False` means
        it has not been measured yet."""
        if str(protocol).lower() in UDP_ONLY:
            self._kind, self._text = "udp", tr("picker.ping_udp")
        elif not known:
            self._kind, self._text = "pending", ""
        elif ms is None:
            self._kind, self._text = "na", tr("picker.ping_unreachable")
        else:
            ms = int(ms)
            self._kind = "good" if ms < 100 else ("mid" if ms < 250 else "bad")
            self._text = tr("picker.ping_ms", ms=ms)
        self.setVisible(bool(self._text))
        self.setFixedWidth(self.sizeHint().width())
        self.update()

    def kind(self) -> str:
        return self._kind

    def text(self) -> str:
        return self._text

    def _font(self) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(tokens.FS_SM)
        return font

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QFontMetrics
        width = tokens.DOT_SM + tokens.SP_1H + QFontMetrics(self._font()).horizontalAdvance(self._text)
        return QSize(width + 1, 16)

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = tokens.colors()
        dot = {"good": c.success, "mid": c.warning, "bad": c.danger, "na": c.danger}.get(
            self._kind, c.text_tertiary)
        text = {"na": c.danger_text, "udp": c.text_tertiary, "pending": c.text_tertiary}.get(
            self._kind, c.text_secondary)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        d = tokens.DOT_SM
        rect = QRectF(0.5, (self.height() - d) / 2, d, d)
        if self._kind == "udp":
            p.setPen(QPen(QColor(dot), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(dot))
            p.drawEllipse(rect)
        p.setPen(QColor(text))
        p.setFont(self._font())
        p.drawText(self.rect().adjusted(d + tokens.SP_1H, 0, 0, 0),
                   Qt.AlignVCenter | Qt.AlignLeft, self._text)
        p.end()


class ServerCard(QFrame):
    """The server in use, at the top of the home screen. Tap to change it."""

    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktServer")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(tokens.SERVER_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(tokens.SP_3, 0, tokens.SP_3H, 0)
        row.setSpacing(tokens.SP_3)

        self.chip = QLabel("")
        self.chip.setObjectName("ktFlag")
        self.chip.setAlignment(Qt.AlignCenter)
        self.chip.setFixedSize(tokens.CHIP, tokens.CHIP)
        self.chip.setTextFormat(Qt.PlainText)
        self._chip_icon = IconLabel("plus", tokens.ICON_MD, "accent_text", self.chip)
        self._chip_icon.move((tokens.CHIP - tokens.ICON_MD) // 2, (tokens.CHIP - tokens.ICON_MD) // 2)
        row.addWidget(self.chip)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(tokens.SP_HALF)
        self.name = ElidedLabel()
        self.name.setObjectName("ktServerName")
        self.meta = ElidedLabel()
        self.meta.setObjectName("ktServerMeta")
        body.addStretch(1)
        body.addWidget(self.name)
        body.addWidget(self.meta)
        body.addStretch(1)
        row.addLayout(body, stretch=1)

        self.ping = PingLabel()
        row.addWidget(self.ping)
        self.chevron = IconLabel("chevron-down", tokens.ICON_MD, "text_tertiary")
        row.addWidget(self.chevron)

        self.set_config(None)

    def set_config(self, cfg: Optional[ProxyConfig]) -> None:
        empty = cfg is None
        self.setProperty("empty", "true" if empty else "false")
        self.chip.setProperty("add", "true" if empty else "false")
        self._chip_icon.setVisible(empty)
        if empty:
            self.chip.setText("")
            self.name.setText(tr("home.server_none"))
            self.meta.setText(tr("home.server_none_hint"))
            self.ping.set_ping(None, known=False)
            self.chevron.set_icon("chevron-right")
        else:
            self.chip.setText(chip_code(cfg))
            self.name.setText(strip_flag(cfg.name))
            self.meta.setText(f"{cfg.protocol.upper() if len(cfg.protocol) <= 5 else cfg.protocol.capitalize()}"
                              f" · {cfg.outbound.get('server', '?')}:{cfg.outbound.get('server_port', '?')}")
            self.chevron.set_icon("chevron-down")
        for w in (self, self.chip):
            w.style().unpolish(w)
            w.style().polish(w)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class RingButton(CircleConnectButton):
    """The connect ring: a power glyph and a caption inside a circle whose
    border and glow say what state the tunnel is in. State machine and glow
    animation are CircleConnectButton's; only the look is new."""

    BURST_PEAK = 48.0
    PULSE_LOW = tokens.GLOW_LOW
    PULSE_HIGH = tokens.GLOW_CONNECTED
    CONNECTED_GLOW = tokens.GLOW_CONNECTED

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktRing")
        self.setAttribute(Qt.WA_Hover, True)
        self._size = tokens.RING
        self.setFixedSize(self._size, self._size)
        self._pulse.setStartValue(self.PULSE_LOW)
        self._pulse.setKeyValueAt(0.5, self.PULSE_HIGH)
        self._pulse.setEndValue(self.PULSE_LOW)
        self._sync_glow_color()

    def set_compact(self, compact: bool) -> None:
        self._size = tokens.RING_COMPACT if compact else tokens.RING
        self.setProperty("compact", "true" if compact else "false")
        self.setFixedSize(self._size, self._size)
        self.update()

    def _sync_glow_color(self) -> None:
        self._glow.setColor(QColor(*tokens.colors().glow))

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.StyleChange:
            self._sync_glow_color()
        super().changeEvent(event)

    def _palette(self) -> tuple[str, str, str]:
        """(border, fill, foreground) for the current state."""
        c = tokens.colors()
        state = self.property("state") or "idle"
        hover, down = self.underMouse(), self.isDown()
        if not self.isEnabled():
            return c.line, c.surface, c.text_disabled
        if state == "connected":
            return (c.accent_hover if hover else c.accent,
                    c.accent_soft if down else c.surface, c.accent_text)
        if state == "connecting":
            return c.accent_line, c.surface, c.accent_text
        if down:
            return c.accent_line, c.surface_2, c.accent_text
        if hover:
            return c.line_hover, c.surface, c.text
        return c.line_strong, c.surface, c.text_secondary

    def paintEvent(self, _event) -> None:  # noqa: N802
        border, fill, fg = self._palette()
        b = tokens.RING_BORDER
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(b / 2, b / 2, -b / 2, -b / 2)
        p.setPen(QPen(QColor(border), b))
        p.setBrush(QColor(fill))
        p.drawEllipse(rect)

        icon = tokens.ICON_XL
        gap = tokens.SP_2H
        font = QFont(self.font())
        font.setPixelSize(tokens.FS_SM)
        font.setWeight(QFont.DemiBold)
        font.setLetterSpacing(QFont.AbsoluteSpacing, tokens.LS_CAPS)
        p.setFont(font)
        text_h = p.fontMetrics().height()
        top = (self.height() - (icon + gap + text_h)) / 2
        p.drawPixmap(int((self.width() - icon) / 2), int(top), icons_v2.pixmap("power", icon, fg))
        p.setPen(QColor(fg))
        p.drawText(QRectF(0, top + icon + gap, self.width(), text_h),
                   Qt.AlignHCenter | Qt.AlignVCenter, self.text())
        p.end()


class StatusLine(QWidget):
    """Under the ring: a dot or an icon, what the tunnel is doing, and — when
    there is something to add — the session timer or the attempt count."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(tokens.SP_2)
        row.addStretch(1)
        self.dot = _Dot()
        row.addWidget(self.dot)
        self.icon = IconLabel("x-circle", tokens.ICON_MD, "danger_text")
        row.addWidget(self.icon)
        self.label = QLabel()
        self.label.setObjectName("ktStatusLabel")
        self.label.setTextFormat(Qt.PlainText)
        row.addWidget(self.label)
        self.sep = QLabel("·")
        self.sep.setObjectName("ktStatusSep")
        row.addWidget(self.sep)
        self.meta = QLabel()
        self.meta.setObjectName("ktStatusMeta")
        self.meta.setTextFormat(Qt.PlainText)
        row.addWidget(self.meta)
        row.addStretch(1)
        self.setFixedHeight(22)
        self.set_state(cs.DISCONNECTED)

    def set_state(self, state: str, detail: str = "", label: str = "") -> None:
        state = cs.normalize(state)
        tone = {cs.CONNECTED: "connected", cs.ERROR: "error", cs.KILLSWITCH_ACTIVE: "error",
                cs.DISCONNECTED: "idle"}.get(state, "busy")
        self.label.setProperty("tone", tone)
        self.label.setText(label or cs.spec(state).label)
        icon = {cs.ERROR: "x-circle", cs.KILLSWITCH_ACTIVE: "shield-alert"}.get(state)
        self.icon.setVisible(icon is not None)
        if icon:
            self.icon.set_icon(icon)
        self.dot.setVisible(icon is None)
        self.dot.set_mode("hollow" if state == cs.DISCONNECTED else "solid")
        self.meta.setProperty("mono", "true" if state == cs.CONNECTED else "false")
        self.meta.setText(detail)
        self.meta.setVisible(bool(detail))
        self.sep.setVisible(bool(detail))
        for w in (self.label, self.meta):
            w.style().unpolish(w)
            w.style().polish(w)

    def text(self) -> str:
        return self.label.text()


class _Dot(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._mode = "hollow"
        self.setFixedSize(tokens.DOT, tokens.DOT)

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = tokens.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._mode == "hollow":
            p.setPen(QPen(QColor(c.text_tertiary), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QRectF(1, 1, tokens.DOT - 2, tokens.DOT - 2))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(c.accent))
            p.drawEllipse(QRectF(0, 0, tokens.DOT, tokens.DOT))
        p.end()


class Banner(QFrame):
    """An inline notice: icon, a bold line, an explanation, and one action."""

    action_clicked = Signal()
    _ICON_TOKEN = {"warning": "accent_text", "danger": "danger_text",
                   "success": "success_text", "neutral": "text_secondary"}

    def __init__(self, kind: str = "neutral", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktBanner")
        row = QHBoxLayout(self)
        row.setContentsMargins(tokens.SP_3, tokens.SP_2H, tokens.SP_3, tokens.SP_2H)
        row.setSpacing(tokens.SP_2H)
        self.icon = IconLabel("info", tokens.ICON_SM, "text_secondary")
        row.addWidget(self.icon, 0, Qt.AlignTop)
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(tokens.SP_HALF)
        self.title = QLabel()
        self.title.setObjectName("ktBannerTitle")
        self.title.setTextFormat(Qt.PlainText)
        self.title.setWordWrap(True)
        self.text = QLabel()
        self.text.setObjectName("ktBannerText")
        self.text.setTextFormat(Qt.PlainText)
        self.text.setWordWrap(True)
        body.addWidget(self.title)
        body.addWidget(self.text)
        row.addLayout(body, stretch=1)
        self.action = QPushButton()
        self.action.setObjectName("ktBannerAction")
        self.action.setCursor(Qt.PointingHandCursor)
        self.action.setFocusPolicy(Qt.NoFocus)
        self.action.clicked.connect(self.action_clicked)
        row.addWidget(self.action, 0, Qt.AlignVCenter)
        self.set_content(kind, "info", "", "", "")

    def set_content(self, kind: str, icon: str, title: str, text: str, action: str) -> None:
        self.setProperty("kind", kind)
        self.action.setProperty("kind", kind)
        self.icon.set_icon(icon, self._ICON_TOKEN.get(kind, "text_secondary"))
        self.title.setText(title)
        self.text.setText(text)
        self.text.setVisible(bool(text))
        self.action.setText(action)
        self.action.setVisible(bool(action))
        for w in (self, self.action):
            w.style().unpolish(w)
            w.style().polish(w)


class _Cell(QFrame):
    def __init__(self, label: str, icon: str = "", icon_token: str = "text_secondary",
                 mono: bool = False, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktCell")
        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.SP_4, tokens.SP_3, tokens.SP_4, tokens.SP_3)
        col.setSpacing(tokens.SP_HALF)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(tokens.SP_1)
        if icon:
            head.addWidget(IconLabel(icon, tokens.ICON_XS, icon_token))
        caption = QLabel(label)
        caption.setObjectName("ktCellLabel")
        head.addWidget(caption)
        head.addStretch(1)
        col.addLayout(head)
        self.value = ElidedLabel("—")
        self.value.setObjectName("ktCellValue")
        self._mono = mono
        col.addWidget(self.value)
        self.set_value("")

    def set_value(self, text: str) -> None:
        live = bool(text)
        self.value.setProperty("muted", "false" if live else "true")
        self.value.setProperty("mono", "true" if (live and self._mono) else "false")
        self.value.setText(text or "—")
        self.value.style().unpolish(self.value)
        self.value.style().polish(self.value)


class _LinkRow(QFrame):
    clicked = Signal()

    def __init__(self, icon: str, text: str, last: bool = False,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktCardLink")
        self.setProperty("last", "true" if last else "false")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(tokens.ROW_H_SM)
        row = QHBoxLayout(self)
        row.setContentsMargins(tokens.SP_4, 0, tokens.SP_3H, 0)
        row.setSpacing(tokens.SP_2H)
        row.addWidget(IconLabel(icon, tokens.ICON_SM, "text_secondary"))
        self.label = QLabel(text)
        self.label.setObjectName("ktCardLinkText")
        row.addWidget(self.label, stretch=1)
        self.value = QLabel("")
        self.value.setObjectName("ktCardLinkValue")
        row.addWidget(self.value)
        row.addWidget(IconLabel("chevron-right", tokens.ICON_SM, "text_tertiary"))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class InfoCard(QFrame):
    """What the connection is doing: IP and location (when the user lets the
    client ask), live speeds, and the way into the direct-sites list."""

    sites_clicked = Signal()
    ip_setting_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktCard")
        col = QVBoxLayout(self)
        col.setContentsMargins(1, 1, 1, 1)
        col.setSpacing(0)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
        self.ip = _Cell(tr("home.ip"), mono=True)
        self.location = _Cell(tr("home.location"))
        self.down = _Cell(tr("home.download"), "arrow-down", "accent")
        self.up = _Cell(tr("home.upload"), "arrow-up", "text_secondary")
        for cell, r, c in ((self.ip, 0, 0), (self.location, 0, 1), (self.down, 1, 0), (self.up, 1, 1)):
            cell.setProperty("br", "true" if c == 0 else "false")
            cell.setProperty("bb", "true" if r == 0 else "false")
            grid.addWidget(cell, r, c)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        col.addLayout(grid)

        self.ip_hidden_row = _LinkRow("eye-off", tr("home.ip_hidden"))
        self.ip_hidden_row.clicked.connect(self.ip_setting_clicked)
        col.addWidget(self.ip_hidden_row)
        self.sites_row = _LinkRow("route", tr("home.direct_sites"), last=True)
        self.sites_row.clicked.connect(self.sites_clicked)
        col.addWidget(self.sites_row)
        self.set_ip_enabled(True)

    def set_ip_enabled(self, enabled: bool) -> None:
        """Hide the IP / location cells when the public-IP lookup is switched
        off — an always-empty cell would read as a fault."""
        self.ip.setVisible(enabled)
        self.location.setVisible(enabled)
        self.ip_hidden_row.setVisible(not enabled)
        for cell in (self.down, self.up):
            cell.setProperty("bb", "false")

    def set_sites_count(self, n: int) -> None:
        self.sites_row.value.setText(plural_domains(n))


class EmptyCard(QFrame):
    """Shown instead of the info card while there is no server at all."""

    add_clicked = Signal()
    subscription_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ktCard")
        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.SP_5, tokens.SP_6, tokens.SP_5, tokens.SP_6)
        col.setSpacing(tokens.SP_2)
        badge = QLabel()
        badge.setObjectName("ktEmptyIcon")
        badge.setFixedSize(56, 56)
        glyph = IconLabel("globe", tokens.ICON_LG, "text_secondary", badge)
        glyph.move(16, 16)
        col.addWidget(badge, 0, Qt.AlignHCenter)
        col.addSpacing(tokens.SP_1)
        title = QLabel(tr("home.empty_title"))
        title.setObjectName("ktEmptyTitle")
        title.setAlignment(Qt.AlignCenter)
        col.addWidget(title)
        text = QLabel(tr("home.empty_text"))
        text.setObjectName("ktEmptyText")
        text.setAlignment(Qt.AlignCenter)
        text.setWordWrap(True)
        col.addWidget(text)
        col.addSpacing(tokens.SP_2)
        actions = QHBoxLayout()
        actions.setSpacing(tokens.SP_2)
        actions.addStretch(1)
        self.add_btn = QPushButton(tr("home.empty_add"))
        self.add_btn.setObjectName("ktBtnPrimary")
        self.add_btn.setIcon(icons_v2.icon("plus", tokens.ICON_SM, tokens.colors().on_accent))
        self.add_btn.clicked.connect(self.add_clicked)
        actions.addWidget(self.add_btn)
        self.sub_btn = QPushButton(tr("home.empty_sub"))
        self.sub_btn.setObjectName("ktBtnSecondary")
        self.sub_btn.setIcon(icons_v2.icon("download", tokens.ICON_SM, tokens.colors().text))
        self.sub_btn.clicked.connect(self.subscription_clicked)
        actions.addWidget(self.sub_btn)
        actions.addStretch(1)
        col.addLayout(actions)


class HomePage(QWidget):
    """The home screen. Same signals and setters as the pre-v2 page."""

    connect_clicked = Signal()
    card_clicked = Signal()          # the server card → choose a server
    banner_clicked = Signal()        # subscription banner → import / renew
    sites_clicked = Signal()
    settings_clicked = Signal()      # "IP hidden" row, kill-switch notice
    logs_clicked = Signal()          # error notice
    add_clicked = Signal()           # empty state

    def __init__(self, parent: Optional[QWidget] = None, compact: bool = False):
        super().__init__(parent)
        self.setObjectName("page")
        self._compact = compact
        self._state = cs.DISCONNECTED
        self._has_config = False
        self._notice_target = ""

        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.PAGE_PAD_X, tokens.SP_3, tokens.PAGE_PAD_X,
                               tokens.SP_3 if compact else tokens.SP_5)
        col.setSpacing(0)

        self.sub_banner = Banner("warning")
        self.sub_banner.action_clicked.connect(self.banner_clicked)
        col.addWidget(self.sub_banner)
        self._sub_gap = _Gap(tokens.SP_3)
        col.addWidget(self._sub_gap)

        self.server_card = ServerCard()
        self.server_card.clicked.connect(self._on_server_card)
        col.addWidget(self.server_card)

        col.addStretch(1)

        self.circle = RingButton()
        self.circle.set_compact(compact)
        self.circle.clicked.connect(self.connect_clicked)
        col.addWidget(self.circle, 0, Qt.AlignHCenter)
        col.addSpacing(tokens.SP_3H)
        self.status_label = StatusLine()
        col.addWidget(self.status_label)

        col.addStretch(1)

        self.notice = Banner("danger")
        self.notice.action_clicked.connect(self._on_notice_action)
        col.addWidget(self.notice)
        self._notice_gap = _Gap(tokens.SP_3)
        col.addWidget(self._notice_gap)

        self.info_card = InfoCard()
        self.info_card.sites_clicked.connect(self.sites_clicked)
        self.info_card.ip_setting_clicked.connect(self.settings_clicked)
        col.addWidget(self.info_card)

        self.empty_card = EmptyCard()
        self.empty_card.add_clicked.connect(self.add_clicked)
        self.empty_card.subscription_clicked.connect(self.banner_clicked)
        col.addWidget(self.empty_card)

        self.refresh_sub_banner()
        self.refresh_sites_count()
        self.refresh_ip_setting()
        self.set_config(None)
        self.set_state(cs.DISCONNECTED)

    # --- what the main window drives ---------------------------------------

    def set_state(self, state: str, detail: str = "") -> None:
        state = cs.normalize(state)
        self._state = state
        self.circle.set_state(state)
        self.circle.setEnabled(self._has_config and cs.spec(state).button_enabled)
        label = "" if self._has_config else tr("home.status_no_servers")
        self.status_label.set_state(state if self._has_config else cs.DISCONNECTED,
                                    detail if self._has_config else "", label)
        if state != cs.CONNECTED:
            self.info_card.ip.set_value("")
            self.info_card.location.set_value("")
            self.info_card.down.set_value("")
            self.info_card.up.set_value("")
        self._refresh_notice(detail)

    def set_public_ip(self, ip: str, country_name: str, city: Optional[str] = None,
                      country_code: str = "") -> None:
        if not ip:
            self.info_card.ip.set_value("")
            self.info_card.location.set_value("")
            return
        self.info_card.ip.set_value(ip)
        place = ", ".join(p for p in (country_name, city) if p)
        self.info_card.location.set_value(place)

    def set_traffic(self, up_rate: float, down_rate: float,
                    up_total: int, down_total: int) -> None:
        from ..core.xray_stats import format_rate
        self.info_card.down.set_value(format_rate(down_rate))
        self.info_card.up.set_value(format_rate(up_rate))

    def set_config(self, cfg: Optional[ProxyConfig]) -> None:
        self._has_config = cfg is not None
        self.server_card.set_config(cfg)
        self.info_card.setVisible(self._has_config)
        self.empty_card.setVisible(not self._has_config)
        self._protocol = cfg.protocol if cfg is not None else ""
        self.set_state(self._state)

    def set_ping(self, ms: object, known: bool = True) -> None:
        """Latency of the server in use, for the chip in the server card."""
        if self._has_config:
            self.server_card.ping.set_ping(ms, self._protocol, known)

    def refresh_sites_count(self) -> None:
        self.info_card.set_sites_count(len(storage.load_sites()))

    def refresh_ip_setting(self) -> None:
        self.info_card.set_ip_enabled(bool(storage.load_settings().get("public_ip_probe", True)))

    def refresh_sub_banner(self) -> None:
        """Shown only when the subscription is running out or has expired,
        per the provider's last Subscription-Userinfo."""
        info = None
        data = storage.load_settings().get("subscription_userinfo")
        if data:
            try:
                from ..core.subscription import SubscriptionInfo
                info = SubscriptionInfo.from_dict(data)
            except Exception:
                info = None
        show = info is not None and info.is_low()
        self.sub_banner.setVisible(show)
        self._sub_gap.setVisible(show)
        if not show:
            return
        expired = info.is_expired()
        self.sub_banner.set_content(
            "danger" if expired else "warning",
            "alert-triangle" if expired else "clock",
            tr("home.sub_expired_title" if expired else "home.sub_expiring_title"),
            info.banner_text() or "",
            _cap(tr("mw.sub_renew" if expired else "mw.sub_extend")),
        )

    # --- internals -----------------------------------------------------------

    def _refresh_notice(self, detail: str) -> None:
        if self._has_config and self._state == cs.KILLSWITCH_ACTIVE:
            self._notice_target = "settings"
            self.notice.set_content("danger", "shield-alert", tr("home.notice_ks_title"),
                                    tr("home.notice_ks_text"), tr("wid.nav_settings"))
        elif self._has_config and self._state == cs.ERROR:
            self._notice_target = "logs"
            self.notice.set_content("danger", "alert-circle",
                                    detail or tr("home.notice_err_title"),
                                    tr("home.notice_err_text"), tr("home.notice_err_action"))
        else:
            self._notice_target = ""
        self.notice.setVisible(bool(self._notice_target))
        self._notice_gap.setVisible(bool(self._notice_target))

    def _on_notice_action(self) -> None:
        (self.settings_clicked if self._notice_target == "settings" else self.logs_clicked).emit()

    def _on_server_card(self) -> None:
        (self.card_clicked if self._has_config else self.add_clicked).emit()


class _Gap(QWidget):
    """A fixed vertical gap that disappears together with what it separates."""

    def __init__(self, height: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(height)
