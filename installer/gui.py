"""Installer UI — frameless, dark, same theme as the main app."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


def _asset_path(filename: str) -> Path:
    """Resolve a file inside installer/ — works both frozen and from source."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "installer" / filename
    return Path(__file__).resolve().parent / filename

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from kapro_vpn import __version__
from kapro_vpn.gui import icons as app_icons
from kapro_vpn.gui.styles import DARK_QSS
from kapro_vpn.gui.titlebar import TitleBar

from . import operations, paths


# --- worker thread --------------------------------------------------------

class _InstallWorker(QThread):
    progress = Signal(str, int)  # (status_text, percent)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, version: str, create_desktop: bool, parent=None):
        super().__init__(parent)
        self._version = version
        self._desktop = create_desktop

    def run(self) -> None:
        try:
            operations.install_everything(
                version=self._version,
                progress=lambda s, p: self.progress.emit(s, p),
                create_desktop=self._desktop,
            )
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class _UninstallWorker(QThread):
    progress = Signal(str, int)
    finished_ok = Signal()
    failed = Signal(str)

    def run(self) -> None:
        try:
            operations.uninstall_everything(
                progress=lambda s, p: self.progress.emit(s, p),
            )
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


# --- pages ----------------------------------------------------------------

class WelcomePage(QWidget):
    install_clicked = Signal(bool)  # bool = create desktop shortcut

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 36, 40, 32)
        layout.setSpacing(0)

        # Hero image — prefer installer/hero.png if present, fall back to splash
        hero_widget = self._build_hero()
        layout.addWidget(hero_widget, alignment=Qt.AlignHCenter)
        layout.addSpacing(20)

        title = QLabel("KaproVPN")
        title.setObjectName("h1")
        title.setAlignment(Qt.AlignHCenter)
        layout.addWidget(title)
        layout.addSpacing(4)

        version_label = QLabel(f"v{__version__} · установка")
        version_label.setObjectName("muted")
        version_label.setAlignment(Qt.AlignHCenter)
        layout.addWidget(version_label)
        layout.addSpacing(16)

        blurb = QLabel(
            "Десктопный VPN-клиент со встроенным split-routing'ом для "
            "российских сайтов. Будет установлен в твой пользовательский "
            "профиль — права администратора не нужны."
        )
        blurb.setObjectName("muted")
        blurb.setWordWrap(True)
        blurb.setAlignment(Qt.AlignHCenter)
        layout.addWidget(blurb)
        layout.addSpacing(20)

        self.desktop_check = QCheckBox("Создать ярлык на Рабочем столе")
        self.desktop_check.setChecked(True)
        layout.addWidget(self.desktop_check, alignment=Qt.AlignHCenter)

        layout.addStretch(1)

        install_btn = QPushButton("Установить")
        install_btn.setObjectName("primary")
        install_btn.setMinimumHeight(40)
        install_btn.clicked.connect(
            lambda: self.install_clicked.emit(self.desktop_check.isChecked())
        )
        layout.addWidget(install_btn)
        layout.addSpacing(8)

        footer = QLabel(
            f"<span style='color:#71717a; font-size:8pt'>"
            f"GPL v3 · "
            f"<a href='{paths.HOMEPAGE}' style='color:#f59e0b'>"
            f"{paths.HOMEPAGE.replace('https://', '')}"
            f"</a></span>"
        )
        footer.setTextFormat(Qt.RichText)
        footer.setOpenExternalLinks(True)
        footer.setAlignment(Qt.AlignHCenter)
        layout.addWidget(footer)

    def _build_hero(self) -> QWidget:
        """Hero artwork from installer/hero.png, fall back to splash logo."""
        hero = _asset_path("hero.png")
        if not hero.is_file():
            # Fall back to main-app splash (always available)
            pix = app_icons.splash_pixmap(280)
        else:
            pix = QPixmap(str(hero)).scaledToWidth(
                380, Qt.SmoothTransformation,
            )
        lbl = QLabel()
        lbl.setPixmap(pix)
        lbl.setAlignment(Qt.AlignHCenter)
        return lbl


class InstallingPage(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 60, 40, 40)
        layout.setSpacing(0)

        title = QLabel("Устанавливаю…")
        title.setObjectName("h1")
        title.setAlignment(Qt.AlignHCenter)
        layout.addWidget(title)
        layout.addSpacing(40)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        layout.addWidget(self.progress_bar)
        layout.addSpacing(12)

        self.status_label = QLabel("Подготовка…")
        self.status_label.setObjectName("muted")
        self.status_label.setAlignment(Qt.AlignHCenter)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addStretch(1)

    def set_progress(self, text: str, percent: int) -> None:
        self.progress_bar.setValue(percent)
        self.status_label.setText(text)


class DonePage(QWidget):
    close_clicked = Signal(bool)  # bool = launch app now

    def __init__(self, success: bool = True, error: str = "",
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 60, 40, 32)
        layout.setSpacing(0)

        # Hero illustration — installer/done.png or fallback to app icon
        done_art = _asset_path("done.png")
        if done_art.is_file() and success:
            pix = QPixmap(str(done_art)).scaled(
                180, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        else:
            pix = app_icons.app_icon().pixmap(140, 140)
        art_label = QLabel()
        art_label.setPixmap(pix)
        art_label.setAlignment(Qt.AlignHCenter)
        layout.addWidget(art_label)
        layout.addSpacing(20)

        if success:
            title = QLabel("Готово!")
            title.setObjectName("h1")
            title.setAlignment(Qt.AlignHCenter)
            layout.addWidget(title)
            layout.addSpacing(8)
            sub = QLabel("KaproVPN установлен")
            sub.setObjectName("muted")
            sub.setAlignment(Qt.AlignHCenter)
            layout.addWidget(sub)
        else:
            title = QLabel("Ошибка")
            title.setObjectName("h1")
            title.setAlignment(Qt.AlignHCenter)
            layout.addWidget(title)
            layout.addSpacing(8)
            err_label = QLabel(error)
            err_label.setObjectName("muted")
            err_label.setAlignment(Qt.AlignHCenter)
            err_label.setWordWrap(True)
            layout.addWidget(err_label)

        layout.addStretch(1)

        if success:
            self.launch_check = QCheckBox("Запустить KaproVPN сейчас")
            self.launch_check.setChecked(True)
            layout.addWidget(self.launch_check, alignment=Qt.AlignHCenter)
            layout.addSpacing(12)
        else:
            self.launch_check = None

        close_btn = QPushButton("Закрыть")
        if success:
            close_btn.setObjectName("primary")
        close_btn.setMinimumHeight(40)
        close_btn.clicked.connect(self._on_close)
        layout.addWidget(close_btn)

    def _on_close(self) -> None:
        launch = bool(self.launch_check and self.launch_check.isChecked())
        self.close_clicked.emit(launch)


# --- main window ----------------------------------------------------------

class InstallerWindow(QMainWindow):
    """The whole installer is one frameless window with a 3-page stack."""

    def __init__(self, uninstall_mode: bool = False):
        super().__init__()
        self._uninstall = uninstall_mode
        self.setWindowTitle(
            "KaproVPN — Удаление" if uninstall_mode else "KaproVPN — Установка"
        )
        self.setWindowIcon(app_icons.app_icon())
        self.setFixedSize(480, 620)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowMinimizeButtonHint)

        shell = QWidget()
        shell.setObjectName("appShell")
        self.setCentralWidget(shell)
        root = QVBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.titlebar = TitleBar()
        self.titlebar.title_label.setText(
            "KaproVPN — Удаление" if uninstall_mode else "KaproVPN — Установка"
        )
        self.titlebar.btn_min.setVisible(False)  # no minimize for installer
        self.titlebar.close_clicked.connect(self.close)
        root.addWidget(self.titlebar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, stretch=1)

        if uninstall_mode:
            self._build_uninstall_flow()
        else:
            self._build_install_flow()

        self._worker = None  # type: ignore

    # --- install flow ------------------------------------------------------

    def _build_install_flow(self) -> None:
        self.welcome = WelcomePage()
        self.welcome.install_clicked.connect(self._start_install)
        self.stack.addWidget(self.welcome)

        self.installing = InstallingPage()
        self.stack.addWidget(self.installing)

        self.stack.setCurrentWidget(self.welcome)

    def _start_install(self, create_desktop: bool) -> None:
        self.stack.setCurrentWidget(self.installing)
        self.titlebar.btn_close.setEnabled(False)  # can't bail mid-install
        self._worker = _InstallWorker(
            version=__version__, create_desktop=create_desktop, parent=self,
        )
        self._worker.progress.connect(self.installing.set_progress)
        self._worker.finished_ok.connect(self._on_install_done)
        self._worker.failed.connect(self._on_install_failed)
        self._worker.start()

    def _on_install_done(self) -> None:
        self.titlebar.btn_close.setEnabled(True)
        done = DonePage(success=True)
        done.close_clicked.connect(self._on_finish)
        self.stack.addWidget(done)
        self.stack.setCurrentWidget(done)

    def _on_install_failed(self, msg: str) -> None:
        self.titlebar.btn_close.setEnabled(True)
        done = DonePage(success=False, error=msg)
        done.close_clicked.connect(self._on_finish)
        self.stack.addWidget(done)
        self.stack.setCurrentWidget(done)

    def _on_finish(self, launch: bool) -> None:
        if launch and not self._uninstall and paths.installed_exe_path().is_file():
            import subprocess
            subprocess.Popen(
                [str(paths.installed_exe_path())],
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                close_fds=True,
            )
        self.close()

    # --- uninstall flow ----------------------------------------------------

    def _build_uninstall_flow(self) -> None:
        confirm = QWidget()
        confirm.setObjectName("page")
        layout = QVBoxLayout(confirm)
        layout.setContentsMargins(40, 60, 40, 32)
        layout.setSpacing(0)

        pix = app_icons.app_icon().pixmap(120, 120)
        art = QLabel()
        art.setPixmap(pix)
        art.setAlignment(Qt.AlignHCenter)
        layout.addWidget(art)
        layout.addSpacing(20)

        title = QLabel("Удалить KaproVPN?")
        title.setObjectName("h1")
        title.setAlignment(Qt.AlignHCenter)
        layout.addWidget(title)
        layout.addSpacing(8)
        sub = QLabel(
            "Будут удалены: программа, ярлыки, запись в Programs &amp; Features. "
            "Скачанные xray.exe / tun2socks.exe в %LOCALAPPDATA%\\KaproVPN\\ "
            "останутся — удали их вручную, если нужно полностью."
        )
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        sub.setAlignment(Qt.AlignHCenter)
        layout.addWidget(sub)

        layout.addStretch(1)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Оставить")
        cancel_btn.clicked.connect(self.close)
        uninst_btn = QPushButton("Удалить")
        uninst_btn.setObjectName("danger")
        uninst_btn.setMinimumHeight(40)
        uninst_btn.clicked.connect(self._start_uninstall)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(uninst_btn)
        layout.addLayout(btn_row)

        self.stack.addWidget(confirm)
        self.installing = InstallingPage()
        self.installing.set_progress("…", 0)
        self.stack.addWidget(self.installing)

    def _start_uninstall(self) -> None:
        self.stack.setCurrentWidget(self.installing)
        self.installing.set_progress("Удаляю…", 5)
        self.titlebar.btn_close.setEnabled(False)
        self._worker = _UninstallWorker(parent=self)
        self._worker.progress.connect(self.installing.set_progress)
        self._worker.finished_ok.connect(self._on_uninstall_done)
        self._worker.failed.connect(self._on_uninstall_failed)
        self._worker.start()

    def _on_uninstall_done(self) -> None:
        self.titlebar.btn_close.setEnabled(True)
        done = DonePage(success=True)
        # Repurpose the title for uninstall context — done.py is generic
        for lbl in done.findChildren(QLabel):
            if lbl.text() == "Готово!":
                lbl.setText("Удалено")
            elif lbl.text() == "KaproVPN установлен":
                lbl.setText("KaproVPN удалён")
        if done.launch_check is not None:
            done.launch_check.setVisible(False)
        done.close_clicked.connect(lambda _launch: self.close())
        self.stack.addWidget(done)
        self.stack.setCurrentWidget(done)

    def _on_uninstall_failed(self, msg: str) -> None:
        self.titlebar.btn_close.setEnabled(True)
        done = DonePage(success=False, error=msg)
        done.close_clicked.connect(lambda _launch: self.close())
        self.stack.addWidget(done)
        self.stack.setCurrentWidget(done)


def run(uninstall: bool = False) -> int:
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("KaproVPN Installer")
    app.setOrganizationName("KaproVPN")
    app.setStyleSheet(DARK_QSS)
    app.setWindowIcon(app_icons.app_icon())

    window = InstallerWindow(uninstall_mode=uninstall)
    window.show()
    return app.exec()
