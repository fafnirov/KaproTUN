"""Application-wide dark theme with amber accent (AmneziaVPN-inspired)."""

# Palette — keep these in sync with QSS below if you change them.
BG = "#0e0e10"
SURFACE = "#18181b"
SURFACE_HI = "#27272a"
BORDER = "#2a2a2d"
TEXT = "#fafafa"
TEXT_MUTED = "#a1a1aa"
TEXT_DIM = "#71717a"
ACCENT = "#f59e0b"           # amber-500
ACCENT_HI = "#fbbf24"        # amber-400
ACCENT_DIM = "#78350f"       # amber-900
DANGER = "#ef4444"

DARK_QSS = f"""
* {{
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 10pt;
    color: {TEXT};
}}

QMainWindow, QDialog {{
    background-color: {BG};
}}

QWidget#page {{
    background-color: {BG};
}}

QWidget#appShell {{
    background-color: {BG};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}

/* --- custom title bar --- */

QFrame#titleBar {{
    background-color: {SURFACE};
    border: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    border-bottom: 1px solid {BORDER};
}}

QLabel#titleBarText {{
    color: {TEXT};
    font-weight: 600;
    font-size: 9pt;
}}

QPushButton#titleBarBtn {{
    background-color: transparent;
    border: none;
    border-radius: 0;
    color: {TEXT_MUTED};
    font-size: 11pt;
    font-weight: 400;
    padding: 0;
}}

QPushButton#titleBarBtn:hover {{
    background-color: {SURFACE_HI};
    color: {TEXT};
}}

QPushButton#titleBarCloseBtn {{
    background-color: transparent;
    border: none;
    border-radius: 0;
    border-top-right-radius: 10px;
    color: {TEXT_MUTED};
    font-size: 11pt;
    font-weight: 400;
    padding: 0;
}}

QPushButton#titleBarCloseBtn:hover {{
    background-color: {DANGER};
    color: white;
}}

/* --- generic text helpers --- */

QLabel#h1   {{ font-size: 18pt; font-weight: 600; }}
QLabel#h2   {{ font-size: 13pt; font-weight: 600; }}
QLabel#muted {{ color: {TEXT_MUTED}; font-size: 9pt; }}
QLabel#dim  {{ color: {TEXT_DIM};  font-size: 9pt; }}

/* --- buttons --- */

QPushButton {{
    background-color: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 500;
}}

QPushButton:hover {{
    background-color: #3a3a3d;
}}

QPushButton:pressed {{
    background-color: #18181b;
}}

QPushButton:disabled {{
    color: {TEXT_DIM};
    background-color: {SURFACE};
    border-color: {BORDER};
}}

QPushButton#primary {{
    background-color: {ACCENT};
    color: #1a1209;
    border: none;
}}

QPushButton#primary:hover {{
    background-color: {ACCENT_HI};
}}

QPushButton#danger {{
    background-color: transparent;
    color: {DANGER};
    border: 1px solid {DANGER};
}}

QPushButton#danger:hover {{
    background-color: rgba(239, 68, 68, 0.1);
}}

/* --- circular connect button --- */

QPushButton#circleBtn {{
    background-color: transparent;
    border: 3px solid {BORDER};
    border-radius: 115px;        /* must equal width/2 = height/2 */
    min-width: 230px; max-width: 230px;
    min-height: 230px; max-height: 230px;
    color: {TEXT_MUTED};
    font-size: 16pt;
    font-weight: 600;
    letter-spacing: 2px;
}}

QPushButton#circleBtn:hover {{
    border-color: {TEXT_MUTED};
    color: {TEXT};
}}

/* Mouse-down feedback: brighter ring, thicker border — instant tactile cue
   before the burst animation kicks in. */
QPushButton#circleBtn:pressed {{
    border: 4px solid {ACCENT_HI};
    color: {ACCENT_HI};
}}

QPushButton#circleBtn[state="connecting"] {{
    border-color: {ACCENT_DIM};
    color: {ACCENT};
}}

QPushButton#circleBtn[state="connected"] {{
    border-color: {ACCENT};
    color: {ACCENT};
}}

QPushButton#circleBtn[state="connecting"]:pressed,
QPushButton#circleBtn[state="connected"]:pressed {{
    border: 4px solid {ACCENT_HI};
    color: {ACCENT_HI};
}}

/* --- icon buttons (nav bar, card chevron) --- */

QPushButton#iconBtn {{
    background-color: transparent;
    border: none;
    border-radius: 8px;
    padding: 10px;
    font-size: 16pt;
    color: {TEXT_MUTED};
    min-width: 48px;
}}

QPushButton#iconBtn:hover {{
    background-color: {SURFACE_HI};
    color: {TEXT};
}}

QPushButton#iconBtn[active="true"] {{
    color: {ACCENT};
}}

/* --- active config card on home --- */

QFrame#configCard {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}

QFrame#configCard:hover {{
    border-color: {ACCENT_DIM};
}}

QFrame#configCard QLabel#cardTitle {{
    font-size: 13pt;
    font-weight: 600;
}}

QFrame#configCard QLabel#cardSub {{
    color: {TEXT_DIM};
    font-size: 9pt;
}}

QFrame#configCard QLabel#cardBadge {{
    background-color: {SURFACE_HI};
    color: {TEXT_MUTED};
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 9pt;
    font-weight: 500;
}}

/* --- list widget (configs page) --- */

QListWidget {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
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
    background-color: {ACCENT_DIM};
    color: {ACCENT_HI};
}}

QListWidget::item:hover:!selected {{
    background-color: {SURFACE_HI};
}}

/* --- inputs --- */

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 12px;
    selection-background-color: {ACCENT};
    selection-color: #1a1209;
}}

QPlainTextEdit, QTextEdit {{
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 9pt;
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}

/* --- checkbox --- */

QCheckBox {{
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 18px; height: 18px;
    border-radius: 4px;
    border: 1px solid {BORDER};
    background-color: {SURFACE};
}}

QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}

/* --- menus --- */

QMenuBar {{
    background-color: {BG};
    border-bottom: 1px solid {BORDER};
}}

QMenuBar::item:selected {{
    background-color: {SURFACE_HI};
}}

QMenu {{
    background-color: {SURFACE_HI};
    border: 1px solid {BORDER};
    padding: 4px;
}}

QMenu::item {{
    padding: 6px 16px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background-color: {ACCENT_DIM};
    color: {ACCENT};
}}

/* --- scrollbars --- */

QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    border: none;
}}

QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 4px;
    min-height: 24px;
}}

QScrollBar::handle:vertical:hover {{
    background: {TEXT_DIM};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

/* --- separator --- */

QFrame[frameShape="4"] {{      /* HLine */
    color: {BORDER};
    max-height: 1px;
}}
"""
