"""Design tokens of the v2 interface ("Пульт", KaproTUN 4.1).

One place for every colour and size the interface uses. Widgets never carry a
literal colour: QSS is generated from these (styles.py), and custom-painted
widgets read `colors()` at paint time, so both follow the theme.

Before v2 a number of widgets hard-coded the dark palette (status line, the
"direct sites" count, the public-IP line), which is why the light theme had
white text on a white background in places.

Sizes are in logical pixels.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Colors:
    bg: str
    surface: str
    surface_2: str
    surface_hover: str
    surface_pressed: str
    input_bg: str
    line: str
    line_strong: str
    line_hover: str
    text: str
    text_secondary: str
    text_tertiary: str
    text_disabled: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    accent_soft: str
    accent_line: str
    on_accent: str
    focus: str
    glow: tuple[int, int, int, int]      # r, g, b, alpha 0-255
    danger: str
    danger_text: str
    danger_soft: str
    danger_line: str
    on_danger: str
    success: str
    success_text: str
    success_soft: str
    success_line: str
    warning: str
    switch_off: str
    switch_off_hover: str
    switch_knob: str
    seg_selected: str
    scrim: tuple[int, int, int, int]
    shadow: tuple[int, int, int, int]
    chart_up: str


DARK = Colors(
    bg="#0e0e10", surface="#18181b", surface_2="#1f1f23",
    surface_hover="#27272a", surface_pressed="#323236", input_bg="#141417",
    line="#2a2a2d", line_strong="#3a3a3d", line_hover="#52525b",
    text="#fafafa", text_secondary="#a1a1aa", text_tertiary="#8b8b94",
    text_disabled="#52525b",
    accent="#f59e0b", accent_hover="#fbbf24", accent_pressed="#d97706",
    accent_text="#f59e0b", accent_soft="#261c0b", accent_line="#6b4a12",
    on_accent="#1a1209", focus="#fbbf24", glow=(245, 158, 11, 82),
    danger="#ef4444", danger_text="#f87171", danger_soft="#2a1414",
    danger_line="#7f1d1d", on_danger="#ffffff",
    success="#22c55e", success_text="#4ade80", success_soft="#0f2417",
    success_line="#14532d", warning="#f59e0b",
    switch_off="#3a3a3d", switch_off_hover="#52525b", switch_knob="#fafafa",
    seg_selected="#2f2f33", scrim=(0, 0, 0, 153), shadow=(0, 0, 0, 128),
    chart_up="#71717a",
)

LIGHT = Colors(
    bg="#fafaf9", surface="#ffffff", surface_2="#f5f5f4",
    surface_hover="#e7e5e4", surface_pressed="#d6d3d1", input_bg="#ffffff",
    line="#e7e5e4", line_strong="#d6d3d1", line_hover="#a8a29e",
    text="#18181b", text_secondary="#57534e", text_tertiary="#6f6862",
    text_disabled="#a8a29e",
    accent="#f59e0b", accent_hover="#d97706", accent_pressed="#b45309",
    accent_text="#b45309", accent_soft="#fef3c7", accent_line="#fcd34d",
    on_accent="#1a1209", focus="#b45309", glow=(245, 158, 11, 71),
    danger="#dc2626", danger_text="#b91c1c", danger_soft="#fee2e2",
    danger_line="#fca5a5", on_danger="#ffffff",
    success="#15803d", success_text="#15803d", success_soft="#dcfce7",
    success_line="#86efac", warning="#b45309",
    switch_off="#d6d3d1", switch_off_hover="#a8a29e", switch_knob="#ffffff",
    seg_selected="#ffffff", scrim=(28, 25, 23, 82), shadow=(28, 25, 23, 41),
    chart_up="#a8a29e",
)

# --- type ------------------------------------------------------------------
FONT = '"Segoe UI", "Inter", sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", monospace'
FS_XS, FS_SM, FS_MD, FS_LG, FS_XL, FS_METRIC = 11, 12, 13, 15, 20, 24
LS_CAPS = 1.5

# --- spacing / radii -------------------------------------------------------
SP_HALF, SP_1, SP_1H, SP_2, SP_2H, SP_3, SP_3H = 2, 4, 6, 8, 10, 12, 14
SP_4, SP_5, SP_6, SP_8, SP_10 = 16, 20, 24, 32, 40
R_SM, R_MD, R_LG, R_WINDOW = 6, 10, 14, 10

# --- geometry --------------------------------------------------------------
WIN_W, WIN_H = 460, 720
TITLEBAR_H, TITLEBAR_BTN_W = 36, 44
NAV_H, NAV_IND_W, NAV_IND_H = 64, 28, 2
PAGE_PAD_X, PAGE_PAD_Y = 24, 16
CONTROL_H_SM, CONTROL_H, CONTROL_H_LG = 32, 36, 44
ROW_H_SM, ROW_H, SERVER_H = 44, 56, 60
CHIP, CHIP_SM, BADGE_H = 36, 32, 20
ICON_XS, ICON_SM, ICON_MD, ICON_LG, ICON_XL = 12, 16, 20, 24, 40
RING, RING_COMPACT, RING_BORDER = 184, 152, 3
DOT, DOT_SM = 8, 6
TOAST_BOTTOM = 80

# --- motion ----------------------------------------------------------------
DUR_FAST, DUR_BASE, DUR_PULSE = 120, 200, 1400
GLOW_CONNECTED, GLOW_LOW = 36.0, 12.0

_theme = "dark"


def set_theme(theme: str) -> str:
    """Record the theme in force ("dark" / "light"; "auto" follows the OS).
    Called wherever the application stylesheet is (re)applied. Returns the
    resolved name."""
    global _theme
    if theme not in ("dark", "light"):
        from . import styles
        theme = styles._detect_system_theme()
    _theme = theme
    return _theme


def theme() -> str:
    return _theme


def colors() -> Colors:
    return LIGHT if _theme == "light" else DARK


def for_theme(name: str) -> Colors:
    return LIGHT if name == "light" else DARK
