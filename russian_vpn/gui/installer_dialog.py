"""Modal dialog that downloads sing-box.exe with a progress bar."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from ..core import singbox_installer


class _DownloadThread(QThread):
    progress = Signal(int, int)  # bytes_done, bytes_total
    finished_ok = Signal()
    failed = Signal(str)

    def run(self) -> None:
        try:
            singbox_installer.download_and_install(
                progress=lambda d, t: self.progress.emit(d, t)
            )
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


def ensure_singbox_installed(parent) -> bool:
    """Download sing-box if missing. Returns True on success (or already present)."""
    if singbox_installer.is_installed():
        return True

    dlg = QProgressDialog("Загрузка sing-box...", None, 0, 100, parent)
    dlg.setWindowTitle("Первый запуск")
    dlg.setCancelButton(None)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setValue(0)

    thread = _DownloadThread()
    error_holder: list[str] = []

    def on_progress(done: int, total: int) -> None:
        if total > 0:
            dlg.setValue(int(done * 100 / total))
            dlg.setLabelText(f"Загрузка sing-box... {done // 1024} / {total // 1024} КБ")
        else:
            dlg.setLabelText(f"Загрузка sing-box... {done // 1024} КБ")

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
        QMessageBox.critical(
            parent,
            "Не удалось скачать sing-box",
            f"{error_holder[0]}\n\n"
            f"Проверь интернет, или скачай sing-box.exe вручную с\n"
            f"https://github.com/SagerNet/sing-box/releases\n"
            f"и положи в:\n{singbox_installer.paths.singbox_dir()}",
        )
        return False
    return singbox_installer.is_installed()
