"""Application entry point."""
from __future__ import annotations

import signal
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QSplashScreen

from .core import autostart
from .gui import icons
from .gui.main_window import MainWindow
from .gui.styles import DARK_QSS


def main() -> int:
    # Let Ctrl+C in the terminal kill the app cleanly
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # --minimized: boot straight to the tray, don't pop the main window
    # (used by the Windows Run-key registration for auto-start on login).
    start_minimized = autostart.MINIMIZED_FLAG in sys.argv

    app = QApplication(sys.argv)
    app.setApplicationName("KaproVPN")
    app.setOrganizationName("KaproVPN")
    app.setStyleSheet(DARK_QSS)
    app.setWindowIcon(icons.app_icon())
    # Don't exit when the user clicks X — we hide to tray instead.
    # Real exit goes through the tray-menu "Выход" item, which calls
    # QApplication.quit() explicitly.
    app.setQuitOnLastWindowClosed(False)

    splash = None
    if not start_minimized:
        splash = QSplashScreen(icons.splash_pixmap(320), Qt.WindowStaysOnTopHint)
        splash.show()
        splash.showMessage("Запуск…", Qt.AlignBottom | Qt.AlignHCenter, Qt.white)
        app.processEvents()

    window = MainWindow()

    def reveal() -> None:
        if not start_minimized:
            window.show()
            if splash is not None:
                splash.finish(window)
        # Optional auto-connect on launch — wait a beat after window
        # construction so any first-run installer dialogs finish first.
        if window.manager.settings.get("autoconnect_on_launch", False):
            QTimer.singleShot(800, window.trigger_autoconnect)

    # Tiny delay before swapping splash → window, so the splash is
    # actually visible. Without this, fast machines flash it for 1 frame.
    QTimer.singleShot(600 if not start_minimized else 0, reveal)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
