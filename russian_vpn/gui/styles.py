"""Dark theme stylesheet for the app."""

DARK_QSS = """
* {
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 10pt;
    color: #e4e4e7;
}

QMainWindow, QDialog {
    background-color: #18181b;
}

QLabel {
    color: #e4e4e7;
}

QLabel#statusBadge, QLabel#statusBadgeOn, QLabel#statusBadgeOff {
    padding: 4px 12px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 9pt;
}

QLabel#statusBadgeOn {
    background-color: #14532d;
    color: #86efac;
}

QLabel#statusBadgeOff {
    background-color: #3f3f46;
    color: #a1a1aa;
}

QLabel#sectionTitle {
    font-size: 11pt;
    font-weight: 600;
    color: #f4f4f5;
    padding-top: 4px;
}

QLabel#muted {
    color: #71717a;
    font-size: 9pt;
}

QPushButton {
    background-color: #27272a;
    color: #f4f4f5;
    border: 1px solid #3f3f46;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: 500;
}

QPushButton:hover {
    background-color: #3f3f46;
    border-color: #52525b;
}

QPushButton:pressed {
    background-color: #18181b;
}

QPushButton:disabled {
    color: #52525b;
    background-color: #1f1f23;
    border-color: #27272a;
}

QPushButton#primary {
    background-color: #16a34a;
    color: white;
    border: none;
}

QPushButton#primary:hover {
    background-color: #15803d;
}

QPushButton#primary:pressed {
    background-color: #166534;
}

QPushButton#danger {
    background-color: #dc2626;
    color: white;
    border: none;
}

QPushButton#danger:hover {
    background-color: #b91c1c;
}

QListWidget, QTextEdit, QPlainTextEdit, QLineEdit {
    background-color: #0a0a0a;
    border: 1px solid #27272a;
    border-radius: 6px;
    padding: 4px;
    selection-background-color: #16a34a;
    selection-color: white;
}

QListWidget::item {
    padding: 8px;
    border-bottom: 1px solid #1f1f23;
}

QListWidget::item:selected {
    background-color: #1c2a1f;
    color: #86efac;
}

QListWidget::item:hover {
    background-color: #1f1f23;
}

QTextEdit, QPlainTextEdit {
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 9pt;
}

QLineEdit {
    padding: 6px 10px;
}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border-color: #16a34a;
}

QGroupBox {
    border: 1px solid #27272a;
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 14px;
}

QGroupBox::title {
    color: #a1a1aa;
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 8px;
}

QMenuBar {
    background-color: #18181b;
    border-bottom: 1px solid #27272a;
}

QMenuBar::item:selected {
    background-color: #27272a;
}

QMenu {
    background-color: #27272a;
    border: 1px solid #3f3f46;
}

QMenu::item:selected {
    background-color: #16a34a;
}

QScrollBar:vertical {
    background: #18181b;
    width: 10px;
    border: none;
}

QScrollBar::handle:vertical {
    background: #3f3f46;
    border-radius: 5px;
    min-height: 20px;
}

QScrollBar::handle:vertical:hover {
    background: #52525b;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
"""
