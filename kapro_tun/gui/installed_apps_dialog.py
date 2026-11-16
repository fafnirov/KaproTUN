"""Searchable multi-selection of registered programs for the direct route."""
from PySide6.QtCore import QFileInfo, QSize, Qt, Signal
from PySide6.QtWidgets import QFileIconProvider, QListWidget, QListWidgetItem

from ..core import installed_apps
from ..core.i18n import tr
from . import kit, tokens
from .background import Background
from .merge_prompt import one_line


class _Discover(Background):
    done = Signal(object)

    def _work(self):
        apps = installed_apps.discover()
        return lambda: self.done.emit(apps)


class InstalledAppsDialog(kit.OverlayDialog):
    def __init__(self, existing=(), parent=None):
        super().__init__(parent, wide=True)
        self.head("apps", tr("apps.choose"), tr("apps.intro"))
        self.search = kit.Input(tr("apps.search"), icon="search")
        self.search.setAccessibleName(tr("apps.search"))
        self.search.textChanged.connect(self._filter)
        self.add_widget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("ktPrograms")
        self.list.setAccessibleName(tr("apps.choose"))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setMinimumHeight(tokens.ROW_H * 3)
        self.list.setIconSize(QSize(tokens.ICON_MD, tokens.ICON_MD))
        self.add_widget(self.list)
        self.note = kit.label(tr("apps.loading"), "hint", wrap=True)
        self.add_widget(self.note)
        self.add_actions([("cancel", tr("btn.cancel"), "secondary"),
                          ("add", tr("apps.add_selected"), "primary")],
                         handlers={"cancel": self.reject, "add": self.accept})
        self.enable_scrolling()
        self.buttons["add"].setEnabled(False)
        self.list.itemChanged.connect(self._selection_changed)
        self._existing = set(existing)
        self._worker = _Discover()
        self._worker.done.connect(self._loaded)
        self._worker.crashed.connect(self._failed)
        self._worker.start()

    def _loaded(self, apps):
        self._worker = None
        provider = QFileIconProvider()
        for app in apps:
            item = QListWidgetItem(provider.icon(QFileInfo(app.path)),
                                   f"{one_line(app.name, 70)}\n{app.executable}")
            item.setData(Qt.UserRole, app.executable)
            item.setToolTip(app.path)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if app.executable in self._existing else Qt.Unchecked)
            self.list.addItem(item)
        self.note.setText(tr("apps.note"))
        self._filter()
        self._selection_changed()

    def _failed(self, _error):
        self._worker = None
        self.note.setText(tr("apps.failed"))

    def _selection_changed(self):
        self.buttons["add"].setEnabled(bool(self.selected_apps()))

    def _filter(self):
        query = self.search.text().casefold().strip()
        visible = 0
        for i in range(self.list.count()):
            item = self.list.item(i)
            hidden = query not in item.text().casefold()
            item.setHidden(hidden)
            visible += not hidden
        if self._worker is None:
            self.note.setText(tr("apps.note" if visible else "apps.no_results"))

    def selected_apps(self):
        return sorted({self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                       if self.list.item(i).checkState() == Qt.Checked})
