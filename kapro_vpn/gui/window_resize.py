"""Frameless-window resize support — 8 invisible edge/corner handles.

KaproVPN's MainWindow is `Qt.FramelessWindowHint`, which means Windows
strips the non-client area entirely — no WM_NCHITTEST is sent for the
client region, no native cursors at edges, no native resize. (My
v1.16.1 attempt used a nativeEvent NCHITTEST hook, which doesn't
work for frameless windows because there's no non-client area to
hit-test in the first place.)

The working approach — same one Telegram Desktop, AmneziaVPN, and
basically every frameless Qt app uses — is to place 8 invisible
child widgets at the window edges and corners:

    +--+--------------------+--+
    |TL|        TOP         |TR|   ← 6 px tall edges
    +--+--------------------+--+
    |  |                    |  |
    |L |       content      |R |   ← 6 px wide edges
    |  |                    |  |
    +--+--------------------+--+
    |BL|       BOTTOM       |BR|
    +--+--------------------+--+

Each handle:
  - is transparent (no QSS background, doesn't show up visually)
  - has its own Qt.Size{Hor,Ver,FDiag,BDiag}Cursor — so hovering
    instantly shows the correct double-arrow
  - intercepts mousePressEvent + mouseMoveEvent to drag-resize the
    parent window

Handles are positioned in MainWindow.resizeEvent so they track size
changes (corners follow corners as the window grows).

Cross-platform — works identically on Windows, macOS, Linux. No
native-API dependencies, no platform forks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget


# Width of the resize-sensitive border, in pixels. 6 px matches what
# Windows itself uses for its native frame border-grab — wide enough
# to grab easily, narrow enough not to steal nearby content clicks.
RESIZE_BORDER = 6


# Bit flags describing which edges a handle responds to. A corner
# handle has both an X-axis flag (LEFT or RIGHT) AND a Y-axis flag
# (TOP or BOTTOM) set. _do_resize uses these to figure out which
# edges of the parent geometry to push as the mouse drags.
_EDGE_LEFT   = 0b0001
_EDGE_RIGHT  = 0b0010
_EDGE_TOP    = 0b0100
_EDGE_BOTTOM = 0b1000


@dataclass(frozen=True)
class _HandleSpec:
    """Static description of one of the 8 resize-zones."""
    edge_flags: int      # which sides this handle pushes
    cursor: Qt.CursorShape

    @property
    def is_left(self) -> bool:   return bool(self.edge_flags & _EDGE_LEFT)
    @property
    def is_right(self) -> bool:  return bool(self.edge_flags & _EDGE_RIGHT)
    @property
    def is_top(self) -> bool:    return bool(self.edge_flags & _EDGE_TOP)
    @property
    def is_bottom(self) -> bool: return bool(self.edge_flags & _EDGE_BOTTOM)


# Eight zones, mapped to the cursor Windows itself shows for each.
# Naming key: T/B/L/R for top/bottom/left/right edges; TL/TR/BL/BR
# for the four corners. Cursor shapes are platform-themed by Qt.
_HANDLES = {
    "T":  _HandleSpec(_EDGE_TOP,                    Qt.SizeVerCursor),
    "B":  _HandleSpec(_EDGE_BOTTOM,                 Qt.SizeVerCursor),
    "L":  _HandleSpec(_EDGE_LEFT,                   Qt.SizeHorCursor),
    "R":  _HandleSpec(_EDGE_RIGHT,                  Qt.SizeHorCursor),
    "TL": _HandleSpec(_EDGE_TOP | _EDGE_LEFT,       Qt.SizeFDiagCursor),
    "TR": _HandleSpec(_EDGE_TOP | _EDGE_RIGHT,      Qt.SizeBDiagCursor),
    "BL": _HandleSpec(_EDGE_BOTTOM | _EDGE_LEFT,    Qt.SizeBDiagCursor),
    "BR": _HandleSpec(_EDGE_BOTTOM | _EDGE_RIGHT,   Qt.SizeFDiagCursor),
}


class _ResizeHandle(QWidget):
    """One of the eight transparent edge/corner zones.

    Stays invisible (no paintEvent, no background) — just exists to
    intercept the cursor and mouse events at its assigned location.
    """

    def __init__(self, parent_window: QWidget, spec: _HandleSpec) -> None:
        super().__init__(parent_window)
        self._window = parent_window
        self._spec = spec
        self.setCursor(spec.cursor)
        # Drag state: when the user mousePresses on us, we capture the
        # window geometry and screen-space mouse origin. Subsequent
        # mouseMove events compute the delta and update the parent
        # geometry. Released on mouseRelease.
        self._drag_origin_global: Optional[QPoint] = None
        self._drag_origin_geo: Optional[QRect] = None

    # ----- mouse handlers -------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_origin_global = event.globalPosition().toPoint()
            self._drag_origin_geo = self._window.geometry()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_origin_global is None or self._drag_origin_geo is None:
            super().mouseMoveEvent(event)
            return
        if not (event.buttons() & Qt.LeftButton):
            super().mouseMoveEvent(event)
            return
        # Delta in screen pixels since the drag started.
        delta = event.globalPosition().toPoint() - self._drag_origin_global
        self._apply_resize(delta)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_origin_global = None
        self._drag_origin_geo = None
        super().mouseReleaseEvent(event)

    # ----- geometry ---------------------------------------------------

    def _apply_resize(self, delta: QPoint) -> None:
        """Apply the drag delta to the parent window's geometry.

        Each edge flag the handle has set determines which side of the
        rectangle moves with the mouse. Bottom-right handle pushes
        right + bottom; top-left pushes left + top (and shifts the
        window position); etc.

        Clamped to min/max sizes — when the user drags past the
        minimum, the moving edge sticks at the limit instead of
        flipping or letting the rectangle invert.
        """
        geo = QRect(self._drag_origin_geo)
        spec = self._spec

        if spec.is_left:
            geo.setLeft(geo.left() + delta.x())
        if spec.is_right:
            geo.setRight(geo.right() + delta.x())
        if spec.is_top:
            geo.setTop(geo.top() + delta.y())
        if spec.is_bottom:
            geo.setBottom(geo.bottom() + delta.y())

        # Enforce minimum: if width/height fell below the parent's
        # minimum, snap the moving edge back. For a left/top drag we
        # have to adjust the leading edge (the one that moves with the
        # mouse) because anchoring is from the opposite side.
        min_w = self._window.minimumWidth()
        min_h = self._window.minimumHeight()
        if geo.width() < min_w:
            if spec.is_left:
                geo.setLeft(geo.right() - min_w + 1)
            elif spec.is_right:
                geo.setRight(geo.left() + min_w - 1)
        if geo.height() < min_h:
            if spec.is_top:
                geo.setTop(geo.bottom() - min_h + 1)
            elif spec.is_bottom:
                geo.setBottom(geo.top() + min_h - 1)

        # Same clamp on max size (relevant only if MainWindow ever sets
        # one — by default Qt's max is 16777215 which never triggers).
        max_w = self._window.maximumWidth()
        max_h = self._window.maximumHeight()
        if geo.width() > max_w:
            if spec.is_left:
                geo.setLeft(geo.right() - max_w + 1)
            elif spec.is_right:
                geo.setRight(geo.left() + max_w - 1)
        if geo.height() > max_h:
            if spec.is_top:
                geo.setTop(geo.bottom() - max_h + 1)
            elif spec.is_bottom:
                geo.setBottom(geo.top() + max_h - 1)

        self._window.setGeometry(geo)


class ResizeHandles:
    """Container for the 8 resize-handles attached to a frameless window.

    Owner code (MainWindow) calls install() once, then reposition()
    in its resizeEvent. The handles stay raised above all other
    children so clicks at the very edge always hit them, even if a
    button or label happens to extend out to the window edge.
    """

    def __init__(self, parent_window: QWidget):
        self._window = parent_window
        self._handles: List[_ResizeHandle] = []
        self._by_key: dict[str, _ResizeHandle] = {}

    def install(self) -> None:
        """Create and parent the 8 handle widgets. Call once."""
        for key, spec in _HANDLES.items():
            handle = _ResizeHandle(self._window, spec)
            self._handles.append(handle)
            self._by_key[key] = handle
            handle.show()
        self.reposition()

    def reposition(self) -> None:
        """Lay out the 8 handles around the parent window's current size.

        Call from MainWindow.resizeEvent so the corners track corners
        when the window grows or shrinks. Also called once after
        install() to get them placed initially.

        Geometry (b = RESIZE_BORDER):
            T : (b, 0,        w-2b, b)
            B : (b, h-b,      w-2b, b)
            L : (0, b,        b,    h-2b)
            R : (w-b, b,      b,    h-2b)
            TL: (0, 0,        b, b)
            TR: (w-b, 0,      b, b)
            BL: (0, h-b,      b, b)
            BR: (w-b, h-b,    b, b)
        """
        w = self._window.width()
        h = self._window.height()
        b = RESIZE_BORDER

        self._by_key["T"].setGeometry(b, 0, max(0, w - 2 * b), b)
        self._by_key["B"].setGeometry(b, h - b, max(0, w - 2 * b), b)
        self._by_key["L"].setGeometry(0, b, b, max(0, h - 2 * b))
        self._by_key["R"].setGeometry(w - b, b, b, max(0, h - 2 * b))

        self._by_key["TL"].setGeometry(0,     0,     b, b)
        self._by_key["TR"].setGeometry(w - b, 0,     b, b)
        self._by_key["BL"].setGeometry(0,     h - b, b, b)
        self._by_key["BR"].setGeometry(w - b, h - b, b, b)

        # Raise above all other children — at the very edge a button
        # or label might overlap our 6 px strip, and we want our
        # cursor + drag to win.
        for handle in self._handles:
            handle.raise_()


# ----- Pure-function hit test (kept for unit testing) ---------------------

def hit_test_local(
    local_x: int, local_y: int,
    width: int, height: int,
    border: int = RESIZE_BORDER,
) -> str:
    """Pure-function classification of a point within a widget's bounds.

    Returns a handle-key ("T", "B", "L", "R", "TL", "TR", "BL", "BR")
    or "CLIENT" for the central region. Kept around for smoke tests —
    the actual runtime resize is event-driven (not poll-based) and
    doesn't call this.
    """
    left   = 0 <= local_x < border
    right  = width - border < local_x <= width
    top    = 0 <= local_y < border
    bottom = height - border < local_y <= height

    if top and left:     return "TL"
    if top and right:    return "TR"
    if bottom and left:  return "BL"
    if bottom and right: return "BR"
    if left:             return "L"
    if right:            return "R"
    if top:              return "T"
    if bottom:           return "B"
    return "CLIENT"
