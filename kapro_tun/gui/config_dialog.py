"""Dialog for adding a new proxy config from a share URL."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..core.i18n import tr
from ..core.parser import ParseError, ProxyConfig, parse


class AddConfigDialog(QDialog):
    """Paste a trojan://, vless://, vmess://, ss://, hysteria2:// URL."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("cfg.window_title"))
        self.resize(640, 360)
        self._result: Optional[ProxyConfig] = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(tr("cfg.paste_url_label")))

        self.url_edit = QPlainTextEdit()
        self.url_edit.setPlaceholderText(
            "trojan://<password>@host.example:443?security=tls&sni=host.example#Name"
        )
        layout.addWidget(self.url_edit, stretch=1)

        parse_row = QHBoxLayout()
        self.parse_btn = QPushButton(tr("cfg.parse_button"))
        self.parse_btn.clicked.connect(self._on_parse)
        parse_row.addWidget(self.parse_btn)
        parse_row.addStretch(1)
        self.detected_label = QLabel("")
        self.detected_label.setObjectName("muted")
        parse_row.addWidget(self.detected_label)
        layout.addLayout(parse_row)

        layout.addWidget(QLabel(tr("cfg.name_label")))
        self.name_edit = QLineEdit()
        layout.addWidget(self.name_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
        )
        buttons.button(QDialogButtonBox.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.Save).setText(tr("cfg.save_button"))
        buttons.button(QDialogButtonBox.Cancel).setText(tr("cfg.cancel_button"))
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # Result code returned via QDialog.done() when the user picks
    # "Open subscription import instead". The parent (configs_picker)
    # watches for this to swap dialogs without bothering the user with
    # a second click.
    SWITCH_TO_SUBSCRIPTION = 100

    def _on_parse(self) -> None:
        text = self.url_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, tr("cfg.empty_title"), tr("cfg.empty_body"))
            return

        # http(s)://-URL ≠ share-URL. The most common mistake is pasting
        # a provider's subscription URL here instead of into "Импорт по
        # подписке". Offer to switch dialogs in one click.
        if text.lower().startswith(("http://", "https://")):
            choice = QMessageBox.question(
                self, tr("cfg.looks_like_sub_title"),
                tr("cfg.looks_like_sub_body"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if choice == QMessageBox.Yes:
                # Stash the URL so the caller can pre-fill the subscription
                # dialog without making the user paste again.
                self._pending_subscription_url = text
                self.done(self.SWITCH_TO_SUBSCRIPTION)
            return

        try:
            cfg = parse(text)
        except ParseError as e:
            QMessageBox.critical(self, tr("cfg.parse_error_title"), str(e))
            self.detected_label.setText("")
            return
        self._result = cfg
        self.name_edit.setText(cfg.name)
        self.detected_label.setText(
            tr("cfg.detected_protocol", protocol=cfg.protocol)
            + f" · {cfg.outbound.get('server', '?')}:{cfg.outbound.get('server_port', '?')}"
        )

    def pending_subscription_url(self) -> Optional[str]:
        """Set when the user agreed to switch to subscription import.
        configs_picker reads this to pre-fill the subscription dialog.
        """
        return getattr(self, "_pending_subscription_url", None)

    def _on_accept(self) -> None:
        if self._result is None:
            self._on_parse()
            if self._result is None:
                return
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, tr("cfg.name_required_title"), tr("cfg.name_required_body"))
            return
        self._result.name = name
        self.accept()

    def result_config(self) -> Optional[ProxyConfig]:
        return self._result
