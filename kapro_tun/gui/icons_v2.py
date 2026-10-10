"""The v2 icon set: one outline per icon on a 24-unit grid, 1.75 stroke, round
caps and joins. Drawn with QSvgRenderer in whatever colour the caller's theme
token says, at any size and pixel ratio — no bitmap assets, no emoji.

(Emoji were the old icons. Windows has no flag emoji at all, renders some
symbols in colour and others as outlines, and none of them follow the theme.)
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel, QWidget

_C = "M3.5 12a8.5 8.5 0 1 0 17 0a8.5 8.5 0 1 0-17 0"
_SHIELD = "M12 3.5 19 6v5.5c0 4.3-2.9 7.6-7 9-4.1-1.4-7-4.7-7-9V6z"

PATHS: dict[str, str] = {
    "minus": "M6 12h12",
    "x": "M6 6l12 12M18 6 6 18",
    "home": "M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5.5h-5V20H5a1 1 0 0 1-1-1z",
    "globe": _C + "M3.5 12h17M12 3.5c2.6 2.4 3.8 5.2 3.8 8.5s-1.2 6.1-3.8 8.5M12 3.5C9.4 5.9 8.2 8.7 8.2 12s1.2 6.1 3.8 8.5",
    "chart": "M5 20v-8M12 20V5M19 20v-9",
    "sliders": "M4 7h9M17 7h3M4 12h3M11 12h9M4 17h11M19 17h1M13 7a2 2 0 1 0 4 0a2 2 0 1 0-4 0M7 12a2 2 0 1 0 4 0a2 2 0 1 0-4 0M15 17a2 2 0 1 0 4 0a2 2 0 1 0-4 0",
    "power": "M12 4v7M7.1 7.2a7 7 0 1 0 9.8 0",
    "chevron-right": "m10 7 5 5-5 5",
    "chevron-left": "m14 7-5 5 5 5",
    "chevron-down": "m7 10 5 5 5-5",
    "chevron-up": "m7 14 5-5 5 5",
    "check": "m5 12.5 4.5 4.5L19 7.5",
    "plus": "M12 5v14M5 12h14",
    "search": "M5 11a6 6 0 1 0 12 0a6 6 0 1 0-12 0M15.5 15.5 20 20",
    "refresh": "M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4",
    "trash": "M4.5 7h15M9.5 7V5h5v2M6.5 7l1 12.5h9L17.5 7M10.5 11v5M13.5 11v5",
    "download": "M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14",
    "link": "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7L11.5 6.8M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.5-1.5",
    "copy": "M10 9h8a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-8a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1zM5 15V6a1 1 0 0 1 1-1h9",
    "paste": "M9 3.5h6v3H9zM9 5H6.5a1 1 0 0 0-1 1v13.5a1 1 0 0 0 1 1h11a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1H15",
    "qr": "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h2v2h-2zM18 18h2v2h-2zM14 18h2M18 14h2",
    "arrow-down": "M12 5v13M7 13l5 5 5-5",
    "arrow-up": "M12 19V6M7 11l5-5 5 5",
    "pin": "M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0C18.5 15.4 12 21 12 21zM9.7 10a2.3 2.3 0 1 0 4.6 0a2.3 2.3 0 1 0-4.6 0",
    "lock": "M7 10.5h10a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-5.5a2 2 0 0 1 2-2zM8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5",
    "shield-check": _SHIELD + "M9 12l2 2 4-4",
    "shield-alert": _SHIELD + "M12 8.5v4M12 15.5v.01",
    "activity": "M3 12h4l2.5-6 5 12 2.5-6H21",
    "route": "M4 12h6c3 0 4-5 7-5h3M10 12c3 0 4 5 7 5h3",
    "clock": _C + "M12 7.5V12l3 2",
    "alert-triangle": "M12 4 21 19.5H3zM12 10v4M12 17v.01",
    "alert-circle": _C + "M12 8v4.5M12 16v.01",
    "info": _C + "M12 11v5M12 8v.01",
    "x-circle": _C + "M9.5 9.5l5 5M14.5 9.5l-5 5",
    "check-circle": _C + "M8.5 12.2l2.4 2.4 4.6-4.8",
    "file": "M7 3.5h7l4 4v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1zM14 3.5v4h4M9 12h6M9 15.5h6",
    "edit": "M5 19h3.5L18.5 9 15 5.5 5 15.5zM13 7.5 16.5 11",
    "external": "M14 5h5v5M19 5l-8 8M17 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h4",
    "sort": "M7 5v14M4 16l3 3 3-3M17 19V5M14 8l3-3 3 3",
    "zap": "M13 3 5 13.5h6L10 21l8-10.5h-6z",
    "gamepad": "M7.5 8h9a4 4 0 0 1 4 4.5l-.6 3.6a2 2 0 0 1-3.4 1.1L14 15h-4l-2.5 2.2a2 2 0 0 1-3.4-1.1l-.6-3.6A4 4 0 0 1 7.5 8zM8 11v3M6.5 12.5h3M15.5 12h.01M17.5 13.5h.01",
    "bug": "M8 9.5h8V15a4 4 0 0 1-8 0zM9.5 9.5V8a2.5 2.5 0 0 1 5 0v1.5M4 13h4M16 13h4M5 8.5l3 1.5M19 8.5l-3 1.5M5 18l3-1.5M19 18l-3-1.5",
    "eye": "M3.5 12S6.5 6 12 6s8.5 6 8.5 6-3 6-8.5 6S3.5 12 3.5 12zM9.5 12a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0",
    "eye-off": "M3.5 12S6.5 6 12 6c1.6 0 3 .5 4.2 1.2M20.5 12S17.5 18 12 18c-1.6 0-3-.5-4.2-1.2M4 4l16 16M10 10.2a2.5 2.5 0 0 0 3.6 3.5",
    "language": "M4 5.5h9M8.5 4v1.5M6 5.5c.6 3.3 2.7 5.8 6 7.5M11 5.5c-.8 3.5-3 6.2-6.5 8M13 20l3.5-8.5L20 20M14.3 17h4.4",
    "moon": "M19.5 14.5A7.5 7.5 0 1 1 9.5 4.5a6 6 0 0 0 10 10z",
    "login": "M14 4.5h4a1.5 1.5 0 0 1 1.5 1.5v12a1.5 1.5 0 0 1-1.5 1.5h-4M10 8l4 4-4 4M14 12H4",
    "plug": "M9 3.5v4M15 3.5v4M7 7.5h10v3a5 5 0 0 1-10 0zM12 15.5v5",
    "apps": "M4.5 4.5h6v6h-6zM13.5 4.5h6v6h-6zM4.5 13.5h6v6h-6zM13.5 13.5h6v6h-6z",
    "gauge": "M4 15a8 8 0 1 1 16 0M12 15l3.5-4.5",
    "database": "M4.5 5h15v5h-15zM4.5 14h15v5h-15zM7.5 7.5h.01M7.5 16.5h.01",
    "video": "M3.5 7h11a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1zM15.5 10.5l5-3v9l-5-3",
    "network": "M9 9h6v6H9zM12 3.5V9M12 15v5.5M3.5 12H9M15 12h5.5",
    "droplet": "M12 3.5s6 6.4 6 10.5a6 6 0 0 1-12 0c0-4.1 6-10.5 6-10.5z",
    "update": _C + "M12 16V8M8.5 11.5 12 8l3.5 3.5",
    "list": "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01",
    "more": "M11 6a1 1 0 1 0 2 0a1 1 0 1 0-2 0M11 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0M11 18a1 1 0 1 0 2 0a1 1 0 1 0-2 0",
    "github": "M9 19c-4 1.3-4-2-5.5-2.5M14.5 21v-3.2a2.8 2.8 0 0 0-.8-2.2c2.7-.3 5.5-1.3 5.5-6a4.7 4.7 0 0 0-1.3-3.2 4.3 4.3 0 0 0-.1-3.2s-1-.3-3.3 1.3a11.4 11.4 0 0 0-6 0C6.2 2.9 5.2 3.2 5.2 3.2a4.3 4.3 0 0 0-.1 3.2A4.7 4.7 0 0 0 3.8 9.6c0 4.7 2.8 5.7 5.5 6a2.8 2.8 0 0 0-.8 2.2V21",
}

_cache: dict[tuple, QPixmap] = {}


def _svg(name: str, color: str, size: int) -> bytes:
    stroke = 2.25 if size <= 12 else 1.75
    d = PATHS.get(name) or PATHS["info"]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
        f'stroke-linejoin="round"><path d="{d}"/></svg>'
    ).encode("ascii")


def pixmap(name: str, size: int = 20, color: str = "#fafafa",
           dpr: Optional[float] = None) -> QPixmap:
    """The icon as a pixmap of `size` logical pixels in `color`."""
    if dpr is None:
        app = QGuiApplication.instance()
        dpr = float(app.devicePixelRatio()) if app is not None else 1.0
    key = (name, size, color, dpr)
    pm = _cache.get(key)
    if pm is not None:
        return pm
    px = max(1, round(size * dpr))
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    QSvgRenderer(QByteArray(_svg(name, color, size))).render(painter, QRectF(0, 0, px, px))
    painter.end()
    pm.setDevicePixelRatio(dpr)
    _cache[key] = pm
    return pm


def icon(name: str, size: int = 20, color: str = "#fafafa") -> QIcon:
    return QIcon(pixmap(name, size, color))


class IconLabel(QLabel):
    """An icon that takes its colour from a theme token and redraws itself
    when the theme changes. `token` is a field name of tokens.Colors."""

    def __init__(self, name: str, size: int = 20, token: str = "text",
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._name, self._size, self._token = name, size, token
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._render()

    def set_icon(self, name: Optional[str] = None, token: Optional[str] = None) -> None:
        if name is not None:
            self._name = name
        if token is not None:
            self._token = token
        self._render()

    def _render(self) -> None:
        from . import tokens
        self.setPixmap(pixmap(self._name, self._size, getattr(tokens.colors(), self._token)))

    def changeEvent(self, event) -> None:  # noqa: N802 — Qt override
        if event.type() == event.Type.StyleChange:
            self._render()
        super().changeEvent(event)
