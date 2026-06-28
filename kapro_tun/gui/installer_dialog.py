"""Modal dialog that downloads required binaries with a progress bar."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from ..core import geoip_ru, sing_box_installer
from ..core.i18n import tr


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


def _run_download(parent, label: str, installer_fn, on_fail_msg: str) -> bool:
    dlg = QProgressDialog(tr("inst.downloading", label=label), None, 0, 100, parent)
    dlg.setWindowTitle(tr("inst.first_run_title"))
    dlg.setCancelButton(None)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setValue(0)

    thread = _DownloadThread(installer_fn)
    error_holder: list[str] = []

    def on_progress(done: int, total: int) -> None:
        if total > 0:
            dlg.setValue(int(done * 100 / total))
            dlg.setLabelText(tr("inst.downloading_progress", label=label,
                                done=done // 1024, total=total // 1024))
        else:
            dlg.setLabelText(tr("inst.downloading_indeterminate", label=label,
                                done=done // 1024))

    def on_done() -> None:
        dlg.setValue(100)
        dlg.close()

    def on_failed(msg: str) -> None:
        error_holder.append(msg)
        dlg.close()

    thread.progress.connect(on_progress)
    thread.finished_ok.connect(on_done)
    thread.failed.connect(on_failed)
    thread.start()
    dlg.exec()
    thread.wait()

    if error_holder:
        QMessageBox.critical(parent, tr("inst.download_failed_title", label=label),
                             f"{error_holder[0]}\n\n{on_fail_msg}")
        return False
    return True


def ensure_sing_box_installed(parent) -> bool:
    """Download sing-box (+ wintun.dll on Windows) if missing. For the v3.0.0
    sing-box TUN engine."""
    if sing_box_installer.is_installed():
        return True
    return _run_download(
        parent, "sing-box + WinTUN", sing_box_installer.download_and_install,
        tr("inst.singbox_manual_hint", path=sing_box_installer.paths.sing_box_dir()),
    ) and sing_box_installer.is_installed()


def ensure_geoip_ru_cached(parent) -> bool:
    """Download the local-IP CIDR list if missing. For TUN-mode split routing.

    Soft requirement — if the download fails, TUN mode still works for
    domains we pre-resolved, just without comprehensive CIDR coverage.
    """
    if geoip_ru.is_cached():
        return True
    return _run_download(
        parent, tr("inst.geoip_label"), geoip_ru.download,
        tr("inst.geoip_manual_hint", url=geoip_ru.GEOIP_RU_URL, path=geoip_ru.cache_file()),
    ) and geoip_ru.is_cached()
