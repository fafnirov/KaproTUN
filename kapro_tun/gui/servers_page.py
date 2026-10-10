"""The Servers tab of the v2 interface: every saved server in one list, with
search, sorting and latency, and the actions on the one you picked.

The page is a view. The main window owns the list, the active server and the
measured pings, and hands them over with set_configs() / set_pings(); the page
answers with signals (connect, delete, add, refresh) and changes nothing on
disk itself.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n import tr
from ..core.parser import ProxyConfig
from . import flags, kit, tokens, world_map
from .home_v2 import ElidedLabel, EmptyCard, PingLabel, chip_code, server_meta, strip_flag
from .icons_v2 import IconLabel
from .merge_prompt import one_line

SORT_SPEED, SORT_NAME, SORT_COUNTRY, SORT_PROTO = range(4)
_PENDING = "pending"


def sort_labels() -> list[str]:
    return [tr("srv.sort_speed"), tr("srv.sort_name"), tr("srv.sort_country"), tr("srv.sort_proto")]


def name_key(cfg: ProxyConfig) -> str:
    """Sort key for a name: without the flag, so "🇳🇱 Нидерланды" sorts under
    "н" and not by the emoji's code point."""
    return strip_flag(cfg.name).casefold()


def country_key(cfg: ProxyConfig) -> tuple:
    try:
        code = flags.country_code(cfg.name) or world_map.country_code_from_flag(cfg.name) or ""
    except Exception:
        code = ""
    return (0, code) if code else (1, "")


def speed_rank(cfg: ProxyConfig, pings: dict) -> tuple:
    """Reachable servers by latency, then the unmeasurable (UDP-only, not
    pinged yet), then the unreachable."""
    value = pings.get(cfg.name, _PENDING)
    if isinstance(value, int) and value >= 0:
        return (0, value)
    if value == -1 or value == _PENDING:
        return (1, 0)
    return (2, 0)


def sort_configs(configs: list, mode: int, pings: dict) -> list:
    out = list(configs)
    if mode == SORT_NAME:
        out.sort(key=name_key)
    elif mode == SORT_PROTO:
        out.sort(key=lambda c: (c.protocol.lower(), name_key(c)))
    elif mode == SORT_COUNTRY:
        out.sort(key=lambda c: (country_key(c), name_key(c)))
    else:
        out.sort(key=lambda c: (speed_rank(c, pings), name_key(c)))
    return out


def matches(cfg: ProxyConfig, query: str) -> bool:
    """Case-insensitive substring match over name, host, port and protocol."""
    hay = " ".join([cfg.name, str(cfg.outbound.get("server") or ""),
                    str(cfg.outbound.get("server_port") or ""), cfg.protocol]).casefold()
    return query.casefold() in hay


def _signature(configs: list) -> tuple:
    return tuple((c.name, c.protocol, str(c.outbound.get("server")),
                  str(c.outbound.get("server_port"))) for c in configs)


class _NameLabel(ElidedLabel):
    """A name that takes the width it needs and no more, so a badge can sit
    right after it — and gives way with "…" when the row is too narrow."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("", parent)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self.font())
        return QSize(fm.horizontalAdvance(self.full_text()) + 2, fm.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, QFontMetrics(self.font()).height())


class ServerRow(QFrame):
    """One server: country chip, name, protocol and address, latency."""

    clicked = Signal(object)      # the row
    activated = Signal(object)    # double click

    def __init__(self, cfg: ProxyConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg = cfg
        self.setObjectName("ktSrow")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(tokens.ROW_H)

        row = QHBoxLayout(self)
        row.setContentsMargins(tokens.SP_3, 0, tokens.SP_3, 0)
        row.setSpacing(tokens.SP_3)

        self.chip = QLabel(chip_code(cfg))
        self.chip.setObjectName("ktFlag")
        self.chip.setProperty("sm", "true")
        self.chip.setAlignment(Qt.AlignCenter)
        self.chip.setTextFormat(Qt.PlainText)
        self.chip.setFixedSize(tokens.CHIP_SM, tokens.CHIP_SM)
        row.addWidget(self.chip)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(tokens.SP_HALF)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(tokens.SP_2)
        self.name = _NameLabel()
        self.name.setObjectName("ktSrowName")
        self.name.setText(one_line(strip_flag(cfg.name), 80))
        top.addWidget(self.name)
        self.badge = kit.badge(tr("srv.active"), "accent", dot=True)
        self.badge.setVisible(False)
        top.addWidget(self.badge)
        top.addStretch(1)
        self.meta = ElidedLabel(server_meta(cfg))
        self.meta.setObjectName("ktSrowMeta")
        body.addStretch(1)
        body.addLayout(top)
        body.addWidget(self.meta)
        body.addStretch(1)
        row.addLayout(body, stretch=1)

        self.ping = PingLabel()
        row.addWidget(self.ping)
        self.check = IconLabel("check", tokens.ICON_MD, "accent_text")
        self.check.setVisible(False)
        row.addWidget(self.check)
        self.set_ping(_PENDING)

    def _restyle(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.check.setVisible(selected)
        self._restyle()

    def is_selected(self) -> bool:
        return self.property("selected") == "true"

    def set_active(self, active: bool) -> None:
        self.badge.setVisible(active)

    def set_last(self, last: bool) -> None:
        if (self.property("last") == "true") != last:
            self.setProperty("last", "true" if last else "false")
            self._restyle()

    def set_ping(self, value: object) -> None:
        """`value`: latency in ms, None = unreachable, -1 = cannot be measured
        over TCP, "pending" = not measured yet."""
        if value == _PENDING:
            self.ping.set_ping(None, self.cfg.protocol, known=False)
        elif value == -1:
            self.ping.set_ping(None, "hysteria2")
        else:
            self.ping.set_ping(value, self.cfg.protocol)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.activated.emit(self)


class _ListFrame(QWidget):
    """Drawn over the list: the rounded border, and the page colour outside
    it — so rows under the corners look cut to the card's shape."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = tokens.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        card = QPainterPath()
        card.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                            tokens.R_LG, tokens.R_LG)
        outside = QPainterPath()
        outside.addRect(QRectF(self.rect()))
        p.fillPath(outside.subtracted(card), QColor(c.bg))
        p.setPen(QPen(QColor(c.line), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(card)
        p.end()


class ServerList(QFrame):
    """The card with the rows. As tall as its rows while they fit, scrolling
    once they do not."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("ktListScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("ktListBody")
        self._col = QVBoxLayout(body)
        self._col.setContentsMargins(0, 0, 0, 0)
        self._col.setSpacing(0)
        self.not_found = QLabel(tr("srv.not_found"))
        self.not_found.setObjectName("ktListEmpty")
        self.not_found.setAlignment(Qt.AlignCenter)
        self.not_found.setFixedHeight(tokens.ROW_H)
        self.not_found.setVisible(False)
        self._col.addWidget(self.not_found)
        self._col.addStretch(1)
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll)
        self._frame = _ListFrame(self)
        self.rows: list[ServerRow] = []

    def set_rows(self, rows: list) -> None:
        for old in self.rows:
            self._col.removeWidget(old)
            old.setParent(None)
            old.deleteLater()
        self.rows = list(rows)
        for i, row in enumerate(self.rows):
            self._col.insertWidget(i, row)

    def fit(self) -> None:
        """Size the card to the rows shown and mark the last of them."""
        shown = [r for r in self.rows if not r.isHidden()]
        for row in shown:
            row.set_last(row is shown[-1])
        self.not_found.setVisible(bool(self.rows) and not shown)
        self.setMaximumHeight(max(1, len(shown)) * tokens.ROW_H + 2)

    def ensure_visible(self, row: ServerRow) -> None:
        self.scroll.ensureWidgetVisible(row, 0, tokens.SP_2)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._frame.setGeometry(self.rect())
        self._frame.raise_()


class ServersPage(QWidget):
    connect_requested = Signal(object)   # ProxyConfig: use this server now
    delete_requested = Signal(object)    # ProxyConfig, already confirmed
    add_clicked = Signal()
    subscription_clicked = Signal()
    ping_requested = Signal()
    refresh_requested = Signal()         # re-fetch saved subscriptions

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.setFocusPolicy(Qt.StrongFocus)
        self._configs: list[ProxyConfig] = []
        self._signature: tuple = ()
        self._pings: dict[str, object] = {}
        self._active_name = ""
        self._connected = False
        self._selected_name = ""
        self._sort_mode = SORT_SPEED

        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.PAGE_PAD_X, tokens.PAGE_PAD_Y,
                               tokens.PAGE_PAD_X, tokens.PAGE_PAD_Y)
        col.setSpacing(tokens.SP_3)

        head = QHBoxLayout()
        head.setSpacing(tokens.SP_2)
        head.addWidget(kit.label(tr("srv.title"), "h1"), 0, Qt.AlignBottom)
        self.count = kit.label("", "count")
        head.addWidget(self.count, 0, Qt.AlignBottom)
        head.addStretch(1)
        self.refresh_btn = kit.Button("", "ghost", icon="refresh", size="sm",
                                      tooltip=tr("srv.refresh_tip"))
        self.refresh_btn.clicked.connect(self.refresh_requested)
        head.addWidget(self.refresh_btn)
        self.sub_btn = kit.Button(tr("srv.sub"), "secondary", icon="download", size="sm")
        self.sub_btn.clicked.connect(self.subscription_clicked)
        head.addWidget(self.sub_btn)
        self.add_btn = kit.Button(tr("srv.add"), "primary", icon="plus", size="sm")
        self.add_btn.clicked.connect(self.add_clicked)
        head.addWidget(self.add_btn)
        col.addLayout(head)

        self.toolbar = QWidget()
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(tokens.SP_2)
        self.search = kit.Input(tr("srv.search"), icon="search")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        bar.addWidget(self.search, stretch=1)
        self.sort = kit.Select(sort_labels(), self._sort_mode, icon="sort")
        self.sort.setToolTip(tr("srv.sort_tip"))
        self.sort.changed.connect(self._on_sort_changed)
        bar.addWidget(self.sort)
        self.ping_btn = kit.Button("", "secondary", icon="activity", tooltip=tr("srv.ping_tip"))
        self.ping_btn.clicked.connect(self.ping_requested)
        bar.addWidget(self.ping_btn)
        col.addWidget(self.toolbar)

        self.list = ServerList()
        col.addWidget(self.list, stretch=1000)
        self._gap = QWidget()
        col.addWidget(self._gap, stretch=1)

        self.action_bar = QFrame()
        self.action_bar.setObjectName("ktActionBar")
        actions = QHBoxLayout(self.action_bar)
        actions.setContentsMargins(tokens.SP_4, tokens.SP_2, tokens.SP_2, tokens.SP_2)
        actions.setSpacing(tokens.SP_2)
        self.picked_caption = kit.label(tr("srv.selected"), "actionText")
        actions.addWidget(self.picked_caption)
        self.picked_name = ElidedLabel()
        self.picked_name.setObjectName("ktActionName")
        actions.addWidget(self.picked_name, stretch=1)
        self.delete_btn = kit.Button(tr("srv.delete"), "danger", icon="trash", size="sm")
        self.delete_btn.clicked.connect(self._on_delete)
        actions.addWidget(self.delete_btn)
        self.connect_btn = kit.Button(tr("srv.connect"), "primary", icon="power", size="sm")
        self.connect_btn.clicked.connect(self._on_connect)
        actions.addWidget(self.connect_btn)
        col.addWidget(self.action_bar)

        self.empty_card = EmptyCard(title=tr("srv.empty_title"), text=tr("srv.empty_text"),
                                    add_label=tr("srv.empty_add"), sub_label=tr("srv.empty_sub"))
        self.empty_card.add_clicked.connect(self.add_clicked)
        self.empty_card.subscription_clicked.connect(self.subscription_clicked)
        self._empty_top = QWidget()
        self._empty_bottom = QWidget()
        col.addWidget(self._empty_top, stretch=1000)
        col.addWidget(self.empty_card)
        col.addWidget(self._empty_bottom, stretch=1000)

        self._rebuild()

    # --- what the main window drives ---------------------------------------

    def set_configs(self, configs: list, active_name: str = "", connected: bool = False) -> None:
        """Show this list. Cheap when nothing changed, so it can be called on
        every refresh tick."""
        signature = _signature(configs)
        changed = signature != self._signature
        self._configs = list(configs)
        self._signature = signature
        state_changed = (active_name, connected) != (self._active_name, self._connected)
        self._active_name, self._connected = active_name, connected
        if changed:
            self._pings = {n: v for n, v in self._pings.items() if n in {c.name for c in configs}}
            self._rebuild()
            return
        # Same servers, possibly as new objects (the list was re-read from disk).
        by_name = {c.name: c for c in self._configs}
        for row in self.list.rows:
            row.cfg = by_name.get(row.cfg.name, row.cfg)
        if state_changed:
            self._sync_state()

    def set_pings(self, pings: dict, pending: bool = False) -> None:
        """Take a full set of results. `pending=True`: a new measurement has
        just started, show every server as not measured yet."""
        self._pings = {} if pending else dict(pings)
        if pending or self._sort_mode != SORT_SPEED:
            for row in self.list.rows:
                row.set_ping(self._pings.get(row.cfg.name, _PENDING))
        else:
            self._rebuild()

    def set_ping(self, name: str, value: object) -> None:
        self._pings[name] = value
        for row in self.list.rows:
            if row.cfg.name == name:
                row.set_ping(value)

    def set_pinging(self, busy: bool) -> None:
        self.ping_btn.setEnabled(not busy)

    def set_refreshing(self, busy: bool) -> None:
        self.refresh_btn.setEnabled(not busy)
        self.refresh_btn.setToolTip(tr("srv.refreshing_tip" if busy else "srv.refresh_tip"))

    def select(self, name: str) -> None:
        if self.search.text() and name not in self.visible_names():
            self.search.clear()     # a filter left over from before would hide it
        self._selected_name = name
        self._sync_state()
        for row in self.list.rows:
            if row.cfg.name == name and not row.isHidden():
                self.list.ensure_visible(row)

    def selected(self) -> Optional[ProxyConfig]:
        return next((c for c in self._configs if c.name == self._selected_name), None)

    def visible_names(self) -> list[str]:
        return [r.cfg.name for r in self.list.rows if not r.isHidden()]

    # --- internals ----------------------------------------------------------

    def _rebuild(self) -> None:
        rows = []
        for cfg in sort_configs(self._configs, self._sort_mode, self._pings):
            row = ServerRow(cfg)
            row.set_ping(self._pings.get(cfg.name, _PENDING))
            row.clicked.connect(self._on_row_clicked)
            row.activated.connect(self._on_row_activated)
            rows.append(row)
        self.list.set_rows(rows)
        names = {c.name for c in self._configs}
        if self._selected_name not in names:
            self._selected_name = (self._active_name if self._active_name in names
                                   else (rows[0].cfg.name if rows else ""))
        has = bool(self._configs)
        for w in (self.toolbar, self.list, self._gap, self.action_bar, self.refresh_btn):
            w.setVisible(has)
        for w in (self.empty_card, self._empty_top, self._empty_bottom):
            w.setVisible(not has)
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self.search.text().strip()
        shown = 0
        for row in self.list.rows:
            hit = not query or matches(row.cfg, query)
            row.setVisible(hit)
            shown += hit
        total = len(self.list.rows)
        self.count.setText("" if not total else
                           tr("picker.count", visible=shown, total=total) if query else str(total))
        self.list.fit()
        # A selection the filter has hidden would leave the action bar acting
        # on a server that is not on screen.
        visible = self.visible_names()
        if visible and self._selected_name not in visible:
            self._selected_name = visible[0]
        self._sync_state()

    def _sync_state(self) -> None:
        for row in self.list.rows:
            row.set_selected(row.cfg.name == self._selected_name)
            row.set_active(row.cfg.name == self._active_name)
        cfg = self.selected()
        on_screen = cfg is not None and cfg.name in self.visible_names()
        in_use = on_screen and self._connected and cfg.name == self._active_name
        self.picked_caption.setText(tr("srv.selected") if on_screen else tr("srv.none_selected"))
        self.picked_name.setText(one_line(strip_flag(cfg.name), 60) if on_screen else "")
        self.connect_btn.setEnabled(on_screen and not in_use)
        self.connect_btn.setText(tr("srv.connected") if in_use else tr("srv.connect"))
        self.delete_btn.setEnabled(on_screen and not in_use)
        self.delete_btn.setToolTip(tr("srv.del_connected_tip") if in_use else "")

    def _on_sort_changed(self, mode: int) -> None:
        self._sort_mode = mode
        self._rebuild()

    def _on_row_clicked(self, row: ServerRow) -> None:
        self._selected_name = row.cfg.name
        self._sync_state()

    def _on_row_activated(self, row: ServerRow) -> None:
        self._selected_name = row.cfg.name
        self._sync_state()
        self._on_connect()

    def _on_connect(self) -> None:
        cfg = self.selected()
        if cfg is not None and self.connect_btn.isEnabled():
            self.connect_requested.emit(cfg)

    def _on_delete(self) -> None:
        cfg = self.selected()
        if cfg is None or not self.delete_btn.isEnabled():
            return
        name = one_line(strip_flag(cfg.name), 40)
        if kit.confirm(self, "trash", tr("srv.del_title", name=name), tr("srv.del_text"),
                       ok_label=tr("srv.delete"), cancel_label=tr("srv.cancel"), danger=True):
            self.delete_requested.emit(cfg)

    def _move_selection(self, step: int) -> None:
        shown = [r for r in self.list.rows if not r.isHidden()]
        if not shown:
            return
        at = next((i for i, r in enumerate(shown) if r.cfg.name == self._selected_name), -1)
        row = shown[max(0, min(len(shown) - 1, at + step))]
        self._selected_name = row.cfg.name
        self._sync_state()
        self.list.ensure_visible(row)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        # Enter and Delete act on the selected server only when they are not
        # meant for something else: the search field or a focused button.
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Delete) \
                and isinstance(self.focusWidget(), (QLineEdit, QAbstractButton)):
            super().keyPressEvent(event)
            return
        if key == Qt.Key_Down:
            self._move_selection(1)
        elif key == Qt.Key_Up:
            self._move_selection(-1)
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self._on_connect()
        elif key == Qt.Key_Delete:
            self._on_delete()
        else:
            super().keyPressEvent(event)
