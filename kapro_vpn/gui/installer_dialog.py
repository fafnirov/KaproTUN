"""Modal dialog that downloads required binaries with a progress bar."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from ..core import geoip_ru, tun2socks_installer, xray_installer


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
    dlg = QProgressDialog(f"Загрузка {label}...", None, 0, 100, parent)
    dlg.setWindowTitle("Первый запуск")
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
            dlg.setLabelText(f"Загрузка {label}... {done // 1024} / {total // 1024} КБ")
        else:
            dlg.setLabelText(f"Загрузка {label}... {done // 1024} КБ")

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
        QMessageBox.critical(parent, f"Не удалось скачать {label}",
                             f"{error_holder[0]}\n\n{on_fail_msg}")
        return False
    return True


def ensure_xray_installed(parent) -> bool:
    """Download Xray-core if missing. Returns True on success (or already present)."""
    if xray_installer.is_installed():
        return True
    return _run_download(
        parent, "Xray-core", xray_installer.download_and_install,
        f"Проверь интернет, или скачай Xray-core вручную с\n"
        f"https://github.com/XTLS/Xray-core/releases\n"
        f"и распакуй в:\n{xray_installer.paths.xray_dir()}",
    ) and xray_installer.is_installed()


def ensure_tun2socks_installed(parent) -> bool:
    """Download tun2socks + wintun.dll if missing. For TUN mode."""
    if tun2socks_installer.is_installed():
        return True
    return _run_download(
        parent, "tun2socks + WinTUN", tun2socks_installer.download_and_install,
        f"Скачай вручную:\n"
        f"- https://github.com/xjasonlyu/tun2socks/releases\n"
        f"- https://www.wintun.net/\n"
        f"и положи tun2socks.exe и wintun.dll в:\n{tun2socks_installer.paths.tun_dir()}",
    ) and tun2socks_installer.is_installed()


def ensure_geoip_ru_cached(parent) -> bool:
    """Download Russian IP CIDR list if missing. For TUN-mode split routing.

    Soft requirement — if the download fails, TUN mode still works for
    domains we pre-resolved, just without comprehensive RU coverage.
    """
    if geoip_ru.is_cached():
        return True
    return _run_download(
        parent, "geoip:ru CIDR list", geoip_ru.download,
        f"Скачай вручную:\n{geoip_ru.GEOIP_RU_URL}\n"
        f"и сохрани как:\n{geoip_ru.cache_file()}",
    ) and geoip_ru.is_cached()
