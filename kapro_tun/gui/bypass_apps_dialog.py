"""Editor for the user's bypass-application list (apps that skip the VPN).

The generic mechanism behind the built-in games bypass: sing-box matches these
executable names with a `process_name` rule and routes them to `direct`, so a
latency-sensitive or geo-pinned app talks to the internet on the real
connection while everything else keeps tunnelling.

One executable name per line ("game.exe"). Names are matched case-insensitively;
paths are intentionally NOT accepted here — a bare name is what the engine
matches, and pasting a full path is the most common way users make the rule
silently miss.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from ..core.i18n import tr


def normalize(raw: str) -> list:
    """Text box -> a clean, de-duplicated, sorted list of executable names.

    Tolerates what users actually paste: full paths (we keep the file name),
    quotes, stray whitespace, blank lines, and a missing .exe suffix on
    Windows. Returns lowercase names because the engine match is
    case-insensitive and dupes-by-case would be confusing in the config."""
    out = set()
    for line in (raw or "").splitlines():
        name = line.strip().strip('"').strip("'")
        if not name or name.startswith("#"):
            continue
        # A pasted path ("C:\\Games\\x\\game.exe") -> just the executable.
        for sep in ("\\", "/"):
            if sep in name:
                name = name.rsplit(sep, 1)[-1]
        name = name.strip().lower()
        if not name:
            continue
        if "." not in name:
            name = f"{name}.exe"
        out.add(name)
    return sorted(out)


class BypassAppsDialog(QDialog):
    """Edit the list of apps that bypass the VPN (one .exe per line)."""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self.setWindowTitle(tr("bypass.window_title"))
        self.resize(520, 560)

        layout = QVBoxLayout(self)
        intro = QLabel(tr("bypass.intro"))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        hint = QLabel(tr("bypass.hint"))
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        current = list(manager.settings.get("bypass_apps", []) or [])
        self.edit = QPlainTextEdit("\n".join(current))
        self.edit.setPlaceholderText("discord.exe\ndota2.exe")
        layout.addWidget(self.edit, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_save(self) -> None:
        apps = normalize(self.edit.toPlainText())
        self._manager.update_settings(bypass_apps=apps)
        self.accept()
