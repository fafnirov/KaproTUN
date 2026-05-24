"""Icon loading + programmatic-drawn fallbacks.

If real artwork is present in `kapro_vpn/data/` (icon.ico, tray_idle.png,
tray_connected.png, tray_connecting.png), it's loaded directly. Otherwise
we draw amber-on-dark placeholders so the app always has *something* to
display — the user can drop in real PNG assets later without code changes.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap

from . import styles


def _data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def _draw_circle_with_letter(size: int, ring_color: QColor, letter: str = "K",
                             letter_color: QColor = None) -> QPixmap:
    """Draw an amber-ring with a centered letter. Used as fallback icon."""
    if letter_color is None:
        letter_color = ring_color
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)

    # Outer ring
    margin = max(2, size // 10)
    thickness = max(2, size // 12)
    rect_size = size - margin * 2
    pen = QPen(ring_color, thickness)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(margin, margin, rect_size, rect_size)

    # Letter
    if letter:
        font_size = max(6, int(size * 0.45))
        f = QFont("Segoe UI", font_size)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(letter_color))
        p.drawText(0, 0, size, size, Qt.AlignCenter, letter)

    p.end()
    return pix


@lru_cache(maxsize=8)
def app_icon() -> QIcon:
    """Main app icon shown in taskbar/title."""
    ico_path = _data_dir() / "icon.ico"
    if ico_path.is_file():
        return QIcon(str(ico_path))
    png_path = _data_dir() / "icon.png"
    if png_path.is_file():
        return QIcon(str(png_path))
    # Fallback: programmatic
    icon = QIcon()
    accent = QColor(styles.ACCENT)
    for sz in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_draw_circle_with_letter(sz, accent))
    return icon


def _tray_icon_for_state(state: str) -> QIcon:
    """state ∈ {'idle', 'connected', 'connecting'}"""
    filename_map = {
        "idle":       "tray_idle.png",
        "connected":  "tray_connected.png",
        "connecting": "tray_connecting.png",
    }
    color_map = {
        "idle":       QColor(styles.TEXT_DIM),    # gray
        "connected":  QColor(styles.ACCENT),       # amber
        "connecting": QColor(styles.ACCENT_HI),    # paler amber
    }
    asset = _data_dir() / filename_map.get(state, "tray_idle.png")
    if asset.is_file():
        return QIcon(str(asset))
    icon = QIcon()
    color = color_map.get(state, QColor(styles.TEXT_DIM))
    for sz in (16, 24, 32):
        icon.addPixmap(_draw_circle_with_letter(sz, color))
    return icon


def tray_idle() -> QIcon:
    return _tray_icon_for_state("idle")


def tray_connected() -> QIcon:
    return _tray_icon_for_state("connected")


def tray_connecting() -> QIcon:
    return _tray_icon_for_state("connecting")


def splash_pixmap(size: int = 320) -> QPixmap:
    """Centered logo for the splash screen."""
    splash_path = _data_dir() / "splash.png"
    if splash_path.is_file():
        return QPixmap(str(splash_path)).scaled(
            size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
    # Fallback: big ring + "K" on dark background
    pix = QPixmap(size, size)
    pix.fill(QColor(styles.BG))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)

    accent = QColor(styles.ACCENT)
    inner = _draw_circle_with_letter(int(size * 0.65), accent)
    x = (size - inner.width()) // 2
    y = (size - inner.height()) // 2 - 20
    p.drawPixmap(x, y, inner)

    # App name beneath the logo
    f = QFont("Segoe UI", 14)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QPen(QColor(styles.TEXT)))
    p.drawText(0, y + inner.height() + 8, size, 30, Qt.AlignHCenter, "KaproVPN")

    p.end()
    return pix
