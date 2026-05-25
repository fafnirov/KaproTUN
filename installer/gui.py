"""Installer UI — frameless, dark, same theme as the main app."""
from __future__ import annotations

import subprocess
import sys
import time
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
        layout.setContentsMargins(40, 28, 40, 28)
        layout.setSpacing(0)

        # Hero image — prefer installer/hero.png if present, fall back to
        # splash. If we used the splash fallback, it already has the
        # "KaproVPN" wordmark baked in, so skip the title label below to
        # avoid the visible "KaproVPN / KaproVPN" duplication.
        hero_widget, hero_has_text = self._build_hero()
        layout.addWidget(hero_widget, alignment=Qt.AlignHCenter)
        layout.addSpacing(18)

        if not hero_has_text:
            title = QLabel("KaproVPN")
            title.setObjectName("h1")
            title.setAlignment(Qt.AlignHCenter)
            layout.addWidget(title)
            layout.addSpacing(4)

        version_label = QLabel(f"Установщик · v{__version__}")
        version_label.setObjectName("muted")
        version_label.setAlignment(Qt.AlignHCenter)
        layout.addWidget(version_label)
        layout.addSpacing(20)

        blurb = QLabel(
            "Десктопный VPN-клиент со split-routing'ом по настраиваемому "
            "списку прямых сайтов. Установится в твой пользовательский "
            "профиль — права администратора не нужны."
        )
        blurb.setObjectName("muted")
        blurb.setWordWrap(True)
        blurb.setAlignment(Qt.AlignHCenter)
        layout.addWidget(blurb)
        layout.addStretch(1)

        self.desktop_check = QCheckBox("Создать ярлык на Рабочем столе")
        self.desktop_check.setChecked(True)
        layout.addWidget(self.desktop_check, alignment=Qt.AlignHCenter)
        layout.addSpacing(14)

        install_btn = QPushButton("Установить")
        install_btn.setObjectName("primary")
        install_btn.setMinimumHeight(44)
        install_btn.clicked.connect(
            lambda: self.install_clicked.emit(self.desktop_check.isChecked())
        )
        layout.addWidget(install_btn)
        layout.addSpacing(10)

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

    def _build_hero(self) -> tuple[QWidget, bool]:
        """Return (widget, has_baked_text).

        `has_baked_text` is True when the fallback splash is used — its
        PNG already contains the "KaproVPN" wordmark, so the caller
        should skip rendering its own title label.
        """
        hero = _asset_path("hero.png")
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignHCenter)
        if hero.is_file():
            pix = QPixmap(str(hero)).scaledToWidth(380, Qt.SmoothTransformation)
            lbl.setPixmap(pix)
            return (lbl, False)
        # Fallback: splash has KaproVPN wordmark baked in.
        pix = app_icons.splash_pixmap(220)
        lbl.setPixmap(pix)
        return (lbl, True)


class MaintenancePage(QWidget):
    """Shown when Setup.exe runs on a machine where KaproVPN is already
    installed. Two big choices: Reinstall or Uninstall. No accidental
    re-install — user has to deliberately pick.
    """
    # No bool arg: at reinstall time the user already has shortcuts
    # from the original install. Re-running install with create_desktop=
    # True would just produce a duplicate Desktop shortcut.
    reinstall_clicked = Signal()
    uninstall_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 28, 40, 28)
        layout.setSpacing(0)

        hero_widget, hero_has_text = self._build_hero()
        layout.addWidget(hero_widget, alignment=Qt.AlignHCenter)
        layout.addSpacing(18)

        if not hero_has_text:
            title = QLabel("KaproVPN")
            title.setObjectName("h1")
            title.setAlignment(Qt.AlignHCenter)
            layout.addWidget(title)
            layout.addSpacing(4)

        # Detect the installed version so the user can see whether the
        # Setup.exe they ran is the same / newer / older than what's
        # already on disk. Cheap (one file-version probe via the same
        # PowerShell trick the in-app updater uses).
        installed_ver = _detect_installed_version()
        if installed_ver and installed_ver != __version__:
            sub = QLabel(
                f"Уже установлен: v{installed_ver}<br>"
                f"Этот установщик: v{__version__}"
            )
        else:
            sub = QLabel(f"Уже установлен · v{installed_ver or __version__}")
        sub.setObjectName("muted")
        sub.setAlignment(Qt.AlignHCenter)
        sub.setTextFormat(Qt.RichText)
        layout.addWidget(sub)
        layout.addSpacing(20)

        blurb = QLabel("Что сделать?")
        blurb.setObjectName("muted")
        blurb.setAlignment(Qt.AlignHCenter)
        layout.addWidget(blurb)
        layout.addStretch(1)

        # --- Reinstall (primary action — most common when a user
        # re-runs Setup.exe is to update to a newer version) ---
        # No "Create desktop shortcut" checkbox here: the user already
        # has shortcuts from the original install, and re-running
        # install with create_desktop=True would just produce a
        # duplicate on the Desktop. Pure-reinstall keeps existing
        # shortcuts as-is.
        reinstall_btn = QPushButton("Переустановить")
        reinstall_btn.setObjectName("primary")
        reinstall_btn.setMinimumHeight(44)
        reinstall_btn.clicked.connect(self.reinstall_clicked.emit)
        layout.addWidget(reinstall_btn)
        layout.addSpacing(8)

        # --- Uninstall (destructive secondary) ---
        uninstall_btn = QPushButton("Удалить KaproVPN")
        uninstall_btn.setObjectName("danger")
        uninstall_btn.setMinimumHeight(38)
        uninstall_btn.clicked.connect(self.uninstall_clicked.emit)
        layout.addWidget(uninstall_btn)
        layout.addSpacing(10)

        footer = QLabel(
            f"<span style='color:#71717a; font-size:8pt'>"
            f"Конфиги и настройки сохраняются при «Переустановить» и "
            f"очищаются при «Удалить»."
            f"</span>"
        )
        footer.setTextFormat(Qt.RichText)
        footer.setAlignment(Qt.AlignHCenter)
        footer.setWordWrap(True)
        layout.addWidget(footer)

    # Reuse WelcomePage's hero-building logic — same artwork, same
    # fallback rules. Duplicating the method here to avoid pulling
    # WelcomePage as a base class (Qt mixins get messy).
    _build_hero = WelcomePage._build_hero


def _detect_installed_version() -> Optional[str]:
    """Read the version embedded in the installed KaproVPN.exe.

    Cheap PowerShell shell-out via Get-Item.VersionInfo. Returns None
    if probe fails — caller falls back to "unknown version".
    """
    exe = paths.installed_exe_path()
    if not exe.is_file():
        return None
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
            capture_output=True, timeout=5,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (proc.stdout or "").strip() or None
    except Exception:
        return None


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

    def __init__(self, mode: str = "install"):
        """`mode` ∈ {'install', 'maintenance', 'uninstall'}.

        - install: classic Welcome → Installing → Done flow.
        - maintenance: Reinstall / Uninstall choice for users who run
          Setup.exe a second time. Selecting Reinstall falls through
          to the install flow; selecting Uninstall to the uninstall flow.
        - uninstall: direct confirm + uninstall (used by Programs &
          Features Uninstall button which we register with --uninstall).
        """
        super().__init__()
        self._mode = mode
        self._uninstall = (mode == "uninstall")
        title = {
            "install": "KaproVPN — Установка",
            "maintenance": "KaproVPN — Установлен",
            "uninstall": "KaproVPN — Удаление",
        }.get(mode, "KaproVPN — Установка")
        self.setWindowTitle(title)
        self.setWindowIcon(app_icons.app_icon())
        # Roomier than the cramped 620 — the welcome page has hero +
        # title + version + blurb + checkbox + button + footer and the
        # extra 80 px lets it breathe.
        self.setFixedSize(520, 700)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowMinimizeButtonHint)

        shell = QWidget()
        shell.setObjectName("appShell")
        self.setCentralWidget(shell)
        root = QVBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.titlebar = TitleBar()
        self.titlebar.title_label.setText(title)
        self.titlebar.btn_min.setVisible(False)  # no minimize for installer
        self.titlebar.close_clicked.connect(self.close)
        root.addWidget(self.titlebar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, stretch=1)

        # Build pages lazily — uninstall and maintenance modes both need
        # the install pages too (uninstall to show "done", maintenance
        # because Reinstall routes through them). Keep flow methods to
        # build sub-trees as needed.
        if mode == "uninstall":
            self._build_uninstall_flow()
        elif mode == "maintenance":
            self._build_maintenance_flow()
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

    # --- maintenance flow (Reinstall / Uninstall choice) ----------------

    def _build_maintenance_flow(self) -> None:
        """Two-choice menu when the user runs Setup.exe on a machine
        that already has KaproVPN installed.

        Reinstall ⇒ falls through to the install flow (which is
        idempotent in operations.install_everything — overwrites in
        place, preserves configs).

        Uninstall ⇒ falls through to the uninstall flow.
        """
        self.maintenance = MaintenancePage()
        self.maintenance.reinstall_clicked.connect(self._switch_to_install)
        self.maintenance.uninstall_clicked.connect(self._switch_to_uninstall)
        self.stack.addWidget(self.maintenance)
        self.stack.setCurrentWidget(self.maintenance)

    def _switch_to_install(self) -> None:
        """Maintenance → Reinstall: build the install flow and kick it
        off without showing the Welcome page (we don't need consent
        twice, the user just clicked Reinstall).

        Reinstall always passes create_desktop=False — the user
        already has a Desktop shortcut from the original install,
        no point creating a duplicate.
        """
        # Build install flow if not yet present.
        if not hasattr(self, "installing"):
            self.installing = InstallingPage()
            self.stack.addWidget(self.installing)
        # Update window chrome so the user knows what's happening now.
        self.setWindowTitle("KaproVPN — Переустановка")
        self.titlebar.title_label.setText("KaproVPN — Переустановка")
        self._mode = "install"
        self._uninstall = False
        # Re-use the install worker path directly (same as
        # WelcomePage's install_clicked signal would have triggered).
        self._start_install(create_desktop=False)

    def _switch_to_uninstall(self) -> None:
        """Maintenance → Uninstall: build the existing uninstall flow."""
        self.setWindowTitle("KaproVPN — Удаление")
        self.titlebar.title_label.setText("KaproVPN — Удаление")
        self._mode = "uninstall"
        self._uninstall = True
        self._build_uninstall_flow()

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

        # QStackedWidget.addWidget auto-selects only when the stack was
        # empty before. Through the maintenance → uninstall path the
        # stack already contains MaintenancePage, so without an explicit
        # setCurrentWidget the confirm page would be hidden behind it
        # and the user sees "nothing happens" when clicking Удалить.
        # Caught in v1.8.1 user testing — fixed in 1.8.2.
        self.stack.setCurrentWidget(confirm)

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


def run(mode: str = "install", *, uninstall: bool = False) -> int:
    """Launch the installer GUI in the given mode.

    `mode` is one of "install" / "maintenance" / "uninstall" — see
    InstallerWindow docstring. The legacy `uninstall=True` kwarg is
    kept for backwards-compat (was the only flag before v1.8.1); if
    set, it overrides `mode` to "uninstall".
    """
    import sys

    from PySide6.QtWidgets import QApplication

    if uninstall:
        mode = "uninstall"

    app = QApplication(sys.argv)
    app.setApplicationName("KaproVPN Installer")
    app.setOrganizationName("KaproVPN")
    app.setStyleSheet(DARK_QSS)
    app.setWindowIcon(app_icons.app_icon())

    window = InstallerWindow(mode=mode)
    window.show()
    return app.exec()


# --- silent (auto-update) mode --------------------------------------------

class _SilentUpdateWindow(QMainWindow):
    """Tiny always-on-top indicator shown during silent auto-update.

    No buttons, no titlebar — just a card with logo, "Обновляю..."
    text and an indeterminate progress bar so the user knows
    something is happening between "main app quit" and "new app
    launched".
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("KaproVPN — обновление")
        self.setWindowIcon(app_icons.app_icon())
        self.setFixedSize(360, 130)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)

        shell = QWidget()
        shell.setObjectName("appShell")
        self.setCentralWidget(shell)
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        row = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(app_icons.app_icon().pixmap(48, 48))
        row.addWidget(icon)
        row.addSpacing(8)

        text_col = QVBoxLayout()
        title = QLabel("Обновляю KaproVPN…")
        title.setObjectName("h2")
        text_col.addWidget(title)
        self.status_label = QLabel("Подготовка…")
        self.status_label.setObjectName("muted")
        text_col.addWidget(self.status_label)
        row.addLayout(text_col, stretch=1)
        layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        layout.addWidget(self.progress)

        # Centre on the primary screen
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen().geometry()
        self.move(
            (screen.width() - self.width()) // 2,
            (screen.height() - self.height()) // 2,
        )

    def set_progress(self, text: str, percent: int) -> None:
        self.status_label.setText(text)
        self.progress.setValue(percent)


def run_silent() -> int:
    """Headless install used by the in-app auto-updater."""
    from PySide6.QtWidgets import QApplication

    # Wait a beat so the calling KaproVPN.exe finishes shutting down
    # and Windows releases its file handles. Without this we get an
    # ERROR_SHARING_VIOLATION when copy_main_exe tries to overwrite
    # the running exe.
    time.sleep(1.5)

    app = QApplication(sys.argv)
    app.setApplicationName("KaproVPN Updater")
    app.setOrganizationName("KaproVPN")
    app.setStyleSheet(DARK_QSS)
    app.setWindowIcon(app_icons.app_icon())

    window = _SilentUpdateWindow()
    window.show()

    worker = _InstallWorker(version=__version__, create_desktop=False, parent=window)
    worker.progress.connect(window.set_progress)

    def on_done() -> None:
        # Relaunch the newly-installed app, then exit ourselves.
        exe = paths.installed_exe_path()
        if exe.is_file():
            try:
                subprocess.Popen(
                    [str(exe)],
                    creationflags=(
                        getattr(subprocess, "DETACHED_PROCESS", 0)
                        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    ),
                    close_fds=True,
                )
            except OSError:
                pass
        QApplication.quit()

    def on_failed(msg: str) -> None:
        window.set_progress(f"Ошибка: {msg}", 0)
        # Leave the window up for 5 s so the user can see what happened
        from PySide6.QtCore import QTimer
        QTimer.singleShot(5000, QApplication.quit)

    worker.finished_ok.connect(on_done)
    worker.failed.connect(on_failed)
    worker.start()

    return app.exec()
