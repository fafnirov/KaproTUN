"""Provider-specific balances and refresh history; subscription URLs stay hidden."""
from datetime import datetime
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..core import storage
from ..core.i18n import tr
from ..core.subscription import SubscriptionInfo, source_id
from . import kit, tokens
from .merge_prompt import one_line


class SubscriptionsDialog(kit.OverlayDialog):
    refresh_requested = Signal()
    import_requested = Signal()

    def __init__(self, configs, parent=None):
        super().__init__(parent, wide=True)
        self.head("download", tr("subs.manage"), tr("subs.intro"))
        self._configs = configs
        self.entries = QVBoxLayout()
        self.entries.setContentsMargins(0, 0, 0, 0)
        self.body.addLayout(self.entries)
        self.add_actions([("close", tr("btn.close"), "secondary"),
                          ("refresh", tr("subs.refresh"), "primary")],
                         left=(("import", tr("srv.add"), "ghost"),),
                         handlers={"close": self.reject, "refresh": self.refresh_requested.emit,
                                   "import": self._import})
        self.enable_scrolling()
        self.refresh()

    def _import(self):
        self.accept()
        self.import_requested.emit()

    def set_busy(self, busy):
        self.buttons["refresh"].setEnabled(not busy)
        self.buttons["refresh"].setText(tr("srv.refreshing_tip" if busy else "subs.refresh"))

    def refresh(self):
        settings = storage.load_settings()
        urls = list(dict.fromkeys(settings.get("subscription_urls") or
                                 [settings.get("subscription_url", "")]))
        urls = [url for url in urls if url]
        statuses = settings.get("subscription_status") or {}
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(tokens.SP_2, tokens.SP_2, tokens.SP_2, tokens.SP_2)
        layout.setSpacing(tokens.SP_4)
        for i, url in enumerate(urls):
            sid = source_id(url)
            status = statuses.get(sid, {})
            title = f"{i + 1}. {one_line(urlsplit(url).hostname or tr('subs.provider'), 80)}"
            layout.addWidget(kit.label(title, "h2", wrap=True))
            count = sum(c.source == sid for c in self._configs)
            layout.addWidget(kit.label(tr("subs.servers", n=count), "hint"))
            data = status.get("userinfo")
            # The legacy global balance belongs only to the last imported URL.
            if not status and url == settings.get("subscription_url"):
                data = settings.get("subscription_userinfo")
            try:
                summary = SubscriptionInfo.from_dict(data).summary() if data else ""
            except (ValueError, TypeError, AttributeError):
                summary = ""
            layout.addWidget(kit.label(summary or tr("subs.no_info"), "dialogText", wrap=True))
            stamp = status.get("updated", 0)
            if not status and url == settings.get("subscription_url"):
                stamp = settings.get("subscription_last_refresh", 0)
            try:
                when = datetime.fromtimestamp(stamp).strftime("%d.%m.%Y %H:%M") if stamp else ""
            except (ValueError, OSError, OverflowError, TypeError):
                when = ""
            layout.addWidget(kit.label(tr("subs.updated", date=when) if when else tr("subs.never"),
                                       "hint", wrap=True))
            if status:
                layout.addWidget(kit.label(tr("subs.changes", added=status.get("added", 0),
                                             changed=status.get("changed", 0),
                                             missing=status.get("missing", 0)), "hint", wrap=True))
        if not urls:
            from .mascot import Mascot
            layout.addWidget(Mascot(tokens.SP_5 * 4), 0, Qt.AlignHCenter)
            layout.addWidget(kit.label(tr("subs.empty"), "dialogText", wrap=True))
        layout.addStretch()
        while self.entries.count():
            old = self.entries.takeAt(0).widget()
            if old:
                old.deleteLater()
        self.entries.addWidget(body)
        self.buttons["refresh"].setEnabled(bool(urls))
        if self.isVisible():
            self._cover_host()
