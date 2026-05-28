"""Frameless-window resize support — Windows native + cross-platform fallback.

KaproVPN's MainWindow is `Qt.FramelessWindowHint`, which means the OS
doesn't draw resize handles for us. Until v1.16.1 we worked around that
by calling `setFixedSize(480, 870)` — fine until users with bigger
monitors wanted more chart real estate, or laptop users wanted to
shrink the window.

This module gives us proper resize behaviour without giving up the
custom dark titlebar:

  - Windows: hook `WM_NCHITTEST` in QWidget.nativeEvent so the OS knows
    that the 6 px border around our client area is a resize zone.
    Cursors change, drag-to-resize works, snap-to-half works, Aero
    Snap with Win+Arrow works — all native, all free.

  - macOS / Linux: drop a `QSizeGrip` widget in the bottom-right corner.
    Only one corner, not all eight edges, but it's the de-facto
    convention for frameless windows on those platforms and Qt handles
    it transparently. The grip is invisible (no chrome) so it doesn't
    clutter the design.

Both paths respect `QWidget.minimumSize()` and `QWidget.maximumSize()`
automatically — Windows reads them from WM_GETMINMAXINFO, QSizeGrip
honours them at drag time.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Tuple

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QSizeGrip, QWidget


# ===== Windows WM_NCHITTEST glue ==========================================

# Win32 message we care about.
_WM_NCHITTEST = 0x0084

# WM_NCHITTEST return codes. The OS uses these to decide what cursor to
# show and what drag behaviour to apply. We map the 8 edge/corner zones
# to the standard codes — the rest of the client area (HTCLIENT) is
# handled by Qt's normal mouse routing.
_HTCLIENT      = 1
_HTLEFT        = 10
_HTRIGHT       = 11
_HTTOP         = 12
_HTTOPLEFT     = 13
_HTTOPRIGHT    = 14
_HTBOTTOM      = 15
_HTBOTTOMLEFT  = 16
_HTBOTTOMRIGHT = 17

# Width of the resize-sensitive border, in DIPs. 6 is the same value
# Windows itself uses for nominal frame border-grab — wider than a
# pixel-thin hairline so it's easy to grab, narrow enough that it
# doesn't steal clicks meant for content near the edge.
_RESIZE_BORDER_DIP = 6


def _parse_nchittest_pos(lparam: int) -> Tuple[int, int]:
    """WM_NCHITTEST packs the screen-space mouse XY in lParam.

    Low word = X, high word = Y. Both are SIGNED 16-bit — a window
    straddling the negative side of a multi-monitor virtual screen
    can produce values like -1500, which would wrap to 64036 if we
    treated them as unsigned. ctypes.c_short handles the sign correctly.
    """
    x = ctypes.c_short(lparam & 0xFFFF).value
    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
    return x, y


def hit_test_local(
    local_x: int, local_y: int,
    width: int, height: int,
    border: int = _RESIZE_BORDER_DIP,
) -> int:
    """Pure-function hit test on local widget coords. No Qt needed.

    Split out from windows_hit_test so smoke tests can exercise the
    border math without a real QWidget (offscreen QApplication
    doesn't actually position windows on screen, so mapFromGlobal
    returns garbage for show()/move() calls).

    Edge/corner detection uses closed intervals on the outer side
    and open on the inner so a click exactly at `border` doesn't
    get double-counted with the central client area.
    """
    left   = 0 <= local_x < border
    right  = width - border < local_x <= width
    top    = 0 <= local_y < border
    bottom = height - border < local_y <= height

    if top and left:     return _HTTOPLEFT
    if top and right:    return _HTTOPRIGHT
    if bottom and left:  return _HTBOTTOMLEFT
    if bottom and right: return _HTBOTTOMRIGHT
    if left:             return _HTLEFT
    if right:            return _HTRIGHT
    if top:              return _HTTOP
    if bottom:           return _HTBOTTOM
    return _HTCLIENT


def windows_hit_test(widget: QWidget, screen_x: int, screen_y: int) -> int:
    """Return the HTxxx code for a mouse-position relative to widget.

    Thin wrapper that converts screen → local coords via mapFromGlobal
    and defers to hit_test_local for the actual decision.
    """
    pos = widget.mapFromGlobal(QPoint(screen_x, screen_y))
    return hit_test_local(pos.x(), pos.y(), widget.width(), widget.height())


def handle_native_event(widget: QWidget, event_type, message) -> Tuple[bool, int]:
    """Intercept WM_NCHITTEST on Windows.

    Returns (handled, result) tuple matching QWidget.nativeEvent's
    contract. For non-Windows or non-NCHITTEST events, returns
    (False, 0) so the caller falls through to super().nativeEvent().
    """
    if sys.platform != "win32":
        return False, 0
    # PySide6 passes the eventType as bytes or str depending on the
    # binding's version — accept both forms.
    if event_type not in (b"windows_generic_MSG", "windows_generic_MSG"):
        return False, 0

    # `message` is a sip/shiboken capsule holding a pointer to MSG.
    # int(message) gives us its address for ctypes.
    try:
        msg_addr = int(message)
    except (TypeError, ValueError):
        return False, 0
    try:
        msg = wintypes.MSG.from_address(msg_addr)
    except (ValueError, OSError):
        return False, 0
    if msg.message != _WM_NCHITTEST:
        return False, 0

    x, y = _parse_nchittest_pos(msg.lParam)
    code = windows_hit_test(widget, x, y)
    if code == _HTCLIENT:
        # Let Qt handle the click normally (button presses, drag-to-
        # move via the titlebar, etc.).
        return False, 0
    return True, code


# ===== Cross-platform QSizeGrip fallback ==================================

def install_size_grip(parent_window: QWidget) -> QSizeGrip:
    """Install an invisible QSizeGrip in the bottom-right corner.

    Useful as a macOS/Linux fallback (WM_NCHITTEST is Windows-only).
    On Windows we don't need it — the native-event hook covers all
    eight edges/corners — so the caller skips this branch.

    The grip is a normal QWidget so it positions inside any parent
    layout. We don't restyle it: Qt's default native grip is just a
    triangular crosshatch in the corner, low-key on a dark theme,
    and matches user expectation for frameless windows.
    """
    grip = QSizeGrip(parent_window)
    grip.setFixedSize(16, 16)
    grip.setCursor(Qt.SizeFDiagCursor)
    return grip
