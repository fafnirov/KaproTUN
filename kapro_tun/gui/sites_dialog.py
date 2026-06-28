"""Dialog for editing the list of domains that always route directly."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..core import storage
from ..core.i18n import tr


class SitesDialog(QDialog):
    """Edit the list of direct-routing domains (one per line)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("sites.window_title"))
        self.resize(520, 600)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(tr("sites.intro")))

        self.editor = QPlainTextEdit()
        self.editor.setPlainText("\n".join(storage.load_sites()))
        layout.addWidget(self.editor, stretch=1)

        actions_row = QHBoxLayout()
        reset_btn = QPushButton(tr("sites.reset_button"))
        reset_btn.clicked.connect(self._on_reset)
        actions_row.addWidget(reset_btn)
        actions_row.addStretch(1)
        layout.addLayout(actions_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
        )
        buttons.button(QDialogButtonBox.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.Save).setText(tr("sites.save"))
        buttons.button(QDialogButtonBox.Cancel).setText(tr("sites.cancel"))
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_save(self) -> None:
        lines = self.editor.toPlainText().splitlines()
        # Strip comments, whitespace, dedupe
        sites = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sites.append(line)
        storage.save_sites(sites)
        self.accept()

    def _on_reset(self) -> None:
        confirm = QMessageBox.question(
            self,
            tr("sites.reset_title"),
            tr("sites.reset_confirm"),
        )
        if confirm == QMessageBox.Yes:
            sites = storage.reset_sites_to_default()
            self.editor.setPlainText("\n".join(sites))
