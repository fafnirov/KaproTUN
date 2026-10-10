"""First-run downloads: the VPN engine and the list of Russian addresses.

A dialog over the window with a progress bar while a file comes down; on
failure, what went wrong, how to do it by hand, and a button to try again.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QHBoxLayout

from ..core import geoip_ru, sing_box_installer
from ..core.i18n import tr
from . import kit
from .merge_prompt import one_line


class _DownloadThread(QThread):
    progress = Signal(int, int)  # bytes_done, bytes_total
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, installer_fn):
        super().__init__()
        self._installer_fn = installer_fn

    def run(self) -> None:
        try:
            self._installer_fn(progress=lambda d, t: self.progress.emit(d, t))
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class DownloadDialog(kit.OverlayDialog):
    """Shown while one file downloads. It has no buttons and does not close
    on Escape: the download is not something that can be stopped half way,
    and the client cannot start without what it brings."""

    def __init__(self, parent, label: str):
        super().__init__(parent)
        self.error = ""
        self._over = False
        self.head("download", tr("inst.first_run_title"), tr("inst.first_run_text"), tone="accent")
        row = QHBoxLayout()
        row.addWidget(kit.label(label, "label"), stretch=1)
        self.meta = kit.label("", "count")
        row.addWidget(self.meta)
        self.body.addLayout(row)
        self.bar = kit.Progress()
        self.add_widget(self.bar)

    def on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.bar.set_fraction(done / total)
            self.meta.setText(tr("inst.progress_kb", done=done // 1024, total=total // 1024))
        else:
            self.bar.set_fraction(None)
            self.meta.setText(tr("inst.progress_kb_unknown", done=done // 1024))

    def on_done(self) -> None:
        self._over = True
        self.accept()

    def on_failed(self, message: str) -> None:
        self.error = message
        self._over = True
        self.accept()

    def reject(self) -> None:
        if self._over:
            super().reject()


def failure_dialog(parent, label: str, error: str, manual_hint: str) -> kit.OverlayDialog:
    """What failed, in the system's own words (plain text, one line), and the
    way to do it by hand."""
    dlg = kit.OverlayDialog(parent, wide=True)
    dlg.head("x-circle", tr("inst.download_failed_title", label=label), "", tone="danger")
    dlg.error_label = dlg.add_long_text(one_line(error, 300), max_height=60, role="mono")
    dlg.hint_label = dlg.add_long_text(manual_hint, max_height=200)
    dlg.add_actions([("close", tr("leak.close_btn"), "secondary"),
                     ("retry", tr("inst.retry"), "primary")],
                    default="retry", icons={"retry": "refresh"})
    return dlg


def _run_download(parent, label: str, installer_fn, on_fail_msg: str) -> bool:
    while True:
        dlg = DownloadDialog(parent, label)
        thread = _DownloadThread(installer_fn)
        thread.progress.connect(dlg.on_progress)
        thread.finished_ok.connect(dlg.on_done)
        thread.failed.connect(dlg.on_failed)
        thread.start()
        dlg.exec()
        thread.wait()
        if not dlg._over:
            # Closed from outside (the app is quitting): not a success.
            return False
        if not dlg.error:
            return True
        if failure_dialog(parent, label, dlg.error, on_fail_msg).ask() != "retry":
            return False


def ensure_sing_box_installed(parent) -> bool:
    """Download sing-box (and wintun.dll on Windows) if it is missing."""
    if sing_box_installer.is_installed():
        return True
    return _run_download(
        parent, "sing-box + WinTUN", sing_box_installer.download_and_install,
        tr("inst.singbox_manual_hint", path=sing_box_installer.paths.sing_box_dir()),
    ) and sing_box_installer.is_installed()


def ensure_geoip_ru_cached(parent) -> bool:
    """Download the list of Russian IP ranges if it is missing — it is what
    "Russian sites direct" routes by.

    Not required to connect: without it the client still sends the domains it
    resolved itself directly, just without the full coverage.
    """
    if geoip_ru.is_cached():
        return True
    return _run_download(
        parent, tr("inst.geoip_label"), geoip_ru.download,
        tr("inst.geoip_manual_hint", url=geoip_ru.GEOIP_RU_URL, path=geoip_ru.cache_file()),
    ) and geoip_ru.is_cached()
