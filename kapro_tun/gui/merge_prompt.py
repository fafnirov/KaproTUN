"""The one way servers get into the saved list — with the user asked whenever
an incoming server would take over a saved one that came from somewhere else.

Every entry point uses merge_with_prompt(): subscription import (from the
picker, Settings, the home banner, onboarding), subscription refresh, and
adding a single server by hand. They used to carry their own copies of a
"replace by name" loop; migrating one and missing another left the hole open
on the path most people actually use.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

from ..core.i18n import tr
from ..core.parser import ProxyConfig
from ..core.subscription import merge_configs

_MAX_SHOWN = 8


def one_line(value: object, limit: int = 60) -> str:
    """A sender-controlled value fit to be one item of a list in a prompt: no
    line breaks or control characters (it could otherwise write a paragraph of
    its own — "All good, press Replace"), and bounded in length."""
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(value)).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _where(cfg: ProxyConfig) -> str:
    return one_line(f"{cfg.outbound.get('server', '?')}:{cfg.outbound.get('server_port', '?')}")


def ask_replace_conflicts(parent: Optional[QWidget], conflicts: list) -> bool:
    """True = replace the saved servers; False = keep both. "Keep both" is the
    default and what Escape, Enter and closing the window mean: it is the
    answer that cannot redirect anyone's traffic."""
    shown = [f"• {one_line(saved.name, 40)}: {_where(saved)} → {_where(new)}"
             for saved, new in conflicts[:_MAX_SHOWN]]
    if len(conflicts) > _MAX_SHOWN:
        shown.append(tr("picker.conflict_more", n=len(conflicts) - _MAX_SHOWN))
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(tr("picker.conflict_title"))
    box.setTextFormat(Qt.PlainText)
    box.setText(tr("picker.conflict_body", items="\n".join(shown)))
    keep = box.addButton(tr("picker.conflict_keep"), QMessageBox.AcceptRole)
    swap = box.addButton(tr("picker.conflict_replace"), QMessageBox.DestructiveRole)
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    box.exec()
    return box.clickedButton() is swap


def merge_with_prompt(
    parent: Optional[QWidget],
    existing: list[ProxyConfig],
    incoming: list[ProxyConfig],
    ask: Optional[Callable[[list], bool]] = None,
) -> tuple[list[ProxyConfig], int, int]:
    """Merge `incoming` into `existing`; returns (new list, added, updated).
    Does not modify `existing`. `ask` replaces the dialog (tests, callers that
    own their own prompt)."""
    res = merge_configs(existing, incoming)
    if not res.conflicts:
        return res.configs, res.added, res.updated
    decide = ask or (lambda conflicts: ask_replace_conflicts(parent, conflicts))
    if decide(res.conflicts):
        return res.replace(), res.added, res.updated + len(res.conflicts)
    return res.keep_both(), res.added + len(res.conflicts), res.updated
