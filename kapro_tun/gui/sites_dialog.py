"""The list of domains that always go direct, past the VPN: one per line."""
from __future__ import annotations

from PySide6.QtWidgets import QPlainTextEdit

from ..core import storage
from ..core.i18n import tr
from . import kit
from .home_v2 import plural_domains


def parse_sites(text: str) -> list[str]:
    """Editor text → the list to save: no blank lines, no comment lines."""
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


class SitesDialog(kit.OverlayDialog):
    def __init__(self, parent=None):
        super().__init__(parent, wide=True)
        self.scrim_dismisses = False
        self.head("route", tr("set.sites"), tr("sites.intro"))
        self.editor = kit.TextArea(height=220)
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setPlainText("\n".join(storage.load_sites()))
        self.add_widget(self.editor)
        self.count = kit.label("", "hint")
        self.add_widget(self.count)
        self.add_actions(
            [("cancel", tr("sites.cancel"), "secondary"), ("save", tr("sites.save"), "primary")],
            left=(("reset", tr("sites.reset_short"), "ghost"),),
            icons={"reset": "refresh"},
            handlers={"cancel": self.reject, "save": self._on_save, "reset": self._on_reset})
        self.editor.textChanged.connect(self._on_changed)
        self._on_changed()
        self.editor.setFocus()

    def _on_changed(self) -> None:
        self.count.setText(plural_domains(len(parse_sites(self.editor.toPlainText()))))

    def _on_save(self) -> None:
        storage.save_sites(parse_sites(self.editor.toPlainText()))
        self.accept()

    def _on_reset(self) -> None:
        if kit.confirm(self, "refresh", tr("sites.reset_confirm"), "",
                       ok_label=tr("sites.reset_ok"), cancel_label=tr("sites.cancel")):
            self.editor.setPlainText("\n".join(storage.reset_sites_to_default()))
