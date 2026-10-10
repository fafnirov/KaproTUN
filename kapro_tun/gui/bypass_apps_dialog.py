"""The user's list of programs that skip the VPN.

The generic mechanism behind the built-in games bypass: sing-box matches these
executable names with a `process_name` rule and routes them to `direct`, so a
latency-sensitive or geo-pinned program talks to the internet on the real
connection while everything else keeps tunnelling.

The engine matches a bare file name, case-insensitively. A full path is the
most common thing people paste, so one is accepted — and cut down to its name.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ..core.i18n import tr
from . import kit, tokens
from .icons_v2 import IconLabel

_ROW_H = tokens.ROW_H_SM
_ROWS_SHOWN = 5


def normalize(raw: str) -> list:
    """Text -> a clean, de-duplicated, sorted list of executable names.

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


class BypassAppsDialog(kit.OverlayDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent, wide=True)
        self.scrim_dismisses = False
        self._manager = manager
        self._apps: list[str] = normalize("\n".join(manager.settings.get("bypass_apps", []) or []))
        self.head("apps", tr("bypass.window_title"), tr("bypass.intro_short"))

        entry = QHBoxLayout()
        entry.setSpacing(tokens.SP_2)
        self.edit = kit.Input(tr("bypass.placeholder"))
        self.edit.returnPressed.connect(self._on_add)
        entry.addWidget(self.edit, stretch=1)
        self.browse_btn = kit.Button("", "secondary", icon="file", tooltip=tr("bypass.browse_tip"))
        self.browse_btn.clicked.connect(self._on_browse)
        entry.addWidget(self.browse_btn)
        self.add_btn = kit.Button(tr("bypass.add"), "secondary", icon="plus")
        self.add_btn.clicked.connect(self._on_add)
        entry.addWidget(self.add_btn)
        self.body.addLayout(entry)

        head = QHBoxLayout()
        head.addWidget(kit.label(tr("bypass.in_list"), "label"), stretch=1)
        self.count = kit.label("", "count")
        head.addWidget(self.count)
        self.body.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("ktMiniList")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("ktMiniListBody")
        self._rows = QVBoxLayout(inner)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(0)
        self._rows.addStretch(1)
        self.scroll.setWidget(inner)
        self.add_widget(self.scroll)
        self.add_widget(kit.label(tr("bypass.hint_short"), "hint", wrap=True))

        self.add_actions(
            [("cancel", tr("sites.cancel"), "secondary"), ("save", tr("sites.save"), "primary")],
            handlers={"cancel": self.reject, "save": self._on_save})
        self._rebuild()
        self.edit.setFocus()

    def apps(self) -> list[str]:
        return list(self._apps)

    def _rebuild(self) -> None:
        while self._rows.count() > 1:
            item = self._rows.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(None)
        for i, name in enumerate(self._apps):
            row = QFrame()
            row.setObjectName("ktAppRow")
            row.setProperty("last", "true" if i == len(self._apps) - 1 else "false")
            row.setFixedHeight(_ROW_H)
            line = QHBoxLayout(row)
            line.setContentsMargins(tokens.SP_3, 0, tokens.SP_1, 0)
            line.setSpacing(tokens.SP_2H)
            line.addWidget(IconLabel("apps", tokens.ICON_SM, "text_tertiary"))
            text = kit.ElidedLabel(name)
            text.setObjectName("ktMono")
            line.addWidget(text, stretch=1)
            remove = kit.Button("", "ghost", icon="trash", size="sm", tooltip=tr("bypass.remove_tip"))
            remove.clicked.connect(lambda _c=False, n=name: self._remove(n))
            line.addWidget(remove)
            self._rows.insertWidget(i, row)
        self.count.setText(str(len(self._apps)))
        shown = max(1, min(len(self._apps), _ROWS_SHOWN))
        self.scroll.setFixedHeight(shown * _ROW_H + 2)

    def _add(self, raw: str) -> None:
        self._apps = sorted(set(self._apps) | set(normalize(raw)))
        self._rebuild()

    def _remove(self, name: str) -> None:
        self._apps = [a for a in self._apps if a != name]
        self._rebuild()

    def _on_add(self) -> None:
        if self.edit.text().strip():
            self._add(self.edit.text())
            self.edit.clear()

    def _on_browse(self) -> None:
        path, _sel = QFileDialog.getOpenFileName(self, tr("bypass.browse_tip"), "",
                                                 tr("bypass.browse_filter"))
        if path:
            self._add(path)

    def _on_save(self) -> None:
        # What is still in the field counts: typing a name and pressing Save
        # without "Add" first is what people do.
        if self.edit.text().strip():
            self._add(self.edit.text())
        self._manager.update_settings(bypass_apps=list(self._apps))
        self.accept()
