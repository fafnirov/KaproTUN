"""Modal dialog for one-shot subscription URL imports.

Pasted URL → background download → parse share-URLs → preview count →
user confirms → configs merged into the saved list.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..core import storage
from ..core.parser import ProxyConfig
from ..core.subscription import SubscriptionResult, import_subscription


class _SubscriptionFetcher(QThread):
    succeeded = Signal(object)  # SubscriptionResult
    failed = Signal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url

    def run(self) -> None:
        try:
            result = import_subscription(self._url)
            self.succeeded.emit(result)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class SubscriptionDialog(QDialog):
    """Paste a subscription URL, fetch it, preview results, save."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Импорт по подписке")
        self.resize(560, 280)
        self._result: Optional[SubscriptionResult] = None
        self._fetcher: Optional[_SubscriptionFetcher] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        title = QLabel("Импорт конфигов из подписки")
        title.setObjectName("h2")
        layout.addWidget(title)

        hint = QLabel(
            "Многие провайдеры выдают одну ссылку, по которой возвращается "
            "список всех их серверов (обычно в base64). Вставь её сюда — "
            "все конфиги добавятся одним кликом."
        )
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        layout.addWidget(hint)

        # URL row
        layout.addWidget(QLabel("URL подписки:"))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://provider.example/sub/abc123")
        # Pre-fill with the last-used URL
        last_url = storage.load_settings().get("subscription_url", "")
        if last_url:
            self.url_edit.setText(last_url)
        layout.addWidget(self.url_edit)

        # Fetch action
        fetch_row = QHBoxLayout()
        self.fetch_btn = QPushButton("Загрузить и распарсить")
        self.fetch_btn.setObjectName("primary")
        self.fetch_btn.clicked.connect(self._on_fetch)
        fetch_row.addWidget(self.fetch_btn)
        fetch_row.addStretch(1)
        layout.addLayout(fetch_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addStretch(1)

        # Save / cancel
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel,
        )
        self.save_btn = buttons.button(QDialogButtonBox.Save)
        self.save_btn.setObjectName("primary")
        self.save_btn.setText("Добавить в список")
        self.save_btn.setEnabled(False)
        buttons.button(QDialogButtonBox.Cancel).setText("Закрыть")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # --- actions ----------------------------------------------------------

    def _on_fetch(self) -> None:
        url = self.url_edit.text().strip()
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            QMessageBox.warning(self, "URL", "Введи корректный http:// или https:// URL.")
            return
        self.fetch_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.status_label.setText("Загрузка…")
        self._fetcher = _SubscriptionFetcher(url, parent=self)
        self._fetcher.succeeded.connect(self._on_fetched)
        self._fetcher.failed.connect(self._on_fetch_failed)
        self._fetcher.start()

    def _on_fetched(self, result: SubscriptionResult) -> None:
        self.fetch_btn.setEnabled(True)
        self._result = result
        if result.configs:
            msg = (
                f"<span style='color:#16a34a; font-weight:600'>"
                f"✓ Найдено {len(result.configs)} конфигов</span>"
            )
            if result.errors:
                msg += (
                    f"<br><span style='color:#a1a1aa'>"
                    f"Пропущено {len(result.errors)} строк "
                    f"(нераспознанный формат)</span>"
                )
            self.status_label.setText(msg)
            self.save_btn.setEnabled(True)
        else:
            self.status_label.setText(
                "<span style='color:#ef4444'>✕ В ответе не найдено ни одного "
                "share-URL. Проверь что ссылка правильная.</span>"
            )

    def _on_fetch_failed(self, msg: str) -> None:
        self.fetch_btn.setEnabled(True)
        self.status_label.setText(
            f"<span style='color:#ef4444'>✕ Не удалось загрузить:</span><br>"
            f"<span style='color:#a1a1aa; font-size:9pt'>{msg}</span>"
        )

    def _on_accept(self) -> None:
        if not self._result or not self._result.configs:
            return
        # Persist the URL for next time
        settings = storage.load_settings()
        settings["subscription_url"] = self.url_edit.text().strip()
        storage.save_settings(settings)
        self.accept()

    # --- result -----------------------------------------------------------

    def imported_configs(self) -> list[ProxyConfig]:
        return list(self._result.configs) if self._result else []
