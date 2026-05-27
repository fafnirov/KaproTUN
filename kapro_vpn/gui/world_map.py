"""Tiny world map widget — shows where the active VPN exit is.

Drawn entirely in QPainter — no SVG file, no QtSvg dep, no bundled
image asset. Continents are hand-crafted polygons in (lat, lon) space,
projected to widget pixels via equirectangular projection. Pin = filled
amber circle with a soft glow centered on the active country's
coordinates.

Why hand-crafted polygons instead of a real SVG:
  - SVG world maps from Wikipedia are 50-500 KB each, and we'd need
    two (dark + light themes have different ocean/land colors), or
    QPainter recolor tricks. Adds binary weight + complexity for what's
    a 400×140 px decorative widget.
  - Hand polygons let us live-recolor by just passing palette colors
    to setBrush() each paintEvent — instant theme support.
  - Geographic accuracy doesn't matter at this scale — the user has
    a country flag and city name in the UI already; the map is for
    "ah that's roughly over there" recognition, not navigation.

Trade-off: continents are simplified to ~10-15 vertices each. Recognizable
silhouettes, but no fjord-level detail.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPaintEvent, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from . import styles


# Continent outlines, each as a list of (lat_deg, lon_deg) vertices.
# Walked clockwise so QPainterPath winding gives a filled shape. Hand-
# placed from memory + a wall map — not GIS-accurate. Updating: add
# vertices, re-eyeball, ship.
_CONTINENT_POLYGONS: list[list[tuple[float, float]]] = [
    # North America — Alaska through Florida / Central America
    [
        (72,-160), (70,-130), (70,-90), (60,-65), (45,-55),
        (35,-75), (25,-80), (18,-95), (8,-78), (15,-95),
        (25,-110), (35,-122), (50,-130), (60,-145), (68,-160),
    ],
    # South America — Caribbean coast through Tierra del Fuego
    [
        (12,-72), (10,-60), (5,-50), (-5,-35), (-25,-42),
        (-40,-60), (-55,-68), (-50,-75), (-30,-72), (-10,-80),
        (5,-80),
    ],
    # Europe — British Isles through Caucasus
    [
        (58,-10), (60,5), (70,25), (66,40), (55,55), (45,40),
        (38,28), (40,15), (45,3), (50,-5),
    ],
    # Africa — Atlas Mountains through Cape of Good Hope
    [
        (35,-10), (35,10), (30,33), (12,43), (-12,40),
        (-32,28), (-35,20), (-25,15), (-15,5), (0,-5),
        (15,-15), (25,-15),
    ],
    # Asia — Caspian through Kamchatka, down to Vietnam
    [
        (55,40), (70,60), (75,100), (68,140), (55,160), (45,150),
        (35,140), (28,122), (20,108), (10,100), (15,80),
        (25,72), (35,55), (40,48),
    ],
    # India sub-continent (separate from main Asia polygon for shape clarity)
    [
        (32,70), (28,78), (22,90), (10,80), (15,72), (25,68),
    ],
    # Indonesia / Malay archipelago (rough strip)
    [
        (-2,95), (5,100), (8,118), (-5,135), (-10,120), (-5,105),
    ],
    # Australia
    [
        (-12,130), (-12,145), (-25,154), (-38,145), (-35,115), (-20,114),
    ],
]


# Country centroids (lat, lon) for the VPN-server countries our users
# actually connect through. Not exhaustive — just the ~50 typical
# locations. Unknown codes => no pin drawn (set_country silently
# no-ops). Coords are rough country centers, not capitals — pin is
# meant to land "in" the country shape, not on the capital dot.
COUNTRY_COORDS: dict[str, tuple[float, float]] = {
    # Europe
    "NL": (52.1, 5.3),  "DE": (51.2, 10.5), "FR": (46.6, 2.2),
    "GB": (54.0, -2.0), "UK": (54.0, -2.0),
    "FI": (61.9, 25.7), "SE": (60.1, 18.6), "NO": (60.5, 8.5),
    "DK": (56.0, 9.5),  "BE": (50.5, 4.5),  "LU": (49.6, 6.1),
    "CH": (46.8, 8.2),  "AT": (47.5, 14.5), "IT": (41.9, 12.6),
    "ES": (40.5, -3.7), "PT": (39.4, -8.2), "IE": (53.4, -8.2),
    "PL": (51.9, 19.1), "CZ": (49.8, 15.5), "SK": (48.7, 19.7),
    "HU": (47.2, 19.5), "RO": (45.9, 24.9), "BG": (42.7, 25.5),
    "GR": (39.1, 21.8), "RS": (44.0, 21.0), "HR": (45.1, 15.2),
    "SI": (46.2, 14.9), "EE": (58.6, 25.0), "LV": (56.9, 24.6),
    "LT": (55.2, 23.9), "MD": (47.4, 28.4), "UA": (48.4, 31.2),
    "BY": (53.7, 27.9), "RU": (61.5, 105.3),
    # Mediterranean / Mid-East
    "TR": (39.0, 35.2), "IL": (31.0, 34.9), "AE": (24.0, 54.0),
    "CY": (35.1, 33.4),
    # North America
    "US": (39.8, -98.6),"CA": (56.1, -106.3),"MX": (23.6, -102.6),
    # South America
    "BR": (-14.2, -51.9),"AR": (-38.4, -63.6),"CL": (-35.7, -71.5),
    # Asia
    "JP": (36.2, 138.3),"KR": (35.9, 127.8),"CN": (35.9, 104.2),
    "HK": (22.3, 114.2),"TW": (23.7, 121.0),"SG": (1.4, 103.8),
    "MY": (4.2, 101.9), "TH": (15.9, 100.9),"VN": (14.1, 108.3),
    "PH": (12.9, 121.8),"ID": (-0.8, 113.9),"IN": (20.6, 78.9),
    "KZ": (48.0, 66.9),"GE": (42.3, 43.4), "AM": (40.1, 45.0),
    # Oceania
    "AU": (-25.3, 133.8),"NZ": (-40.9, 174.9),
    # Africa (rarely seen as VPN exit but included for completeness)
    "ZA": (-30.6, 22.9),"EG": (26.8, 30.8),
}


# Map widget dimensions. 2:1 aspect matches equirectangular — wider
# would just empty-ocean padding, narrower would crush continents.
_MAP_W = 400
_MAP_H = 200


def _project(lat: float, lon: float, width: int, height: int) -> QPointF:
    """Equirectangular: lon → x, lat → y (with y flipped because
    pixel-y grows downward but lat grows upward)."""
    x = (lon + 180.0) / 360.0 * width
    y = (90.0 - lat) / 180.0 * height
    return QPointF(x, y)


class WorldMapWidget(QWidget):
    """Tiny world map. Call set_country('NL') to plant a pin.

    Theme reads palette via styles.get_active_palette() on each paint —
    so when the user flips dark/light at runtime, the map re-paints
    in the right colors on the next event loop tick (no explicit
    refresh-on-theme-change wiring needed).
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(_MAP_W, _MAP_H)
        self._country_code: Optional[str] = None
        self._theme_getter = lambda: "auto"  # main_window sets this

    def set_theme_getter(self, getter) -> None:
        """Hand us a callable that returns the current theme setting
        ('auto'/'dark'/'light'). Called on every paintEvent so the
        map matches the live theme without explicit wiring.
        """
        self._theme_getter = getter

    def set_country(self, country_code: Optional[str]) -> None:
        """Move the pin. None or unknown code → no pin drawn."""
        self._country_code = (country_code or "").upper() or None
        self.update()

    # --- painting --------------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        palette = styles.get_active_palette(self._theme_getter())

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Background — same as surrounding page so the widget blends in
        # rather than looking like an inset panel.
        p.fillRect(self.rect(), QColor(palette.BG))

        # Continents — drawn as one combined path so they share the same
        # brush fill and we make one paint call.
        land_color = QColor(palette.SURFACE_HI)
        land_outline = QColor(palette.BORDER)
        land_path = QPainterPath()
        for poly in _CONTINENT_POLYGONS:
            if not poly:
                continue
            first = _project(poly[0][0], poly[0][1], _MAP_W, _MAP_H)
            land_path.moveTo(first)
            for lat, lon in poly[1:]:
                land_path.lineTo(_project(lat, lon, _MAP_W, _MAP_H))
            land_path.closeSubpath()
        p.setBrush(QBrush(land_color))
        p.setPen(QPen(land_outline, 1.0))
        p.drawPath(land_path)

        # Pin — drawn last so it sits on top of continents. Outer glow
        # gives the amber "active" look that matches the connect-button
        # state, tying the map visually to the rest of the connected UI.
        if self._country_code and self._country_code in COUNTRY_COORDS:
            lat, lon = COUNTRY_COORDS[self._country_code]
            center = _project(lat, lon, _MAP_W, _MAP_H)
            self._paint_pin(p, center, QColor(palette.ACCENT))

        p.end()

    def _paint_pin(self, p: QPainter, center: QPointF, color: QColor) -> None:
        # Soft outer glow — radial gradient from accent (centre, full
        # alpha) to transparent at the edge. ~12 px radius for visible
        # halo on the dense continent fill.
        glow_radius = 12.0
        glow = QRadialGradient(center, glow_radius)
        glow_color = QColor(color)
        glow_color.setAlpha(150)
        glow.setColorAt(0.0, glow_color)
        glow_color.setAlpha(0)
        glow.setColorAt(1.0, glow_color)
        p.setBrush(QBrush(glow))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(center, glow_radius, glow_radius)

        # Solid inner dot — 4 px radius, full accent color. Sharp enough
        # to be visibly "where the pin is" even on continents of the
        # same warm-grey background colour.
        p.setBrush(QBrush(color))
        p.drawEllipse(center, 4.0, 4.0)
