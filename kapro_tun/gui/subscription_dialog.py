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
from ..core.i18n import tr
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
        self.setWindowTitle(tr("sub.title"))
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

        title = QLabel(tr("sub.heading"))
        title.setObjectName("h2")
        layout.addWidget(title)

        hint = QLabel(tr("sub.intro_hint"))
        hint.setWordWrap(True)
        hint.setObjectName("dim")
        layout.addWidget(hint)

        # --- URL row ---
        layout.addWidget(QLabel(tr("sub.url_label")))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://provider.example/sub/abc123")
        # Precedence: explicit prefill (from auto-redirect) > last-used URL
        last_url = storage.load_settings().get("subscription_url", "")
        if prefill_url:
            self.url_edit.setText(prefill_url)
        elif last_url:
            self.url_edit.setText(last_url)
        layout.addWidget(self.url_edit)

        # --- Quick import: clipboard / QR (v3.1.9) ---
        # A provider's QR usually encodes either a subscription URL or a single
        # share-link; both routes are handled by _ingest_text.
        import_row = QHBoxLayout()
        import_row.setSpacing(8)
        self.paste_btn = QPushButton(tr("sub.paste_btn"))
        self.paste_btn.setObjectName("ghost")
        self.paste_btn.setToolTip(tr("sub.paste_tooltip"))
        self.paste_btn.clicked.connect(self._on_paste_clipboard)
        import_row.addWidget(self.paste_btn)
        self.qr_btn = QPushButton(tr("sub.qr_btn"))
        self.qr_btn.setObjectName("ghost")
        self.qr_btn.setToolTip(tr("sub.qr_tooltip"))
        self.qr_btn.clicked.connect(self._on_import_qr)
        import_row.addWidget(self.qr_btn)
        import_row.addStretch(1)
        layout.addLayout(import_row)

        fetch_row = QHBoxLayout()
        self.fetch_btn = QPushButton(tr("sub.fetch"))
        self.fetch_btn.setObjectName("primary")
        self.fetch_btn.clicked.connect(self._on_fetch)
        fetch_row.addWidget(self.fetch_btn)
        # Manual paste toggle — always available, also auto-revealed on fail.
        self.manual_toggle = QPushButton(tr("sub.manual_show"))
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

        manual_hint = QLabel(tr("sub.manual_hint"))
        manual_hint.setWordWrap(True)
        manual_hint.setObjectName("dim")
        manual_lay.addWidget(manual_hint)

        self.manual_edit = QPlainTextEdit()
        self.manual_edit.setPlaceholderText(tr("sub.manual_placeholder"))
        self.manual_edit.setMinimumHeight(160)
        manual_lay.addWidget(self.manual_edit)

        manual_btn_row = QHBoxLayout()
        self.manual_parse_btn = QPushButton(tr("sub.manual_parse_btn"))
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
        self.save_btn.setText(tr("sub.add_to_list"))
        self.save_btn.setEnabled(False)
        buttons.button(QDialogButtonBox.Cancel).setText(tr("sub.close"))
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
            QMessageBox.warning(self, "URL", tr("sub.warn_empty_url"))
            return
        # HTTPS-only: a subscription URL is a bearer credential and http://
        # sends it (and the server list it returns) in cleartext. Block it
        # with an actionable message instead of fetching insecurely. The
        # manual-paste path stays available for any edge case.
        if url.lower().startswith("http://"):
            QMessageBox.warning(
                self, tr("sub.warn_insecure_title"),
                tr("sub.warn_insecure_body"),
            )
            return
        if not is_https_url(url):
            QMessageBox.warning(
                self, "URL", tr("sub.warn_bad_url"))
            return
        self.fetch_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.status_label.setText(tr("sub.loading"))
        self._fetcher = _SubscriptionFetcher(url, parent=self)
        self._fetcher.succeeded.connect(self._on_fetched)
        self._fetcher.failed.connect(self._on_fetch_failed)
        self._fetcher.start()

    def _ingest_text(self, text: str, source: Optional[str] = None) -> None:
        """Route imported text: an http(s) URL → the URL fetch path (which
        gates http:// + validates https); a share-URL list or base64 body →
        the manual-parse path. Shared by clipboard + QR import."""
        if source is None:
            source = tr("sub.src_clipboard")
        text = (text or "").strip()
        if not text:
            QMessageBox.information(
                self, tr("sub.empty_title"), tr("sub.src_empty", source=source))
            return
        low = text.lower()
        if low.startswith("https://") or low.startswith("http://"):
            self.url_edit.setText(text)
            self._on_fetch()
            return
        if not self.manual_toggle.isChecked():
            self.manual_toggle.setChecked(True)
        self.manual_edit.setPlainText(text)
        self._on_parse_pasted()

    def _on_paste_clipboard(self) -> None:
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        text = cb.text()
        if text and text.strip():
            self._ingest_text(text, source=tr("sub.src_clipboard"))
            return
        # No text — maybe a copied QR image. Try to decode it.
        img = cb.image()
        if img is not None and not img.isNull():
            decoded = self._decode_qimage(img)
            if decoded:
                self._ingest_text(decoded, source=tr("sub.src_clipboard_qr"))
                return
            QMessageBox.information(
                self, tr("sub.clipboard_title"),
                tr("sub.clipboard_qr_fail"))
            return
        QMessageBox.information(self, tr("sub.clipboard_title"), tr("sub.clipboard_empty"))

    def _on_import_qr(self) -> None:
        from ..core import qr
        if not qr.decoder_available():
            QMessageBox.information(
                self, tr("sub.qr_import_title"),
                tr("sub.qr_no_decoder"))
            return
        from PySide6.QtWidgets import QFileDialog
        path, _sel = QFileDialog.getOpenFileName(
            self, tr("sub.qr_pick_image"), "",
            tr("sub.qr_image_filter"))
        if not path:
            return
        decoded = qr.decode_qr_image(path)
        if not decoded:
            QMessageBox.information(
                self, tr("sub.qr_import_title"), tr("sub.qr_not_recognized"))
            return
        self._ingest_text(decoded, source=tr("sub.src_qr"))

    def _decode_qimage(self, qimage) -> Optional[str]:
        """Save a clipboard QImage to a temp PNG and decode it via core.qr.
        None if no decoder / no QR. Never raises."""
        from ..core import qr
        if not qr.decoder_available():
            return None
        import os
        import tempfile
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="kt-qr-")
            os.close(fd)
            if not qimage.save(tmp, "PNG"):
                return None
            return qr.decode_qr_image(tmp)
        except Exception:
            return None
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

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
            tr("sub.manual_hide") if checked else tr("sub.manual_show")
        )

    def _on_parse_pasted(self) -> None:
        body = self.manual_edit.toPlainText().strip()
        if not body:
            QMessageBox.warning(
                self, tr("sub.empty_title"),
                tr("sub.warn_empty_paste"),
            )
            return
        result = result_from_body(body)
        self._show_result(result, source_label=tr("sub.src_pasted"))

    def _show_result(
        self,
        result: SubscriptionResult,
        source_label: Optional[str] = None,
    ) -> None:
        self._result = result
        if result.configs:
            msg = tr("sub.result_found", n=len(result.configs))
            if source_label:
                msg += tr("sub.result_source", source=source_label)
            elif result.via_proxy:
                msg += tr("sub.result_via_proxy")
            if result.errors:
                msg += tr("sub.result_skipped_lines", n=len(result.errors))
            if result.placeholders:
                msg += tr("sub.result_skipped_stub", n=len(result.placeholders))
            if result.userinfo is not None and result.userinfo.summary():
                msg += tr("sub.result_userinfo", summary=result.userinfo.summary())
            self.status_label.setText(msg)
            self.save_btn.setEnabled(True)
        elif result.placeholders:
            # Parsed fine, but every entry was a provider stub (e.g.
            # gmailvpn's 0.0.0.0:1 "App not supported"). Explain instead
            # of silently importing a dead server.
            self.status_label.setText(
                tr("sub.result_only_stub", n=len(result.placeholders))
            )
        else:
            self.status_label.setText(tr("sub.result_no_share_url"))

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
        import time as _t
        settings["subscription_last_refresh"] = int(_t.time())
        storage.save_settings(settings)
        self.accept()

    # --- result -----------------------------------------------------------

    def imported_configs(self) -> list[ProxyConfig]:
        return list(self._result.configs) if self._result else []
