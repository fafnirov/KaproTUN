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

from ..core.parser import ParseError, ProxyConfig, parse


class AddConfigDialog(QDialog):
    """Paste a trojan://, vless://, vmess://, ss://, hysteria2:// URL."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Добавить конфиг")
        self.resize(640, 360)
        self._result: Optional[ProxyConfig] = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "Вставь ссылку на конфиг "
            "(trojan://, vless://, vmess://, ss://, hysteria2://):"
        ))

        self.url_edit = QPlainTextEdit()
        self.url_edit.setPlaceholderText(
            "trojan://1eb7503f-...@host.example:28193?security=tls&sni=...#Имя"
        )
        layout.addWidget(self.url_edit, stretch=1)

        parse_row = QHBoxLayout()
        self.parse_btn = QPushButton("Распарсить")
        self.parse_btn.clicked.connect(self._on_parse)
        parse_row.addWidget(self.parse_btn)
        parse_row.addStretch(1)
        self.detected_label = QLabel("")
        self.detected_label.setObjectName("muted")
        parse_row.addWidget(self.detected_label)
        layout.addLayout(parse_row)

        layout.addWidget(QLabel("Имя (как будет отображаться в списке):"))
        self.name_edit = QLineEdit()
        layout.addWidget(self.name_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
        )
        buttons.button(QDialogButtonBox.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_parse(self) -> None:
        text = self.url_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Пусто", "Сначала вставь URL.")
            return
        try:
            cfg = parse(text)
        except ParseError as e:
            QMessageBox.critical(self, "Ошибка парсинга", str(e))
            self.detected_label.setText("")
            return
        self._result = cfg
        self.name_edit.setText(cfg.name)
        self.detected_label.setText(
            f"Протокол: {cfg.protocol} · "
            f"{cfg.outbound.get('server', '?')}:{cfg.outbound.get('server_port', '?')}"
        )

    def _on_accept(self) -> None:
        if self._result is None:
            self._on_parse()
            if self._result is None:
                return
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Имя", "Укажи имя для конфига.")
            return
        self._result.name = name
        self.accept()

    def result_config(self) -> Optional[ProxyConfig]:
        return self._result
