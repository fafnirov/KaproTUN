"""Desktop UX regressions. No live VPN, providers or user settings are touched."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from kapro_tun.core import sing_box_config, storage
from kapro_tun.core.desktop_experience import (
    favorite_key,
    reconcile_favorites,
    subscription_delta,
)
from kapro_tun.core.i18n import set_locale, tr
from kapro_tun.core.parser import ProxyConfig


def server(name="Finland", source="sub:one", host="vpn.example"):
    return ProxyConfig(name, "vless", "", {"type": "vless", "server": host,
                       "server_port": 443, "uuid": "11111111-1111-1111-1111-111111111111"},
                       source=source)


class ExperienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_locale("ru")
        self.settings = {"reduce_motion": True, "desktop_experience_seen": True}
        for target, value in (("load_settings", lambda: dict(self.settings)),
                              ("update_settings", lambda data, **kw: self.settings.update(data)),
                              ("load_sites", lambda: ["example.ru"])):
            patcher = patch.object(storage, target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_favorites_survive_rename_and_key_rotation_and_remove_deleted(self):
        original = server()
        renamed = replace(original, name="Finland Premium")
        self.assertEqual(favorite_key(original), favorite_key(renamed))
        rotated = replace(original, outbound={**original.outbound, "server": "new.example"})
        self.assertEqual(reconcile_favorites([favorite_key(original)], [original], [rotated]),
                         [favorite_key(rotated)])
        self.assertEqual(reconcile_favorites([favorite_key(original)], [original], []), [])
        self.assertNotEqual(favorite_key(original), favorite_key(replace(original, source="sub:two")))

    def test_summary_counts_real_changes_and_retains_missing(self):
        a, b = server(), server("Germany", host="de.example")
        renamed = replace(a, name="Finland Premium")
        new = server("Netherlands", host="nl.example")
        self.assertEqual(subscription_delta([a, b], [renamed, b, new], [renamed, new], a.source),
                         {"added": 1, "changed": 1, "missing": 1})
        self.assertEqual(subscription_delta([a], [a], [a], a.source),
                         {"added": 0, "changed": 0, "missing": 0})

    def test_metadata_is_per_provider_and_clears_missing_headers(self):
        from kapro_tun.core.desktop_experience import remember_subscription
        from kapro_tun.core.subscription import SubscriptionInfo, source_id
        result = SimpleNamespace(userinfo=SubscriptionInfo(total=1024), configs=[])
        remember_subscription("https://provider.example/private-token", result, [], [])
        result.userinfo = None
        remember_subscription("https://other.example/private-token", result, [], [])
        statuses = self.settings["subscription_status"]
        self.assertEqual(len(statuses), 2)
        self.assertNotIn("private-token", str(statuses))
        remember_subscription("https://provider.example/private-token", result, [], [])
        self.assertIsNone(self.settings["subscription_status"][source_id("https://provider.example/private-token")]["userinfo"])

    def test_forced_sites_precede_direct_and_ru_in_both_modes(self):
        for build in (sing_box_config.build_config, sing_box_config.build_proxy_config):
            with patch.object(sing_box_config, "_ru_cidrs", return_value=["1.0.0.0/8"]):
                config = build(server(), ["example.com"], proxy_domains=["vpn.example.com"],
                               route_ru_direct=True)
            rules = config["route"]["rules"]
            forced = next(i for i, r in enumerate(rules) if r.get("domain_suffix") == ["vpn.example.com"])
            direct = next(i for i, r in enumerate(rules) if r.get("domain_suffix") == ["example.com"])
            ru = next(i for i, r in enumerate(rules) if r.get("ip_cidr") == ["1.0.0.0/8"])
            self.assertLess(forced, direct)
            self.assertLess(forced, ru)
            self.assertEqual(rules[forced]["outbound"], "proxy")

    def test_lockdown_keeps_direct_exceptions_disabled(self):
        cfg = sing_box_config.build_config(server(), ["direct.example"],
                                            proxy_domains=["vpn.example"],
                                            bypass_apps=["game.exe"], lockdown=True)
        self.assertFalse(any(r.get("domain_suffix") == ["direct.example"] or r.get("process_name")
                             for r in cfg["route"]["rules"]))

    def test_sites_normalize_and_conflict_does_not_write(self):
        from kapro_tun.gui.sites_dialog import SitesDialog, normalized_sites
        self.assertEqual(normalized_sites("https://Example.com/path\n*.example.com\n# comment"), ["example.com"])
        for bad in ("invalid domain", "https://user:secret@example.com", "x;bad", "file://server/file"):
            with self.assertRaises(ValueError):
                normalized_sites(bad)
        dlg = SitesDialog()
        dlg.editor.setPlainText("example.com")
        dlg.proxy_editor.setPlainText("example.com")
        with patch.object(storage, "save_sites") as save:
            dlg._on_save()
            save.assert_not_called()
        self.assertEqual(dlg.count.text(), tr("route.duplicate"))
        dlg.deleteLater()

    def test_reset_cancel_does_not_write(self):
        from kapro_tun.gui.sites_dialog import SitesDialog
        dlg = SitesDialog()
        with patch("kapro_tun.gui.kit.confirm", return_value=True), patch.object(storage, "save_sites") as save:
            dlg._on_reset()
            dlg.reject()
            save.assert_not_called()
        dlg.deleteLater()

    def test_home_preserves_session_timer_and_keyboard_favorite_action(self):
        from kapro_tun.gui.home_v2 import HomePage
        home = HomePage()
        configs = [server(), server("Germany", host="de.example")]
        home.set_config(configs[0])
        home.set_state("connected", "01:23")
        home.set_config(configs[0])
        self.assertEqual(home.status_label.meta.text(), "01:23")
        home.set_favorites(configs, {"Germany": 35}, active_name="Finland", connected=True)
        self.assertTrue(home.favorite_buttons[0].isChecked())
        chosen = []
        home.favorite_chosen.connect(chosen.append)
        QTest.keyClick(home.favorite_buttons[1], Qt.Key_Space)
        self.assertEqual(chosen, [configs[1]])
        home.set_favorites(configs, {}, busy=True)
        self.assertFalse(any(b.isEnabled() for b in home.favorite_buttons[:2]))
        home.deleteLater()

    def test_error_remains_after_config_refresh_and_retry_has_action(self):
        from kapro_tun.gui.home_v2 import HomePage
        home = HomePage()
        home.set_config(server())
        home.set_state("error", "Сервер недоступен")
        home.set_config(server())
        self.assertEqual(home.notice.text.text(), "Сервер недоступен")
        retry = Mock()
        home.connect_clicked.connect(retry)
        home.notice.action.click()
        retry.assert_called_once()
        home.deleteLater()

    def test_new_features_entry_and_provider_specific_expiry(self):
        from kapro_tun.gui.home_v2 import HomePage
        self.settings["desktop_experience_seen"] = False
        self.settings["subscription_userinfo"] = {"expire": 1}
        self.settings["subscription_status"] = {"sub:one": {"userinfo": None}}
        home = HomePage()
        home.set_config(server())
        self.assertTrue(home.sub_banner.isHidden())
        opened = Mock()
        home.whats_new_clicked.connect(opened)
        home.notice.action.click()
        opened.assert_called_once()
        home.deleteLater()

    def test_program_picker_search_keeps_checked_choices(self):
        from kapro_tun.core.installed_apps import InstalledApp, local_executable
        from kapro_tun.gui.installed_apps_dialog import InstalledAppsDialog, _Discover
        self.assertEqual(local_executable(r"\\server\share\game.exe"), "")
        self.assertEqual(local_executable(r"C:\Apps\uninstall.exe"), "")
        with patch.object(_Discover, "start"):
            dlg = InstalledAppsDialog()
        dlg._loaded([InstalledApp("Browser", r"C:\Apps\browser.exe"),
                     InstalledApp("Game", r"C:\Games\game.exe")])
        dlg.list.item(0).setCheckState(Qt.Checked)
        dlg.search.setText("game")
        self.assertTrue(dlg.list.item(0).isHidden())
        self.assertEqual(dlg.selected_apps(), ["browser.exe"])
        dlg.deleteLater()

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell syntax")
    def test_start_menu_script_parses_without_running_shortcuts(self):
        import ast
        import inspect
        import subprocess

        from kapro_tun.core import installed_apps, win_tools
        tree = ast.parse(inspect.getsource(installed_apps.discover))
        script = next(n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "script" for t in n.targets))
        parser = ("$errors = $null; $tokens = $null; "
                  "[System.Management.Automation.Language.Parser]::ParseInput("
                  "[Console]::In.ReadToEnd(), [ref]$tokens, [ref]$errors) | Out-Null; "
                  "if ($errors.Count) { $errors | Out-String | Write-Output; exit 1 }")
        result = subprocess.run([win_tools.tool("powershell"), "-NoProfile", "-NonInteractive",
                                 "-WindowStyle", "Hidden", "-Command", parser], input=script,
                                capture_output=True, text=True, timeout=10, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW,
                                startupinfo=win_tools.hidden_startupinfo(), env=win_tools.powershell_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_large_text_dialogs_wrap_and_keep_actions_reachable(self):
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QWidget

        from kapro_tun.gui import styles, tokens
        from kapro_tun.gui.mascot import WhatsNewDialog
        from kapro_tun.gui.sites_dialog import SitesDialog
        if sys.platform == "win32":
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
            self.app.setFont(QFont("Segoe UI", 9))
        previous = tokens.text_scale
        tokens.set_text_scale(2)
        self.app.setStyleSheet(styles.get_qss("dark"))
        host = QWidget()
        host.resize(460, 640)
        host.show()
        try:
            for cls, action in ((SitesDialog, "save"), (WhatsNewDialog, "close")):
                dlg = cls(host)
                dlg.show()
                self.app.processEvents()
                area = dlg._scroll_body
                self.assertLessEqual(area.widget().width(), area.viewport().width())
                self.assertLessEqual(dlg.card.height(), host.height())
                button = dlg.buttons[action]
                area.ensureWidgetVisible(button)
                self.app.processEvents()
                point = button.mapTo(area.viewport(), button.rect().center())
                self.assertTrue(area.viewport().rect().contains(point))
                dlg.hide()
                dlg.deleteLater()
        finally:
            host.hide()
            host.deleteLater()
            tokens.set_text_scale(previous)
            self.app.setStyleSheet(styles.get_qss("dark"))

    def test_mascot_is_bundled_and_reduced_motion_stays_static(self):
        from kapro_tun.gui.mascot import Mascot
        mascot = Mascot()
        self.assertFalse(mascot.pixmap().isNull())
        mascot.show()
        self.app.processEvents()
        self.assertEqual(mascot._fade.state(), QAbstractAnimation.Stopped)
        mascot.hide()
        mascot.deleteLater()


if __name__ == "__main__":
    unittest.main()
