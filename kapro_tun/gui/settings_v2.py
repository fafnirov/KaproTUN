"""The Settings tab of the v2 interface, and the log viewer it opens.

Before v2 this was one long column: a check box, then a paragraph, then the
next check box — every explanation always open. Now each setting is one row
in a card of its section: a switch on the right, a one-line hint under the
title, and the full explanation unfolding in place for whoever wants it.

Nothing about what the settings do has changed: the handlers are the ones the
old page had, and the full texts are the same strings.
"""
from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import admin, app_log, autostart, sing_box_installer, storage
from ..core.controller import MODE_TUN, ConnectionManager
from ..core.i18n import tr
from . import kit, tokens
from .home_v2 import plural_domains
from .toast import show_toast

REPO_URL = "https://github.com/fafnirov/KaproTUN"
_LANGS = ("auto", "en", "ru")
_THEMES = ("auto", "dark", "light")


class SettingsPage(QWidget):
    sites_clicked = Signal()
    logs_clicked = Signal()
    diagnostics_clicked = Signal()
    bypass_apps_clicked = Signal()
    subscription_clicked = Signal()
    check_updates_requested = Signal()
    settings_changed = Signal()

    def __init__(self, manager: ConnectionManager, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        self._manager = manager
        s = manager.settings

        wrapper = QVBoxLayout(self)
        wrapper.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("ktPageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrapper.addWidget(scroll)
        content = QWidget()
        content.setObjectName("page")
        scroll.setWidget(content)
        col = QVBoxLayout(content)
        col.setContentsMargins(tokens.PAGE_PAD_X, tokens.PAGE_PAD_Y, tokens.PAGE_PAD_X, tokens.SP_6)
        col.setSpacing(tokens.SP_6)
        col.addWidget(kit.label(tr("mw.settings_title"), "h1"))

        # --- startup ---
        g = kit.Group(tr("set.sec_startup"))
        self.autostart_check = self._switch_row(
            g, "login", tr("mw.autostart_check"), tr("set.autostart_hint"), "",
            autostart.is_enabled(), self._on_autostart_changed)
        self.autoconnect_check = self._switch_row(
            g, "plug", tr("mw.autoconnect_check"), "", "",
            bool(s.get("autoconnect_on_launch", False)), self._on_autoconnect_changed)
        col.addWidget(g)

        # --- security ---
        g = kit.Group(tr("mw.security_section"))
        self._admin_row = g.add(kit.SettingRow("shield-check", ""))
        self._relaunch_btn = self._admin_row.add_button(tr("mw.relaunch_admin"), "primary")
        self._relaunch_btn.clicked.connect(self._on_relaunch_admin)
        self._refresh_admin_row()

        self.kill_check = self._switch_row(
            g, "shield-alert", tr("set.kill"), tr("set.kill_hint"), tr("mw.kill_switch_hint"),
            bool(s.get("kill_switch", False)), self._on_kill_switch_changed)

        # Built into the tunnel and always on, so this is a statement, not a
        # switch: on and locked. Until v4.0.0 it was a live check box that
        # wrote a setting nothing read.
        row = g.add(kit.SettingRow("network", tr("set.ipv6"), tr("set.ipv6_hint"), tr("mw.ipv6_hint")))
        row.add_control(kit.badge(tr("set.always")))
        self.ipv6_check = row.add_switch(True)
        self.ipv6_check.setEnabled(False)
        row.add_more()

        self.webrtc_check = self._switch_row(
            g, "video", tr("mw.webrtc_check"), tr("set.webrtc_hint"), tr("mw.webrtc_hint"),
            bool(s.get("webrtc_leak_protection", True)), self._on_webrtc_leak_changed)

        row = g.add(kit.SettingRow("droplet", tr("mw.leak_test_btn"), tr("set.leak_hint"),
                                   tr("mw.leak_test_hint")))
        self.leak_test_btn = row.add_button(tr("set.leak_btn"))
        self.leak_test_btn.clicked.connect(self._on_leak_test_clicked)
        row.add_more()

        self.ip_probe_check = self._switch_row(
            g, "eye", tr("set.ipprobe"), tr("set.ipprobe_hint"), tr("mw.ip_probe_hint"),
            bool(s.get("public_ip_probe", True)), self._on_ip_probe_changed)

        # DNS is always the system resolver (v3.1.1): nothing to switch, but
        # what that means for privacy must be said.
        row = g.add(kit.SettingRow("database", tr("set.dns"), tr("set.dns_hint"), tr("mw.dns_note")))
        row.add_more("info")
        col.addWidget(g)

        # --- routing ---
        g = kit.Group(tr("mw.routing_section"))
        self.ru_direct_check = self._switch_row(
            g, "route", tr("set.ru"), tr("set.ru_hint"), tr("mw.ru_direct_hint"),
            bool(s.get("route_ru_direct", True)), self._on_route_ru_direct_changed)
        self.games_check = self._switch_row(
            g, "gamepad", tr("set.games"), tr("set.games_hint"), tr("mw.games_hint"),
            bool(s.get("games_direct", True)), self._on_games_direct_changed)
        self.turbo_check = self._switch_row(
            g, "zap", tr("set.turbo"), tr("set.turbo_hint"), tr("mw.turbo_hint"),
            bool(s.get("high_speed", False)), self._on_high_speed_changed)
        self.bypass_apps_row = self._link_row(
            g, "apps", tr("set.bypass"), tr("set.bypass_hint"), self.bypass_apps_clicked)
        self.diagnostics_row = self._link_row(
            g, "gauge", tr("mw.diagnostics_btn"), tr("set.diag_hint"), self.diagnostics_clicked)
        self.netdebug_check = self._switch_row(
            g, "bug", tr("mw.netdebug_check"), tr("set.netdebug_hint"), tr("mw.netdebug_hint"),
            bool(s.get("network_debug", False)), self._on_network_debug_changed)
        col.addWidget(g)

        # --- language and theme ---
        g = kit.Group(tr("set.sec_look"))
        row = g.add(kit.SettingRow("language", tr("settings.language_label"), tr("set.lang_hint")))
        lang = str(s.get("language", "auto"))
        self.lang_select = row.add_select(
            [tr("settings.language_auto"), "English", "Русский"],
            _LANGS.index(lang) if lang in _LANGS else 0)
        self.lang_select.changed.connect(self._on_language_changed)
        row = g.add(kit.SettingRow("moon", tr("mw.theme_label"), tr("set.theme_hint")))
        theme = str(s.get("theme", "auto"))
        self.theme_select = row.add_select(
            [tr("mw.theme_auto"), tr("mw.theme_dark"), tr("mw.theme_light")],
            _THEMES.index(theme) if theme in _THEMES else 0)
        self.theme_select.changed.connect(self._on_theme_changed)
        col.addWidget(g)

        # --- subscription and lists ---
        g = kit.Group(tr("set.sec_sub"))
        self._sub_row = self._link_row(
            g, "download", tr("mw.sub_import_title"), self._sub_info_text(), self.subscription_clicked)
        self.minmeta_check = self._switch_row(
            g, "eye-off", tr("set.minmeta"), tr("set.minmeta_hint"), tr("mw.minmeta_hint"),
            bool(s.get("minimal_metadata", False)), self._on_minimal_metadata_changed)
        self._sites_row = g.add(kit.SettingRow("list", tr("set.sites"), tr("set.sites_hint")))
        self._sites_value = self._sites_row.add_value("")
        self._sites_row.make_link()
        self._sites_row.clicked.connect(self.sites_clicked)
        self.refresh_sites_count()
        self.logs_row = self._link_row(g, "file", tr("set.logs"), tr("set.logs_hint"), self.logs_clicked)
        col.addWidget(g)

        # --- about ---
        g = kit.Group(tr("set.sec_about"))
        sb = sing_box_installer.get_installed_version() or tr("mw.not_installed")
        self._about_hint = tr("set.version_hint", sb=sb)
        self._version_row = g.add(kit.SettingRow(
            "info", tr("set.version", version=__version__), self._about_hint))
        self.check_updates_btn = self._version_row.add_button(tr("mw.check_updates_btn"))
        self.check_updates_btn.clicked.connect(self._on_check_updates_clicked)
        row = g.add(kit.SettingRow("github", REPO_URL.split("//", 1)[1], tr("set.github_hint")))
        row.make_link(external=True)
        row.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(REPO_URL)))
        col.addWidget(g)
        col.addStretch(1)

    # --- building -----------------------------------------------------------

    @staticmethod
    def _switch_row(group: kit.Group, icon: str, title: str, hint: str, full: str,
                    checked: bool, on_toggled) -> kit.Switch:
        row = group.add(kit.SettingRow(icon, title, hint, full))
        switch = row.add_switch(checked)
        switch.toggled.connect(on_toggled)
        if full:
            row.add_more()
        return switch

    @staticmethod
    def _link_row(group: kit.Group, icon: str, title: str, hint: str, signal) -> kit.SettingRow:
        row = group.add(kit.SettingRow(icon, title, hint))
        row.make_link()
        row.clicked.connect(signal)
        return row

    # --- what the main window drives ---------------------------------------

    def refresh_sites_count(self) -> None:
        self._sites_value.setText(plural_domains(len(storage.load_sites())))

    def _sub_info_text(self) -> str:
        """The subscription row's hint: remaining traffic / expiry (if the
        provider sends Subscription-Userinfo) and how long ago it was
        refreshed; else what the row is for."""
        default = tr("set.sub_default_hint")
        try:
            from ..core.subscription import SubscriptionInfo, humanize_ago
            settings = storage.load_settings()
            data = settings.get("subscription_userinfo")
            base = default
            if data:
                base = SubscriptionInfo.from_dict(data).summary() or default
            ago = humanize_ago(int(settings.get("subscription_last_refresh", 0) or 0))
            return tr("mw.sub_updated_ago", base=base, ago=ago) if ago else base
        except Exception:
            return default

    def refresh_sub_info(self) -> None:
        self._sub_row.set_hint(self._sub_info_text())

    def set_update_status(self, text: str, accent: bool = False) -> None:
        """What the update check says, under the version. Empty text puts the
        engine version back."""
        self._version_row.set_hint(text or self._about_hint, "accent" if (accent and text) else "")

    # --- handlers (unchanged in meaning since before v2) --------------------

    def _on_autostart_changed(self, checked: bool) -> None:
        ok = autostart.enable(minimized=True) if checked else autostart.disable()
        if not ok:
            # The registry (or its counterpart) refused: show what is true.
            self.autostart_check.blockSignals(True)
            self.autostart_check.setChecked(not checked)
            self.autostart_check.blockSignals(False)

    def _on_autoconnect_changed(self, checked: bool) -> None:
        self._manager.update_settings(autoconnect_on_launch=checked)
        self.settings_changed.emit()

    def _on_kill_switch_changed(self, checked: bool) -> None:
        self._manager.update_settings(kill_switch=checked)
        if not checked:
            # Switching it off lifts the block now, not at the next connect:
            # with the tunnel down this is the way to get the internet back
            # without reconnecting.
            self._manager.release_killswitch()
        self.settings_changed.emit()

    def _on_webrtc_leak_changed(self, checked: bool) -> None:
        self._manager.update_settings(webrtc_leak_protection=checked)
        self.settings_changed.emit()

    def _on_games_direct_changed(self, checked: bool) -> None:
        self._manager.update_settings(games_direct=checked)

    def _on_minimal_metadata_changed(self, checked: bool) -> None:
        # Takes effect on the next subscription fetch.
        self._manager.update_settings(minimal_metadata=checked)

    def _on_network_debug_changed(self, checked: bool) -> None:
        # Immediate, no reconnect: the flag only gates what app_log.net() writes.
        self._manager.update_settings(network_debug=checked)
        app_log.set_net_debug(checked)
        app_log.log(f"[net-debug] {'enabled' if checked else 'disabled'} by user")

    def _on_route_ru_direct_changed(self, checked: bool) -> None:
        self._manager.update_settings(route_ru_direct=checked)
        self.settings_changed.emit()

    def _on_high_speed_changed(self, checked: bool) -> None:
        self._manager.update_settings(high_speed=checked)
        self.settings_changed.emit()

    def _on_ip_probe_changed(self, checked: bool) -> None:
        self._manager.update_settings(public_ip_probe=checked)
        self.settings_changed.emit()

    def _on_leak_test_clicked(self) -> None:
        """In TUN mode the route table already sends everything through
        sing-box, so the probes use the ordinary system stack. Not connected,
        they report the real IP and DNS as a baseline."""
        from .leak_test_dialog import LeakTestDialog
        LeakTestDialog(None, manager=self._manager, parent=self.window()).exec()

    def _on_theme_changed(self, index: int) -> None:
        """Save the theme and apply it to the running app. Widgets styled by
        the sheet follow at once; the ones that paint themselves read the
        tokens when they repaint, which the main window asks for on
        settings_changed."""
        theme = _THEMES[index]
        self._manager.update_settings(theme=theme)
        from .styles import get_qss
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(get_qss(theme))
        self.settings_changed.emit()

    def _on_language_changed(self, index: int) -> None:
        """Saved now, applied at the next start: every label was translated
        when it was built."""
        self._manager.update_settings(language=_LANGS[index])

    def _on_check_updates_clicked(self) -> None:
        self.check_updates_requested.emit()

    def _refresh_admin_row(self) -> None:
        row = self._admin_row
        if admin.is_admin():
            row.title.setText(tr("set.admin_yes"))
            row.set_hint(tr("set.admin_yes_hint"))
            row.set_tone("success", "shield-check")
            row.set_full("")
            self._relaunch_btn.setVisible(False)
        elif self._manager.planned_mode() != MODE_TUN:
            # macOS without root: proxy mode works as it is, so this is not a
            # warning. Say what it covers, and leave the full TUN one click
            # away for whoever accepts the password prompt that goes with it.
            row.title.setText(tr("set.proxy_mode"))
            row.set_hint(tr("set.proxy_mode_hint"))
            row.set_tone("", "info")
            row.set_full(tr("mw.proxy_mode_info"))
            if row.more is None:
                row.add_more()
            self._relaunch_btn.setText(tr("set.relaunch_tun"))
            self._relaunch_btn.setVisible(True)
        else:
            row.title.setText(tr("set.admin_no"))
            row.set_hint(tr("set.admin_no_hint"))
            row.set_tone("warning", "alert-triangle")
            row.set_full("")
            self._relaunch_btn.setText(tr("mw.relaunch_admin"))
            self._relaunch_btn.setVisible(True)

    def _on_relaunch_admin(self) -> None:
        rc = admin.relaunch_as_admin()
        # ShellExecuteW reports success as a value above 32; the macOS and
        # Linux helpers return 1. Testing "> 32" everywhere made every
        # successful relaunch off Windows show the failure dialog and leave
        # this unprivileged copy running next to the elevated one.
        launched = rc > 32 if sys.platform == "win32" else rc > 0
        if launched:
            # The elevated copy is starting; this one leaves.
            sys.exit(0)
        else:
            kit.notify(self, "alert-triangle", tr("mw.relaunch_failed_title"),
                       tr("mw.relaunch_failed_body"), ok_label=tr("dlg.ok"), tone="danger")


class _LogMarks(QSyntaxHighlighter):
    """Colours the bracketed mark a line starts with: [*] for what the client
    did, [!] for what went wrong."""

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        if len(text) < 3 or text[0] != "[" or text[2] != "]":
            return
        c = tokens.colors()
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(c.danger_text if text[1] == "!" else c.accent_text))
        self.setFormat(0, 3, fmt)


class LogsPage(QWidget):
    """Read-only view of the engine log."""

    back_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.PAGE_PAD_X, tokens.PAGE_PAD_Y,
                               tokens.PAGE_PAD_X, tokens.PAGE_PAD_Y)
        col.setSpacing(tokens.SP_3)

        head = QHBoxLayout()
        head.setSpacing(tokens.SP_2)
        self.back_btn = kit.Button("", "ghost", icon="chevron-left", size="sm",
                                   tooltip=tr("logs.back_tip"))
        self.back_btn.clicked.connect(self.back_clicked)
        head.addWidget(self.back_btn)
        head.addWidget(kit.label(tr("set.logs"), "h1"), stretch=1)
        self.copy_btn = kit.Button("", "ghost", icon="copy", size="sm", tooltip=tr("logs.copy_tip"))
        self.copy_btn.clicked.connect(self._on_copy)
        head.addWidget(self.copy_btn)
        self.clear_btn = kit.Button(tr("mw.logs_clear"), "ghost", icon="trash", size="sm")
        self.clear_btn.clicked.connect(self._on_clear)
        head.addWidget(self.clear_btn)
        col.addLayout(head)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("ktLog")
        self.log_view.setReadOnly(True)
        self.log_view.setFrameShape(QFrame.NoFrame)
        self.log_view.setMaximumBlockCount(5000)
        self._marks = _LogMarks(self.log_view.document())
        col.addWidget(self.log_view, stretch=1)
        col.addWidget(kit.label(tr("logs.caption"), "caption", wrap=True))

    def append(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.StyleChange and hasattr(self, "_marks"):
            self._marks.rehighlight()      # the marks' colours are the theme's
        super().changeEvent(event)

    def _on_copy(self) -> None:
        text = self.log_view.toPlainText()
        if not text.strip():
            show_toast(self.window(), tr("logs.empty"), kind="info")
            return
        # What is copied gets pasted into chats and issue trackers: share
        # links and UUIDs the engine may have quoted are cut out, as they are
        # in the log file.
        QApplication.clipboard().setText(app_log.redact(text))
        show_toast(self.window(), tr("logs.copied"), kind="success")

    def _on_clear(self) -> None:
        self.log_view.clear()
