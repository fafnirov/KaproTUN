"""Application theming — dark (default, AmneziaVPN-inspired) + light.

v1.13.0: split the original DARK_QSS module-level constant into a
Palette dataclass + _build_qss() builder so we can have multiple
themes from one source of truth. Backward compat: `DARK_QSS` still
exists as a top-level string, so older imports keep working.

To add a third theme, define another Palette instance — no QSS
changes needed; all colours plug in via the palette fields.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    BG: str
    SURFACE: str
    SURFACE_HI: str
    SURFACE_HI_HI: str    # the "extra deep" hover state (button:hover bg)
    BORDER: str
    TEXT: str
    TEXT_MUTED: str
    TEXT_DIM: str
    ACCENT: str           # amber-500, shared brand color across themes
    ACCENT_HI: str        # accent on hover — lighter on dark, darker on light
    ACCENT_DIM: str       # bg for selected list items, hover for config card
    ACCENT_DIM_TEXT: str  # fg for selected list items (contrast on ACCENT_DIM)
    PRIMARY_TEXT: str     # text on primary (amber) buttons — same on both
    DANGER: str
    SUCCESS: str
    # Tray icon variant — system tray on light desktops needs a darker
    # icon for visibility; on dark desktops the standard one works.
    TRAY_PREFERS_LIGHT_ICON: bool


# --- Dark theme: original AmneziaVPN-style palette ------------------------

DARK_PALETTE = Palette(
    BG="#0e0e10",
    SURFACE="#18181b",
    SURFACE_HI="#27272a",
    SURFACE_HI_HI="#3a3a3d",
    BORDER="#2a2a2d",
    TEXT="#fafafa",
    TEXT_MUTED="#a1a1aa",
    TEXT_DIM="#71717a",
    ACCENT="#f59e0b",
    ACCENT_HI="#fbbf24",        # amber-400, brighter on dark for hover pop
    ACCENT_DIM="#78350f",       # amber-900 — deep amber for selected items
    ACCENT_DIM_TEXT="#fbbf24",
    PRIMARY_TEXT="#1a1209",     # near-black on amber button
    DANGER="#ef4444",
    SUCCESS="#16a34a",
    TRAY_PREFERS_LIGHT_ICON=True,
)


# --- Light theme: warm off-white, same amber accent ------------------------
# Background is #fafaf9 (stone-50), NOT pure white — pure white plus the
# amber accent looks like a half-broken default Qt theme. The warm tint
# also pairs better with the amber brand. Borders are #d6d3d1 (stone-300),
# subtle but visible. Accent hover goes DARKER (amber-600) on light, not
# lighter — opposite of dark theme — so hover stays distinguishable from
# rest state.

LIGHT_PALETTE = Palette(
    BG="#fafaf9",
    SURFACE="#ffffff",
    SURFACE_HI="#f5f5f4",
    SURFACE_HI_HI="#e7e5e4",
    BORDER="#d6d3d1",
    TEXT="#18181b",
    TEXT_MUTED="#57534e",
    TEXT_DIM="#78716c",
    ACCENT="#f59e0b",
    ACCENT_HI="#d97706",        # amber-600, darker hover for contrast on light
    ACCENT_DIM="#fef3c7",       # amber-100 — pale amber wash for selection
    ACCENT_DIM_TEXT="#92400e",  # amber-800 — strong text on pale-amber bg
    PRIMARY_TEXT="#1a1209",     # same near-black: dark text on amber works both
    DANGER="#dc2626",
    SUCCESS="#15803d",
    TRAY_PREFERS_LIGHT_ICON=False,
)


def _build_qss(p: Palette) -> str:
    """Generate the full QSS sheet from one palette.

    Single source of truth — every color in every selector comes from
    `p`. Add a field to Palette, use it here, both themes pick it up.
    """
    return f"""
* {{
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 10pt;
    color: {p.TEXT};
}}

QMainWindow, QDialog {{
    background-color: {p.BG};
}}

QWidget#page {{
    background-color: {p.BG};
}}

QWidget#appShell {{
    background-color: {p.BG};
    border: 1px solid {p.BORDER};
    border-radius: 10px;
}}

/* --- custom title bar --- */

QFrame#titleBar {{
    background-color: {p.SURFACE};
    border: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    border-bottom: 1px solid {p.BORDER};
}}

QLabel#titleBarText {{
    color: {p.TEXT};
    font-weight: 600;
    font-size: 9pt;
}}

QPushButton#titleBarBtn {{
    background-color: transparent;
    border: none;
    border-radius: 0;
    color: {p.TEXT_MUTED};
    font-size: 11pt;
    font-weight: 400;
    padding: 0;
}}

QPushButton#titleBarBtn:hover {{
    background-color: {p.SURFACE_HI};
    color: {p.TEXT};
}}

QPushButton#titleBarCloseBtn {{
    background-color: transparent;
    border: none;
    border-radius: 0;
    border-top-right-radius: 10px;
    color: {p.TEXT_MUTED};
    font-size: 11pt;
    font-weight: 400;
    padding: 0;
}}

QPushButton#titleBarCloseBtn:hover {{
    background-color: {p.DANGER};
    color: white;
}}

/* --- generic text helpers --- */

QLabel#h1   {{ font-size: 18pt; font-weight: 600; }}
QLabel#h2   {{ font-size: 13pt; font-weight: 600; }}
QLabel#muted {{ color: {p.TEXT_MUTED}; font-size: 9pt; }}
QLabel#dim  {{ color: {p.TEXT_DIM};  font-size: 9pt; }}

/* --- typography tokens (v2.1.0) — one consistent text scale ---
   Five roles used app-wide; letter-spacing 0, no viewport-based scaling.
   The legacy #h1/#h2/#muted/#dim above stay as aliases (same metrics) for
   screens not yet migrated; new/updated labels use these tokens so sizes
   and line-heights stop being ad-hoc. Secondary text is TEXT_MUTED (a step
   brighter than the old TEXT_DIM) for readability on the dark theme. */
QLabel#title     {{ font-size: 18pt; font-weight: 600; letter-spacing: 0; color: {p.TEXT}; }}
QLabel#section   {{ font-size: 13pt; font-weight: 600; letter-spacing: 0; color: {p.TEXT}; }}
QLabel#body      {{ font-size: 10pt; font-weight: 400; letter-spacing: 0; color: {p.TEXT}; }}
QLabel#secondary {{ font-size:  9pt; font-weight: 400; letter-spacing: 0; color: {p.TEXT_MUTED}; }}
QLabel#caption   {{ font-size:  8pt; font-weight: 400; letter-spacing: 0; color: {p.TEXT_DIM}; }}

/* Traffic-legend arrows — colour-matched to the home sparkline so the ↑/↓
   next to the live numbers double as the graph legend. graphValue keeps the
   numbers in the brighter TEXT for contrast against the muted captions. */
QLabel#graphDown  {{ color: {p.ACCENT};     font-size: 11pt; font-weight: 700; letter-spacing: 0; }}
QLabel#graphUp    {{ color: {p.TEXT_MUTED}; font-size: 11pt; font-weight: 700; letter-spacing: 0; }}
QLabel#graphValue {{ color: {p.TEXT}; font-size: 10pt; font-weight: 500; letter-spacing: 0; }}

/* --- buttons --- */

QPushButton {{
    background-color: {p.SURFACE_HI};
    color: {p.TEXT};
    border: 1px solid {p.BORDER};
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 500;
}}

QPushButton:hover {{
    background-color: {p.SURFACE_HI_HI};
}}

QPushButton:pressed {{
    background-color: {p.SURFACE};
}}

QPushButton:disabled {{
    color: {p.TEXT_DIM};
    background-color: {p.SURFACE};
    border-color: {p.BORDER};
}}

QPushButton#primary {{
    background-color: {p.ACCENT};
    color: {p.PRIMARY_TEXT};
    border: none;
}}

QPushButton#primary:hover {{
    background-color: {p.ACCENT_HI};
}}

QPushButton#danger {{
    background-color: transparent;
    color: {p.DANGER};
    border: 1px solid {p.DANGER};
}}

QPushButton#danger:hover {{
    background-color: rgba(239, 68, 68, 0.1);
}}

/* --- circular connect button --- */

QPushButton#circleBtn {{
    background-color: transparent;
    border: 3px solid {p.BORDER};
    /* v1.14.5: 190 → 220 to dominate the page visually again — at 190
       the button was the same size as the world map below it, which
       killed the hierarchy ("the connect circle is the hero"). 220 +
       smaller font (14pt) + tighter letter-spacing (1px instead of 2)
       lets "ПОДКЛЮЧЕНИЕ…" fit inside the ring without clipping on
       the left edge as it did at 190 / 15pt / 2px. */
    border-radius: 110px;
    min-width: 220px; max-width: 220px;
    min-height: 220px; max-height: 220px;
    color: {p.TEXT_MUTED};
    font-size: 14pt;
    font-weight: 600;
    letter-spacing: 1px;
}}

/* Compact preset — smaller hero circle for low-height screens.
   Higher specificity than the base #circleBtn rule, so it wins on size. */
QPushButton#circleBtn[compact="true"] {{
    min-width: 168px; max-width: 168px;
    min-height: 168px; max-height: 168px;
    border-radius: 84px;
    font-size: 12pt;
}}

QPushButton#circleBtn:hover {{
    border-color: {p.TEXT_MUTED};
    color: {p.TEXT};
}}

/* Mouse-down feedback: brighter ring, thicker border — instant tactile cue
   before the burst animation kicks in. */
QPushButton#circleBtn:pressed {{
    border: 4px solid {p.ACCENT_HI};
    color: {p.ACCENT_HI};
}}

QPushButton#circleBtn[state="connecting"] {{
    border-color: {p.ACCENT_DIM};
    color: {p.ACCENT};
}}

QPushButton#circleBtn[state="connected"] {{
    border-color: {p.ACCENT};
    color: {p.ACCENT};
}}

QPushButton#circleBtn[state="connecting"]:pressed,
QPushButton#circleBtn[state="connected"]:pressed {{
    border: 4px solid {p.ACCENT_HI};
    color: {p.ACCENT_HI};
}}

/* --- icon buttons (nav bar, card chevron) --- */

QPushButton#iconBtn {{
    /* Force Segoe UI Symbol — Segoe UI Emoji would render ⌂/⚙ as
       colored emoji even with U+FE0E in some Qt builds. */
    font-family: "Segoe UI Symbol", "Segoe UI", sans-serif;
    background-color: transparent;
    border: none;
    border-radius: 8px;
    padding: 8px 0;
    font-size: 18pt;
    font-weight: 400;
    color: {p.TEXT_MUTED};
    min-width: 56px;
    min-height: 44px;
    max-height: 44px;
}}

QPushButton#iconBtn:hover {{
    background-color: {p.SURFACE_HI};
    color: {p.TEXT};
}}

QPushButton#iconBtn[active="true"] {{
    color: {p.ACCENT};
}}

/* --- active config card on home --- */

QFrame#configCard {{
    background-color: {p.SURFACE};
    border: 1px solid {p.BORDER};
    border-radius: 14px;
}}

QFrame#configCard:hover {{
    border-color: {p.ACCENT_DIM};
}}

QFrame#configCard QLabel#cardTitle {{
    font-size: 13pt;
    font-weight: 600;
}}

QFrame#configCard QLabel#cardSub {{
    color: {p.TEXT_DIM};
    font-size: 9pt;
}}

QFrame#configCard QLabel#cardBadge {{
    background-color: {p.SURFACE_HI};
    color: {p.TEXT_MUTED};
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 9pt;
    font-weight: 500;
}}

/* --- list widget (configs page) --- */

QListWidget {{
    background-color: {p.SURFACE};
    border: 1px solid {p.BORDER};
    border-radius: 10px;
    padding: 4px;
    outline: 0;
}}

QListWidget::item {{
    padding: 12px;
    border-radius: 6px;
    margin: 2px;
}}

QListWidget::item:selected {{
    background-color: {p.ACCENT_DIM};
    color: {p.ACCENT_DIM_TEXT};
}}

QListWidget::item:hover:!selected {{
    background-color: {p.SURFACE_HI};
}}

/* --- inputs --- */

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox {{
    background-color: {p.SURFACE};
    border: 1px solid {p.BORDER};
    border-radius: 8px;
    padding: 8px 12px;
    selection-background-color: {p.ACCENT};
    selection-color: {p.PRIMARY_TEXT};
}}

/* QSpinBox needs explicit sizing — otherwise the up/down arrow buttons
   compress the text field to a hairline on some Windows themes. */
QSpinBox {{
    min-height: 22px;
    padding: 4px 8px;
}}

QSpinBox::up-button, QSpinBox::down-button {{
    background-color: {p.SURFACE_HI};
    border: none;
    width: 18px;
}}

QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background-color: {p.SURFACE_HI_HI};
}}

QPlainTextEdit, QTextEdit {{
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 9pt;
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus {{
    border-color: {p.ACCENT};
}}

/* --- checkbox --- */

QCheckBox {{
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 18px; height: 18px;
    border-radius: 4px;
    border: 1px solid {p.BORDER};
    background-color: {p.SURFACE};
}}

QCheckBox::indicator:checked {{
    background-color: {p.ACCENT};
    border-color: {p.ACCENT};
}}

/* --- menus --- */

QMenuBar {{
    background-color: {p.BG};
    border-bottom: 1px solid {p.BORDER};
}}

QMenuBar::item:selected {{
    background-color: {p.SURFACE_HI};
}}

QMenu {{
    background-color: {p.SURFACE_HI};
    border: 1px solid {p.BORDER};
    padding: 4px;
}}

QMenu::item {{
    padding: 6px 16px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background-color: {p.ACCENT_DIM};
    color: {p.ACCENT_DIM_TEXT};
}}

/* --- scroll area inside Settings page --- */

QScrollArea#settingsScroll {{
    background: transparent;
    border: none;
}}

QScrollArea#settingsScroll > QWidget > QWidget {{
    background: {p.BG};
}}

/* --- scrollbars --- */

QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    border: none;
}}

QScrollBar::handle:vertical {{
    background: {p.BORDER};
    border-radius: 4px;
    min-height: 24px;
}}

QScrollBar::handle:vertical:hover {{
    background: {p.TEXT_DIM};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

/* --- toast notifications --- */

QFrame#toastInfo, QFrame#toastSuccess, QFrame#toastError {{
    background-color: {p.SURFACE};
    border-radius: 10px;
    padding: 10px 14px;
}}

QFrame#toastInfo {{
    border: 1px solid {p.ACCENT};
}}

QFrame#toastSuccess {{
    border: 1px solid {p.SUCCESS};
}}

QFrame#toastError {{
    border: 1px solid {p.DANGER};
}}

QLabel#toastText {{
    color: {p.TEXT};
    font-size: 9pt;
    background: transparent;
}}

QLabel#toastIcon {{
    font-size: 13pt;
    font-weight: 700;
    color: {p.ACCENT};
    background: transparent;
    padding-right: 4px;
}}

QFrame#toastSuccess QLabel#toastIcon {{
    color: {p.SUCCESS};
}}

QFrame#toastError QLabel#toastIcon {{
    color: {p.DANGER};
}}

/* --- onboarding cards --- */

QFrame#onboardCard {{
    background-color: {p.SURFACE};
    border: 1px solid {p.SURFACE_HI};
    border-radius: 10px;
}}

QFrame#onboardCard QLabel#onboardTitle {{
    color: {p.TEXT};
    font-weight: 600;
    font-size: 11pt;
}}

/* --- separator --- */

QFrame[frameShape="4"] {{      /* HLine */
    color: {p.BORDER};
    max-height: 1px;
}}
"""


# Pre-built sheets — module import time, no per-call build cost.
DARK_QSS = _build_qss(DARK_PALETTE)
LIGHT_QSS = _build_qss(LIGHT_PALETTE)


# Backward-compat module-level constants. widgets.py and onboarding.py
# `import styles; styles.ACCENT` — these stayed valid after the refactor
# by aliasing to DARK_PALETTE values. They look fine in both themes
# (amber accent is shared; TEXT_MUTED works as a neutral gray on light too).
# Full refactor to use get_active_palette() at runtime is on the todo list
# for v1.14.0 when the map/graph widgets will need theme-aware colors.
BG = DARK_PALETTE.BG
SURFACE = DARK_PALETTE.SURFACE
SURFACE_HI = DARK_PALETTE.SURFACE_HI
BORDER = DARK_PALETTE.BORDER
TEXT = DARK_PALETTE.TEXT
TEXT_MUTED = DARK_PALETTE.TEXT_MUTED
TEXT_DIM = DARK_PALETTE.TEXT_DIM
ACCENT = DARK_PALETTE.ACCENT
ACCENT_HI = DARK_PALETTE.ACCENT_HI
ACCENT_DIM = DARK_PALETTE.ACCENT_DIM
DANGER = DARK_PALETTE.DANGER


def _detect_system_theme() -> str:
    """Return "light" or "dark" based on OS preference.

    Uses Qt 6.5+ QStyleHints.colorScheme() when available — that's the
    portable way that respects both system themes AND any per-app
    overrides Windows might apply. Falls back to "dark" if the API or
    QApplication isn't ready yet (e.g. theme requested before app
    construction, which shouldn't happen but we'd rather have a
    sensible default than crash).
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return "dark"
        hints = app.styleHints()
        # ColorScheme.Light / .Dark / .Unknown. Unknown → fall back to dark
        # which matches our historical default.
        scheme = hints.colorScheme()
        if scheme == Qt.ColorScheme.Light:
            return "light"
        return "dark"
    except Exception:
        return "dark"


def get_qss(theme: str = "auto") -> str:
    """Return the QSS string for a settings value.

    theme: "auto" (follow OS), "dark", "light". Unknown values fall
    back to dark (forgiving: a bad/missing setting never errors the UI).
    """
    if theme == "light":
        return LIGHT_QSS
    if theme == "dark":
        return DARK_QSS
    # auto / anything else
    return LIGHT_QSS if _detect_system_theme() == "light" else DARK_QSS


def get_active_palette(theme: str = "auto") -> Palette:
    """Same selector logic as get_qss but returns the Palette dataclass
    so non-QSS UI code (custom QPainter widgets like Sparkline, plus the
    upcoming map / bandwidth-graph in v1.14/v1.15) can pick colors that
    match the active theme.
    """
    if theme == "light":
        return LIGHT_PALETTE
    if theme == "dark":
        return DARK_PALETTE
    return LIGHT_PALETTE if _detect_system_theme() == "light" else DARK_PALETTE
