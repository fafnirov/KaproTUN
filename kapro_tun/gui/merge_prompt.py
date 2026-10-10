"""The one way servers get into the saved list — with the user asked whenever
an incoming server would take over a saved one that came from somewhere else.

Every entry point uses merge_with_prompt(): subscription import (from
Servers, Settings, the home banner, the empty home screen), subscription
refresh, and adding a single server by hand. They used to carry their own copies of a
"replace by name" loop; migrating one and missing another left the hole open
on the path most people actually use.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

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


def conflict_lines(conflicts: list) -> list[tuple[str, str, str]]:
    """(name, saved address, incoming address) for each conflict shown. When
    the address is the same and something else differs (a key, a transport),
    the third value says so instead of repeating the address."""
    from .home_v2 import strip_flag
    out = []
    for saved, new in conflicts[:_MAX_SHOWN]:
        old, now = _where(saved), _where(new)
        out.append((one_line(strip_flag(saved.name), 40), old,
                    tr("conflict.same_addr") if now == old else now))
    return out


def build_conflict_dialog(parent: Optional[QWidget], conflicts: list):
    """The question shown over the window. Every value in it is the sender's
    text and is shown as text, one line each."""
    from . import kit, tokens
    from .home_v2 import ElidedLabel
    from .icons_v2 import IconLabel

    dlg = kit.OverlayDialog(parent, wide=True)
    dlg.head("alert-triangle", tr("picker.conflict_title"), tr("conflict.intro"), tone="accent")
    diff = QFrame()
    diff.setObjectName("ktDiff")
    col = QVBoxLayout(diff)
    col.setContentsMargins(tokens.SP_3, tokens.SP_2H, tokens.SP_3, tokens.SP_2H)
    col.setSpacing(tokens.SP_1H)
    font = QFont(QApplication.font())
    font.setPixelSize(tokens.FS_SM)
    font.setWeight(QFont.DemiBold)
    metrics = QFontMetrics(font)
    lines = conflict_lines(conflicts)
    # One width for every name, so the addresses line up in columns.
    name_w = min(120, max(metrics.horizontalAdvance(name + ":") for name, _o, _n in lines) + 4)
    for name, old, new in lines:
        row = QHBoxLayout()
        row.setSpacing(tokens.SP_1H)
        cells = []
        for text, role in ((name + ":", "ktDiffName"), (old, "ktDiffOld"), (new, "ktDiffNew")):
            cell = ElidedLabel(text)
            cell.setObjectName(role)
            cells.append(cell)
        # Fixed, not the label's own "take whatever is left": the two
        # addresses share the rest of the row.
        cells[0].setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        cells[0].setFixedWidth(name_w)
        row.addWidget(cells[0])
        row.addWidget(cells[1], stretch=1)
        row.addWidget(IconLabel("chevron-right", tokens.ICON_XS, "text_tertiary"))
        row.addWidget(cells[2], stretch=1)
        col.addLayout(row)
    if len(conflicts) > _MAX_SHOWN:
        col.addWidget(kit.label(tr("picker.conflict_more", n=len(conflicts) - _MAX_SHOWN), "diffMore"))
    dlg.add_widget(diff)
    dlg.add_text(tr("conflict.advice"))
    dlg.add_actions([("replace", tr("picker.conflict_replace"), "secondary"),
                     ("keep", tr("picker.conflict_keep"), "primary")], default="keep")
    return dlg


def ask_replace_conflicts(parent: Optional[QWidget], conflicts: list) -> bool:
    """True = replace the saved servers; False = keep both. "Keep both" is the
    default and what Escape, Enter and a click outside the dialog mean: it is
    the answer that cannot redirect anyone's traffic."""
    return build_conflict_dialog(parent, conflicts).ask() == "replace"


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
