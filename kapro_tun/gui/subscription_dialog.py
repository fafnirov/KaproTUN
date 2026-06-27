"""Modal dialog for one-shot subscription URL imports.

Two paths:

1. URL fetch — paste a subscription URL, KaproTUN downloads & parses it.
   Auto-retries through the local xray tunnel if the direct fetch trips
   the RU DPI signature.

2. Manual paste fallback — for sites that reject every TLS client we
   send (REALITY-fronted or IP-whitelisted subscription endpoints, e.g.
   gmailvpn.ru), the user opens the URL in their browser and pastes the
   raw response body into a textarea. Same parser as the URL path, so
   the result is indistinguishable from a successful fetch.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..core import storage
from ..core.parser import ProxyConfig
from ..core.subscription import (
    FetchError,
    SubscriptionResult,
    classify_fetch_error,
    import_with_dpi_fallback,
    is_https_url,
    result_from_body,
)


class _SubscriptionFetcher(QThread):
    succeeded = Signal(object)  # SubscriptionResult
    failed = Signal(object)  # FetchError (classified cause)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url

    def run(self) -> None:
        try:
            # Direct first, automatic fallback through the sing-box health-proxy
            # (127.0.0.1:HEALTH_PROXY_PORT, tunnels via the active VPN) if the
            # direct fetch looks DPI-blocked AND the VPN is connected. v3.1.2.
            result = import_with_dpi_fallback(self._url)
            self.succeeded.emit(result)
        except Exception as e:
            # Classify the failure so the dialog shows the real cause
            # (a 404 is a dead link, not a REALITY block).
            self.failed.emit(classify_fetch_error(e))


class SubscriptionDialog(QDialog):
    """Paste a subscription URL, fetch it, preview results, save."""

    def __init__(self, parent=None, prefill_url: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("Импорт по подписке")
        self.resize(620, 520)
        self._result: Optional[SubscriptionResult] = None
        self._fetcher: Optional[_SubscriptionFetcher] = None
        # If we were opened because the user pasted a subscription URL
        # into the wrong dialog, kick off the fetch automatically after
        # showing — they already expressed clear intent.
        self._autostart_fetch: bool = bool(prefill_url)

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

        # --- URL row ---
        layout.addWidget(QLabel("URL подписки:"))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://provider.example/sub/abc123")
        # Precedence: explicit prefill (from auto-redirect) > last-used URL
        last_url = storage.load_settings().get("subscription_url", "")
        if prefill_url:
            self.url_edit.setText(prefill_url)
        elif last_url:
            self.url_edit.setText(last_url)
        layout.addWidget(self.url_edit)

        fetch_row = QHBoxLayout()
        self.fetch_btn = QPushButton("Загрузить и распарсить")
        self.fetch_btn.setObjectName("primary")
        self.fetch_btn.clicked.connect(self._on_fetch)
        fetch_row.addWidget(self.fetch_btn)
        # Manual paste toggle — always available, also auto-revealed on fail.
        self.manual_toggle = QPushButton("Вставить вручную ▾")
        self.manual_toggle.setObjectName("ghost")
        self.manual_toggle.setCheckable(True)
        self.manual_toggle.toggled.connect(self._on_manual_toggled)
        fetch_row.addWidget(self.manual_toggle)
        fetch_row.addStretch(1)
        layout.addLayout(fetch_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # --- Manual-paste section (hidden by default) ---
        self.manual_frame = QFrame()
        self.manual_frame.setObjectName("manual_frame")
        manual_lay = QVBoxLayout(self.manual_frame)
        manual_lay.setContentsMargins(0, 8, 0, 0)
        manual_lay.setSpacing(6)

        manual_hint = QLabel(
            "Если сайт провайдера не открывается из приложения, открой URL "
            "в браузере, скопируй полностью ответ страницы и вставь сюда. "
            "Подойдёт как base64-строка, так и обычный список share-ссылок "
            "(vless://, trojan://, …) — каждая на своей строке."
        )
        manual_hint.setWordWrap(True)
        manual_hint.setObjectName("dim")
        manual_lay.addWidget(manual_hint)

        self.manual_edit = QPlainTextEdit()
        self.manual_edit.setPlaceholderText(
            "Сюда — содержимое страницы подписки\n"
            "(одна большая base64-строка ИЛИ список share-URL построчно)"
        )
        self.manual_edit.setMinimumHeight(160)
        manual_lay.addWidget(self.manual_edit)

        manual_btn_row = QHBoxLayout()
        self.manual_parse_btn = QPushButton("Распарсить вставленное")
        self.manual_parse_btn.setObjectName("primary")
        self.manual_parse_btn.clicked.connect(self._on_parse_pasted)
        manual_btn_row.addWidget(self.manual_parse_btn)
        manual_btn_row.addStretch(1)
        manual_lay.addLayout(manual_btn_row)

        self.manual_frame.setVisible(False)
        layout.addWidget(self.manual_frame)

        layout.addStretch(1)

        # --- Save / cancel ---
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

    def showEvent(self, event) -> None:
        """Auto-kick the fetch when we were opened with a prefilled URL
        (typical case: user pasted a sub URL into the wrong dialog and
        we redirected them here). Run via QTimer.singleShot so the
        window is fully painted before the fetcher thread starts.
        """
        super().showEvent(event)
        if self._autostart_fetch:
            self._autostart_fetch = False  # one-shot
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._on_fetch)

    # --- actions ----------------------------------------------------------

    def _on_fetch(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "URL", "Введи URL подписки.")
            return
        # HTTPS-only: a subscription URL is a bearer credential and http://
        # sends it (and the server list it returns) in cleartext. Block it
        # with an actionable message instead of fetching insecurely. The
        # manual-paste path stays available for any edge case.
        if url.lower().startswith("http://"):
            QMessageBox.warning(
                self, "Небезопасная ссылка",
                "HTTP-подписки небезопасны — ссылка и список серверов "
                "передаются открытым текстом и могут быть перехвачены. "
                "Попросите у провайдера HTTPS-ссылку (https://).",
            )
            return
        if not is_https_url(url):
            QMessageBox.warning(
                self, "URL", "Введи корректный https:// URL подписки.")
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
        self._show_result(result)

    def _on_fetch_failed(self, info: FetchError) -> None:
        self.fetch_btn.setEnabled(True)
        self.status_label.setText(
            f"<span style='color:#ef4444'>✕ {info.title}</span><br>"
            f"<span style='color:#fbbf24'>{info.detail}</span><br>"
            f"<span style='color:#a1a1aa; font-size:9pt'>{info.raw}</span>"
        )
        # Only push the manual-paste escape hatch when it could actually
        # help (DPI / whitelist / timeout). For a 404 or server error it
        # can't — don't send the user down a dead end.
        if info.suggest_manual:
            if not self.manual_toggle.isChecked():
                self.manual_toggle.setChecked(True)
            self.manual_edit.setFocus()

    def _on_manual_toggled(self, checked: bool) -> None:
        self.manual_frame.setVisible(checked)
        self.manual_toggle.setText(
            "Вставить вручную ▴" if checked else "Вставить вручную ▾"
        )

    def _on_parse_pasted(self) -> None:
        body = self.manual_edit.toPlainText().strip()
        if not body:
            QMessageBox.warning(
                self, "Пусто",
                "Сначала вставь содержимое страницы подписки.",
            )
            return
        result = result_from_body(body)
        self._show_result(result, source_label="вставленный текст")

    def _show_result(
        self,
        result: SubscriptionResult,
        source_label: Optional[str] = None,
    ) -> None:
        self._result = result
        if result.configs:
            msg = (
                f"<span style='color:#16a34a; font-weight:600'>"
                f"✓ Найдено {len(result.configs)} конфигов</span>"
            )
            if source_label:
                msg += (
                    f"<br><span style='color:#a1a1aa'>"
                    f"Источник: {source_label}.</span>"
                )
            elif result.via_proxy:
                msg += (
                    "<br><span style='color:#a1a1aa'>"
                    "Скачано через активный туннель (сайт провайдера "
                    "недоступен напрямую).</span>"
                )
            if result.errors:
                msg += (
                    f"<br><span style='color:#a1a1aa'>"
                    f"Пропущено {len(result.errors)} строк "
                    f"(нераспознанный формат)</span>"
                )
            if result.placeholders:
                msg += (
                    f"<br><span style='color:#a1a1aa'>"
                    f"Пропущена заглушка от провайдера "
                    f"({len(result.placeholders)} шт., нерабочий сервер)</span>"
                )
            if result.userinfo is not None and result.userinfo.summary():
                msg += (
                    f"<br><span style='color:#fbbf24'>"
                    f"Подписка: {result.userinfo.summary()}</span>"
                )
            self.status_label.setText(msg)
            self.save_btn.setEnabled(True)
        elif result.placeholders:
            # Parsed fine, but every entry was a provider stub (e.g.
            # gmailvpn's 0.0.0.0:1 "App not supported"). Explain instead
            # of silently importing a dead server.
            n = len(result.placeholders)
            self.status_label.setText(
                "<span style='color:#ef4444'>✕ Провайдер вернул только "
                f"заглушку ({n}), а не рабочие серверы.</span><br>"
                "<span style='color:#fbbf24'>Обычно это значит: подписка не "
                "активирована / не оплачена, либо провайдер не отдаёт конфиги "
                "стороннему клиенту (нужен их Clash / официальное приложение). "
                "Проверь статус подписки у провайдера.</span>"
            )
        else:
            self.status_label.setText(
                "<span style='color:#ef4444'>✕ В ответе не найдено ни одного "
                "share-URL (vless://, trojan://, vmess://, ss://, hysteria2://). "
                "Проверь, что скопировал страницу полностью.</span>"
            )

    def _on_accept(self) -> None:
        if not self._result or not self._result.configs:
            return
        # Persist the URL for next time, plus the provider's remaining-
        # traffic / expiry info so Settings can show it without re-fetching.
        url = self.url_edit.text().strip()
        settings = storage.load_settings()
        settings["subscription_url"] = url
        # Track every distinct subscription URL we've imported from, so the
        # picker's «Обновить» can re-fetch them all — not just the last one.
        urls = [u for u in (settings.get("subscription_urls") or []) if u]
        if url and url not in urls:
            urls.append(url)
        settings["subscription_urls"] = urls
        if self._result.userinfo is not None:
            settings["subscription_userinfo"] = self._result.userinfo.to_dict()
        storage.save_settings(settings)
        self.accept()

    # --- result -----------------------------------------------------------

    def imported_configs(self) -> list[ProxyConfig]:
        return list(self._result.configs) if self._result else []
