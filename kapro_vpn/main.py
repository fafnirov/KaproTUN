"""Application entry point."""
from __future__ import annotations

import signal
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QSplashScreen

from .gui import icons
from .gui.main_window import MainWindow
from .gui.styles import DARK_QSS


def main() -> int:
    # Let Ctrl+C in the terminal kill the app cleanly
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QApplication(sys.argv)
    app.setApplicationName("KaproVPN")
    app.setOrganizationName("KaproVPN")
    app.setStyleSheet(DARK_QSS)
    app.setWindowIcon(icons.app_icon())
    # Don't exit when the user clicks X — we hide to tray instead.
    # Real exit goes through the tray-menu "Выход" item, which calls
    # QApplication.quit() explicitly.
    app.setQuitOnLastWindowClosed(False)

    # Splash screen masks the ~200 ms of PySide initialization
    splash = QSplashScreen(icons.splash_pixmap(320), Qt.WindowStaysOnTopHint)
    splash.show()
    splash.showMessage(
        "Запуск…",
        Qt.AlignBottom | Qt.AlignHCenter,
        Qt.white,
    )
    app.processEvents()

    window = MainWindow()
    # Tiny delay before swapping splash → window, so the splash is
    # actually visible. Without this, fast machines flash it for 1 frame.
    QTimer.singleShot(600, lambda: (window.show(), splash.finish(window)))

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
