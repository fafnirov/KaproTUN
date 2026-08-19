"""Network Diagnostics screen — one place that answers "what is my networking
actually doing right now?".

Exists because diagnosing a TUN VPN by hand is booby-trapped: `ping` and
`tracert` are answered locally by the userspace stack, so they report <1 ms and
a single hop and tell you nothing (see net_diag.ICMP_NOTE). This runs the tests
that DO measure the real data path (TCP connect, a real UDP round-trip, and the
proxy outbound) and dumps the adapter/route facts next to them.

Collection runs on a worker thread — the probes are bounded but can take a few
seconds, and blocking the GUI thread here is how you get "the app froze".
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..core import net_diag
from ..core.i18n import tr


class _CollectWorker(QThread):
    """Runs net_diag.collect() off the GUI thread."""

    done = Signal(object)   # net_diag.Snapshot

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._manager = manager

    def run(self) -> None:
        try:
            snap = net_diag.collect(self._manager)
        except Exception as e:                       # never kill the dialog
            snap = net_diag.Snapshot()
            snap.errors.append(f"collect: {type(e).__name__}: {e}")
        self.done.emit(snap)


class DiagnosticsDialog(QDialog):
    """Shows the snapshot as copy-pasteable text, with a re-run button."""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._worker: _CollectWorker | None = None

        self.setWindowTitle(tr("diag.window_title"))
        self.resize(640, 620)

        layout = QVBoxLayout(self)
        intro = QLabel(tr("diag.intro"))
        intro.setObjectName("dim")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlainText(tr("diag.collecting"))
        # Monospace so the aligned report columns stay aligned.
        self.output.setStyleSheet(
            'font-family: "Cascadia Mono", "Consolas", monospace; font-size: 9pt;')
        layout.addWidget(self.output, stretch=1)

        self.copy_btn = QPushButton(tr("diag.copy_btn"))
        self.copy_btn.clicked.connect(self._on_copy)
        self.rerun_btn = QPushButton(tr("diag.rerun_btn"))
        self.rerun_btn.clicked.connect(self._start)

        buttons = QDialogButtonBox()
        buttons.addButton(self.copy_btn, QDialogButtonBox.ActionRole)
        buttons.addButton(self.rerun_btn, QDialogButtonBox.ActionRole)
        buttons.addButton(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._start()

    # --- collection -------------------------------------------------------

    def _start(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.rerun_btn.setEnabled(False)
        self.output.setPlainText(tr("diag.collecting"))
        self._worker = _CollectWorker(self._manager, parent=self)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, snap) -> None:
        try:
            self.output.setPlainText(net_diag.format_report(snap))
        except Exception as e:
            self.output.setPlainText(f"{type(e).__name__}: {e}")
        self.rerun_btn.setEnabled(True)

    def _on_copy(self) -> None:
        QApplication.clipboard().setText(self.output.toPlainText())
        self.copy_btn.setText(tr("diag.copied"))

    # --- teardown ---------------------------------------------------------

    def _stop_worker(self) -> None:
        """Join the collector before this dialog (its parent) is destroyed — a
        QThread deleted while running aborts the whole app with a C++ qFatal
        (the v3.4.1 crash class)."""
        w = self._worker
        try:
            if w is not None and w.isRunning():
                w.requestInterruption()
                w.quit()
                w.wait(6000)
        except Exception:
            pass

    def done(self, result: int) -> None:
        self._stop_worker()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._stop_worker()
        super().closeEvent(event)
