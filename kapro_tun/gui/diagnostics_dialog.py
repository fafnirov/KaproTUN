"""Network diagnostics — one place that answers "what is my networking
actually doing right now?".

Exists because diagnosing a TUN VPN by hand is booby-trapped: `ping` and
`tracert` are answered locally by the userspace stack, so they report <1 ms and
a single hop and tell you nothing (see net_diag.ICMP_NOTE). This runs the tests
that DO measure the real data path (TCP connect, a real UDP round-trip, and the
proxy outbound) and dumps the adapter/route facts next to them.

The collection takes a few seconds and runs off the UI thread; closing the
dialog does not wait for it (background.py).
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication

from ..core import net_diag
from ..core.i18n import tr
from . import kit
from .background import Background


class _Collect(Background):
    done = Signal(object)   # net_diag.Snapshot

    def __init__(self, manager):
        super().__init__()
        self._manager = manager

    def _work(self) -> Callable[[], None]:
        try:
            snap = net_diag.collect(self._manager)
        except Exception as e:                       # report it, do not die
            snap = net_diag.Snapshot()
            snap.errors.append(f"collect: {type(e).__name__}: {e}")
        return lambda: self.done.emit(snap)


class DiagnosticsDialog(kit.OverlayDialog):
    """The snapshot as text that can be copied, with a button to take it again."""

    def __init__(self, manager, parent=None):
        super().__init__(parent, wide=True)
        self._manager = manager
        self._worker = None
        self.head("gauge", tr("diag.window_title"), tr("diag.intro"))
        self.output = kit.Report(height=300)
        self.add_widget(self.output)
        self.add_actions(
            [("close", tr("leak.close_btn"), "secondary"), ("copy", tr("diag.copy_btn"), "primary")],
            default="copy", left=(("rerun", tr("diag.rerun_btn"), "ghost"),),
            icons={"copy": "copy", "rerun": "refresh"},
            handlers={"close": self.reject, "copy": self._on_copy, "rerun": self._start})
        self.copy_btn, self.rerun_btn = self.buttons["copy"], self.buttons["rerun"]
        self._start()

    def _start(self) -> None:
        if self._worker is not None:
            return
        self.rerun_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)
        self.copy_btn.setText(tr("diag.copy_btn"))
        self.output.setPlainText(tr("diag.collecting"))
        self._worker = _Collect(self._manager)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, snap) -> None:
        self._worker = None
        try:
            self.output.setPlainText(net_diag.format_report(snap))
        except Exception as e:
            self.output.setPlainText(f"{type(e).__name__}: {e}")
        self.rerun_btn.setEnabled(True)
        self.copy_btn.setEnabled(True)

    def _on_copy(self) -> None:
        QApplication.clipboard().setText(self.output.toPlainText())
        self.copy_btn.setText(tr("diag.copied"))
