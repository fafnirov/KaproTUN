"""The list of domains that always go direct, past the VPN: one per line."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from PySide6.QtWidgets import QPlainTextEdit

from ..core import storage
from ..core.i18n import tr
from . import kit
from .home_v2 import plural_domains


def parse_sites(text: str) -> list[str]:
    """Editor text → the list to save: no blank lines, no comment lines."""
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def normalized_sites(text: str) -> list[str]:
    domains = set()
    for raw in parse_sites(text):
        value = raw.lower().strip().removeprefix("*.").removeprefix(".")
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
                raise ValueError(raw)
            value = parsed.hostname or ""
        value = value.rstrip(".")
        try:
            value = value.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(raw) from None
        if len(value) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p)
                                      for p in value.split(".")):
            raise ValueError(raw)
        domains.add(value)
    return sorted(domains)


class SitesDialog(kit.OverlayDialog):
    def __init__(self, parent=None):
        super().__init__(parent, wide=True)
        self.scrim_dismisses = False
        self.head("route", tr("route.sites"), tr("route.intro"))
        self.add_widget(kit.label(tr("route.direct"), "label", wrap=True))
        self.editor = kit.TextArea(height=100)
        self.editor.setAccessibleName(tr("route.direct"))
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setPlainText("\n".join(storage.load_sites()))
        self.add_widget(self.editor)
        self.add_widget(kit.label(tr("route.proxy"), "label", wrap=True))
        self.proxy_editor = kit.TextArea(height=100)
        self.proxy_editor.setAccessibleName(tr("route.proxy"))
        self.proxy_editor.setPlainText("\n".join(storage.load_settings().get("proxy_sites", []) or []))
        self.add_widget(self.proxy_editor)
        self.add_widget(kit.label(tr("route.precedence"), "hint", wrap=True))
        if storage.load_settings().get("kill_switch", False):
            self.add_widget(kit.label(tr("route.lockdown"), "hint", wrap=True))
        self.count = kit.label("", "hint", wrap=True)
        self.add_widget(self.count)
        self.add_actions(
            [("cancel", tr("sites.cancel"), "secondary"), ("save", tr("sites.save"), "primary")],
            left=(("reset", tr("sites.reset_short"), "ghost"),),
            icons={"reset": "refresh"},
            handlers={"cancel": self.reject, "save": self._on_save, "reset": self._on_reset})
        self.enable_scrolling()
        self.editor.textChanged.connect(self._on_changed)
        self.proxy_editor.textChanged.connect(self._on_changed)
        self._on_changed()
        self.editor.setFocus()

    def _on_changed(self) -> None:
        direct = len(parse_sites(self.editor.toPlainText()))
        proxy = len(parse_sites(self.proxy_editor.toPlainText()))
        self.count.setText(tr("route.count", direct=direct, proxy=proxy) if proxy else plural_domains(direct))

    def _on_save(self) -> None:
        try:
            direct = normalized_sites(self.editor.toPlainText())
            proxy = normalized_sites(self.proxy_editor.toPlainText())
        except ValueError:
            self.count.setText(tr("route.invalid"))
            return
        if set(direct) & set(proxy):
            self.count.setText(tr("route.duplicate"))
            return
        storage.save_sites(direct)
        storage.update_settings({"proxy_sites": proxy})
        self.accept()

    def _on_reset(self) -> None:
        if kit.confirm(self, "refresh", tr("sites.reset_confirm"), "",
                       ok_label=tr("sites.reset_ok"), cancel_label=tr("sites.cancel")):
            # Reset only the draft: Cancel must not change the saved routes.
            import json

            from ..core import paths
            data = json.loads(paths.bundled_default_sites().read_text(encoding="utf-8"))
            self.editor.setPlainText("\n".join(data.get("sites", [])))
