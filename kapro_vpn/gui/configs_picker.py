"""Modal dialog for picking, adding, and removing proxy configs."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..core import storage
from ..core.parser import ProxyConfig
from .config_dialog import AddConfigDialog


class ConfigsPickerDialog(QDialog):
    """Pick a saved config, or add/remove from the saved list.

    On Accept, `selected_config()` returns the chosen ProxyConfig (or None).
    Mutations to the saved list happen in-place — caller should reload from
    `storage.load_configs()` after the dialog closes either way.
    """

    def __init__(
        self,
        configs: list[ProxyConfig],
        current_name: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Выбор конфига")
        self.resize(420, 520)
        self._configs = list(configs)
        self._current_name = current_name
        self._chosen: Optional[ProxyConfig] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("Конфиги")
        title.setObjectName("h2")
        layout.addWidget(title)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.list_widget, stretch=1)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        add_btn = QPushButton("＋ Добавить")
        add_btn.clicked.connect(self._on_add)
        remove_btn = QPushButton("Удалить")
        remove_btn.setObjectName("danger")
        remove_btn.clicked.connect(self._on_remove)

        button_row.addWidget(add_btn)
        button_row.addWidget(remove_btn)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        cancel_btn = QPushButton("Закрыть")
        cancel_btn.clicked.connect(self.reject)
        use_btn = QPushButton("Использовать")
        use_btn.setObjectName("primary")
        use_btn.clicked.connect(self._on_use)
        bottom_row.addWidget(cancel_btn)
        bottom_row.addWidget(use_btn)
        layout.addLayout(bottom_row)

        self._refresh()

    # --- helpers ----------------------------------------------------------

    def _refresh(self) -> None:
        self.list_widget.clear()
        for cfg in self._configs:
            srv = cfg.outbound.get("server", "?")
            port = cfg.outbound.get("server_port", "?")
            label = f"{cfg.name}\n{cfg.protocol.upper()}  ·  {srv}:{port}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, cfg)
            self.list_widget.addItem(item)
            if cfg.name == self._current_name:
                self.list_widget.setCurrentItem(item)
        if self.list_widget.currentRow() < 0 and self._configs:
            self.list_widget.setCurrentRow(0)

    def _selected_index(self) -> int:
        return self.list_widget.currentRow()

    # --- actions ----------------------------------------------------------

    def _on_add(self) -> None:
        dlg = AddConfigDialog(self)
        if dlg.exec() != AddConfigDialog.Accepted:
            return
        new_cfg = dlg.result_config()
        if new_cfg is None:
            return
        # Replace existing by name, else append
        for i, c in enumerate(self._configs):
            if c.name == new_cfg.name:
                self._configs[i] = new_cfg
                break
        else:
            self._configs.append(new_cfg)
        storage.save_configs(self._configs)
        self._current_name = new_cfg.name
        self._refresh()

    def _on_remove(self) -> None:
        idx = self._selected_index()
        if idx < 0:
            return
        cfg = self._configs[idx]
        confirm = QMessageBox.question(
            self, "Удалить", f"Удалить конфиг «{cfg.name}»?"
        )
        if confirm != QMessageBox.Yes:
            return
        del self._configs[idx]
        storage.save_configs(self._configs)
        self._refresh()

    def _on_use(self) -> None:
        idx = self._selected_index()
        if idx < 0:
            QMessageBox.information(self, "Конфиг", "Выбери конфиг из списка.")
            return
        self._chosen = self._configs[idx]
        self.accept()

    def _on_double_click(self, _item) -> None:
        self._on_use()

    # --- result -----------------------------------------------------------

    def selected_config(self) -> Optional[ProxyConfig]:
        return self._chosen
