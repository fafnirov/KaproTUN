"""UI regressions isolated from user settings, VPN and system commands."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget
from kapro_tun.gui.kit import SettingRow
from kapro_tun.gui.tray import TrayManager


class WindowLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_setting_hints_never_show_as_independent_windows(self):
        shown = []

        class Watch(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow():
                    shown.append(obj.objectName())
                return False

        watch = Watch()
        self.app.installEventFilter(watch)
        try:
            row = SettingRow('network', 'Title', 'Visible hint')
        finally:
            self.app.removeEventFilter(watch)
        self.assertEqual(shown, [])
        self.assertIs(row.hint.window(), row)
        row.show()
        self.app.processEvents()
        self.assertTrue(row.hint.isVisible())
        row.hide()
        row.deleteLater()

    def test_hide_dismisses_tray_menu_and_submenu(self):
        owner = QWidget()
        tray = TrayManager(owner)
        self.assertIs(tray.menu.parentWidget(), owner)
        tray.menu.show()
        tray.configs_menu.show()
        self.assertTrue(tray.menu.isVisible())
        self.assertTrue(tray.configs_menu.isVisible())
        tray.hide()
        self.assertFalse(tray.menu.isVisible())
        self.assertFalse(tray.configs_menu.isVisible())
        owner.deleteLater()

    def test_quit_hides_menu_before_waiting_for_workers(self):
        from kapro_tun.gui.main_window import MainWindow
        owner = QWidget()
        tray = TrayManager(owner)
        tray.menu.show()
        tray.configs_menu.show()
        manager = Mock(killswitch_held=False)
        manager.is_connected.return_value = False

        def wait_for_workers():
            self.assertFalse(tray.menu.isVisible())
            self.assertFalse(tray.configs_menu.isVisible())

        window = SimpleNamespace(
            _really_quitting=False, tray=tray, manager=manager,
            _dns_watchdog=Mock(), _net_watchdog=Mock(),
            _join_workers=Mock(side_effect=wait_for_workers),
        )
        with patch.object(QApplication, 'quit') as quit_app:
            MainWindow._on_quit_for_real(window)
            MainWindow._on_quit_for_real(window)
            quit_app.assert_called_once()
        window._join_workers.assert_called_once()
        manager.disconnect.assert_not_called()
        manager.release_killswitch.assert_not_called()
        owner.deleteLater()


if __name__ == '__main__':
    unittest.main()
