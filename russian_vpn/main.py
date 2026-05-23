"""Application entry point."""
from __future__ import annotations

import signal
import sys

from PySide6.QtWidgets import QApplication

from .gui.main_window import MainWindow
from .gui.styles import DARK_QSS


def main() -> int:
    # Let Ctrl+C in the terminal kill the app cleanly
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QApplication(sys.argv)
    app.setApplicationName("Russian VPN")
    app.setOrganizationName("russian-vpn")
    app.setStyleSheet(DARK_QSS)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
