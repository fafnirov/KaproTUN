"""The "Add server" page of the v2 interface: one server by its share link, or
a provider's subscription (every server at once).

It replaces both the old in-window add page and the separate subscription
window. Whatever comes back from outside — a provider's message, a fetch
error, a pasted link — is shown as plain text; a provider's "support" address
opens only if it is an ordinary web link.

The page parses and previews; it saves nothing to the server list. It hands
the result to the main window (config_ready / subscription_imported), which
merges it the one way servers get merged (merge_prompt).
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QVBoxLayout, QWidget

from ..core import app_log, storage
from ..core.i18n import tr
from ..core.parser import ParseError, ProxyConfig, parse
from ..core.subscription import FetchError, SubscriptionResult, is_https_url, result_from_body
from . import kit, tokens
from .home_v2 import server_meta
from .icons_v2 import IconLabel
from .merge_prompt import one_line
from .sub_workers import SubscriptionFetch
from .toast import show_toast

MODE_LINK, MODE_SUB = 0, 1
ST_INPUT, ST_LOADING, ST_RESULT, ST_ERROR, ST_MESSAGE = "input", "loading", "result", "error", "message"


def _is_web_link(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def _without_link_secrets(text: str) -> str:
    """An error line with every web address cut down to its host. A
    subscription link is a credential (the token is in its path or query),
    and exception texts like to quote the address they failed on."""
    return re.sub(r"(https?://)(?:[^/\s@]*@)?([^/\s?#'\"]+)[^\s'\"]*", r"\1\2/…", text)


class PasteDialog(kit.OverlayDialog):
    """Paste the body of a subscription page by hand — for providers whose
    site will not answer the app but opens in a browser."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, wide=True)
        self.scrim_dismisses = False
        self.head("paste", tr("add2.paste_title"), tr("add2.paste_text"), tone="accent")
        self.area = kit.TextArea(height=300)
        self.add_widget(self.area)
        foot = QHBoxLayout()
        foot.setSpacing(tokens.SP_2)
        foot.addWidget(kit.label(tr("add2.paste_hint"), "hint", wrap=True), stretch=1)
        self.chars = kit.label("", "hint")
        foot.addWidget(self.chars, 0, Qt.AlignTop)
        self.body.addLayout(foot)
        self.add_actions([("cancel", tr("add2.cancel"), "secondary"),
                          ("parse", tr("add2.paste_parse"), "primary")],
                         default="parse", icons={"parse": "list"})
        self.area.textChanged.connect(self._on_changed)
        self._on_changed()
        self.area.setFocus()

    def _on_changed(self) -> None:
        n = len(self.area.toPlainText().strip())
        self.chars.setText(tr("add2.paste_chars", n=n))
        self.buttons["parse"].setEnabled(n > 0)

    def text(self) -> str:
        return self.area.toPlainText().strip()


class AddServerPage(QWidget):
    back_clicked = Signal()
    config_ready = Signal(object)            # ProxyConfig, named by the user
    subscription_imported = Signal(object)   # list[ProxyConfig]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("page")
        self._parsed: Optional[ProxyConfig] = None
        self._result: Optional[SubscriptionResult] = None
        self._result_url = ""           # the link the result came from; "" if pasted
        self._fetcher = None            # the fetch whose answer is still wanted
        self._auto_name = ""            # the name the page filled in by itself
        self._state = ST_INPUT
        self._make_fetcher = SubscriptionFetch

        col = QVBoxLayout(self)
        col.setContentsMargins(tokens.PAGE_PAD_X, tokens.PAGE_PAD_Y,
                               tokens.PAGE_PAD_X, tokens.PAGE_PAD_Y)
        col.setSpacing(tokens.SP_4)

        head = QHBoxLayout()
        head.setSpacing(tokens.SP_2)
        self.back_btn = kit.Button("", "ghost", icon="chevron-left", size="sm",
                                   tooltip=tr("add2.back_tip"))
        self.back_btn.clicked.connect(self.back_clicked)
        head.addWidget(self.back_btn)
        head.addWidget(kit.label(tr("add2.title"), "h1"), stretch=1)
        col.addLayout(head)

        self.seg = kit.Segmented([(tr("add2.tab_link"), "link"), (tr("add2.tab_sub"), "download")])
        self.seg.changed.connect(self._on_mode_changed)
        col.addWidget(self.seg)

        self.link_pane = self._build_link_pane()
        self.sub_pane = self._build_sub_pane()
        col.addWidget(self.link_pane, stretch=1)
        col.addWidget(self.sub_pane, stretch=1)
        self.reset()

    # --- building -----------------------------------------------------------

    @staticmethod
    def _field(*widgets) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(tokens.SP_1H)
        for w in widgets:
            if isinstance(w, QWidget):
                box.addWidget(w)
            else:
                box.addLayout(w)
        return box

    def _build_link_pane(self) -> QWidget:
        pane = QWidget()
        col = QVBoxLayout(pane)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(tokens.SP_4)

        self.url_edit = kit.TextArea(tr("add2.url_placeholder"))
        self.url_edit.textChanged.connect(self._on_link_changed)
        status = QHBoxLayout()
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(tokens.SP_1)
        self.link_ok = IconLabel("check", tokens.ICON_XS, "success_text")
        status.addWidget(self.link_ok)
        self.link_status = kit.label("", "hint", wrap=True)
        status.addWidget(self.link_status, stretch=1)
        self.to_sub = kit.LinkButton(tr("add2.open_sub"))
        self.to_sub.clicked.connect(self._on_to_subscription)
        col.addLayout(self._field(kit.label(tr("add2.url_label"), "label"), self.url_edit,
                                  status, self.to_sub, kit.label(tr("add2.supported"), "hint")))

        self.name_edit = kit.Input(tr("add2.name_placeholder"))
        self.name_edit.textChanged.connect(lambda _t: self.name_edit.set_error(False))
        col.addLayout(self._field(kit.label(tr("add2.name_label"), "label"), self.name_edit))

        col.addStretch(1)
        self.save_btn = kit.Button(tr("add2.save"), "primary", icon="power", size="lg")
        self.save_btn.clicked.connect(self._on_save)
        col.addWidget(self.save_btn)
        return pane

    def _build_sub_pane(self) -> QWidget:
        pane = QWidget()
        col = QVBoxLayout(pane)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(tokens.SP_4)
        col.addWidget(kit.label(tr("add2.sub_intro"), "textSm", wrap=True))

        row = QHBoxLayout()
        row.setSpacing(tokens.SP_2)
        self.sub_edit = kit.Input("https://…", icon="link")
        self.sub_edit.textEdited.connect(self._on_sub_edited)
        self.sub_edit.returnPressed.connect(self._on_fetch)
        row.addWidget(self.sub_edit, stretch=1)
        self.paste_btn = kit.Button("", "secondary", icon="paste", tooltip=tr("add2.paste_tip"))
        self.paste_btn.clicked.connect(self._on_paste_clipboard)
        row.addWidget(self.paste_btn)
        self.qr_btn = kit.Button("", "secondary", icon="qr", tooltip=tr("add2.qr_tip"))
        self.qr_btn.clicked.connect(self._on_import_qr)
        row.addWidget(self.qr_btn)
        self.sub_hint = kit.label(tr("add2.https_hint"), "hint", wrap=True)
        col.addLayout(self._field(kit.label(tr("add2.sub_label"), "label"), row, self.sub_hint))

        self.loading = QWidget()
        loading = QVBoxLayout(self.loading)
        loading.setContentsMargins(0, 0, 0, 0)
        loading.setSpacing(tokens.SP_2)
        loading.addWidget(kit.Progress())
        loading.addWidget(kit.label(tr("add2.loading_hint"), "hint", wrap=True))
        col.addWidget(self.loading)

        self.notice = kit.Notice()
        col.addWidget(self.notice)

        self.manual_btn = kit.Button(tr("add2.manual"), "ghost", icon="paste", size="sm")
        self.manual_btn.clicked.connect(self._on_manual)
        col.addWidget(self.manual_btn, 0, Qt.AlignLeft)

        col.addStretch(1)
        bottom = QHBoxLayout()
        bottom.setSpacing(tokens.SP_2)
        self.cancel_btn = kit.Button(tr("add2.cancel"), "secondary", size="lg")
        self.cancel_btn.clicked.connect(self._on_cancel_result)
        bottom.addWidget(self.cancel_btn)
        self.main_btn = kit.Button(tr("add2.load"), "primary", icon="download", size="lg")
        self.main_btn.clicked.connect(self._on_main)
        bottom.addWidget(self.main_btn, stretch=1)
        col.addLayout(bottom)
        return pane

    # --- what the main window drives ---------------------------------------

    def reset(self) -> None:
        """An empty form, as when the page is opened anew."""
        self._abandon_fetch()
        self.url_edit.blockSignals(True)
        self.url_edit.clear()
        self.url_edit.blockSignals(False)
        self.name_edit.clear()
        self.name_edit.set_error(False)
        self._auto_name = ""
        self._parsed = None
        self._set_link_status("", "")
        self.save_btn.setEnabled(False)
        self.sub_edit.clear()
        self._result, self._result_url = None, ""
        self._set_sub_state(ST_INPUT)
        self.set_mode(MODE_LINK)

    def open(self, mode: int = MODE_LINK, url: str = "") -> None:
        """Start fresh in `mode`. A subscription `url` is fetched right away —
        the user already said what they want."""
        self.reset()
        self.set_mode(mode)
        if mode == MODE_SUB:
            if url:
                self.sub_edit.setText(url)
                QTimer.singleShot(0, self._on_fetch)
            self.sub_edit.setFocus()
        else:
            self.url_edit.setFocus()

    def set_mode(self, mode: int) -> None:
        self.seg.set_current(mode, emit=False)
        self._on_mode_changed(mode)

    def mode(self) -> int:
        return self.seg.current_index()

    def sub_state(self) -> str:
        return self._state

    def shutdown(self) -> None:
        """Called at quit. A download in flight is left to end with the
        process (see sub_workers); its answer is just no longer wanted."""
        self._abandon_fetch()

    # --- link mode ----------------------------------------------------------

    def _on_mode_changed(self, mode: int) -> None:
        self.link_pane.setVisible(mode == MODE_LINK)
        self.sub_pane.setVisible(mode == MODE_SUB)

    def _set_link_status(self, text: str, tone: str, offer_sub: bool = False) -> None:
        self.link_status.setText(text)
        self.link_status.setProperty("tone", tone)
        self.link_status.style().unpolish(self.link_status)
        self.link_status.style().polish(self.link_status)
        self.link_status.setVisible(bool(text))
        self.link_ok.setVisible(tone == "success")
        self.to_sub.setVisible(offer_sub)

    def _on_link_changed(self) -> None:
        text = self.url_edit.toPlainText().strip()
        self._parsed = None
        self.save_btn.setEnabled(False)
        if not text:
            self._set_link_status("", "")
            return
        if _is_web_link(text):
            self._set_link_status(tr("add2.looks_like_sub"), "warning", offer_sub=True)
            return
        try:
            cfg = parse(text)
        except ParseError as e:
            self._set_link_status(tr("add2.parse_failed", error=one_line(e, 160)), "error")
            return
        self._parsed = cfg
        # Name the server after the link — also when the field still holds
        # the name of the link pasted before this one. A name the user typed
        # is theirs and stays.
        current = self.name_edit.text().strip()
        if not current or current == self._auto_name:
            self._auto_name = one_line(cfg.name, 60)
            self.name_edit.setText(self._auto_name)
        proto, _sep, where = server_meta(cfg).partition(" · ")
        self._set_link_status(tr("add2.recognized", proto=proto, where=one_line(where, 80)), "success")
        self.save_btn.setEnabled(True)

    def _on_to_subscription(self) -> None:
        url = self.url_edit.toPlainText().strip()
        self.open(MODE_SUB, url)

    def _on_save(self) -> None:
        if self._parsed is None:
            return
        name = one_line(self.name_edit.text(), 60)
        if not name:
            self.name_edit.set_error(True)
            self.name_edit.setFocus()
            show_toast(self.window(), tr("add2.name_required"), kind="error")
            return
        self._parsed.name = name
        self.config_ready.emit(self._parsed)

    # --- subscription mode --------------------------------------------------

    def _set_sub_state(self, state: str, field_error: str = "") -> None:
        self._state = state
        busy = state == ST_LOADING
        self.loading.setVisible(busy)
        self.notice.setVisible(state in (ST_RESULT, ST_ERROR, ST_MESSAGE))
        self.cancel_btn.setVisible(state == ST_RESULT)
        self.manual_btn.setVisible(state != ST_RESULT)
        for w in (self.sub_edit, self.paste_btn, self.qr_btn, self.manual_btn):
            w.setEnabled(not busy)
        self.main_btn.setEnabled(not busy)
        if state == ST_RESULT:
            self.main_btn.setText(tr("add2.add_to_list"))
            self.main_btn.set_icon("plus")
        elif busy:
            self.main_btn.setText(tr("add2.loading_btn"))
            self.main_btn.set_icon("download")
        else:
            self.main_btn.setText(tr("add2.load" if state == ST_INPUT else "add2.load_again"))
            self.main_btn.set_icon("download")
        self.sub_edit.set_error(bool(field_error) or state == ST_ERROR)
        self.sub_hint.setText(field_error or tr("add2.https_hint"))
        self.sub_hint.setProperty("tone", "error" if field_error else "")
        self.sub_hint.style().unpolish(self.sub_hint)
        self.sub_hint.style().polish(self.sub_hint)
        self.sub_hint.setVisible(bool(field_error) or state == ST_INPUT)

    def _on_sub_edited(self, _text: str) -> None:
        # Typing over a shown result means it no longer describes the field.
        if self._state != ST_LOADING:
            self._result, self._result_url = None, ""
            self._set_sub_state(ST_INPUT)

    def _on_main(self) -> None:
        if self._state == ST_RESULT:
            self._on_accept()
        else:
            self._on_fetch()

    def _on_fetch(self) -> None:
        if self._state == ST_LOADING:
            return
        url = self.sub_edit.text().strip()
        # https only: a subscription link is a bearer credential, and over
        # http it and the server list it returns travel in the clear.
        problem = ("add2.err_empty_url" if not url else
                   "add2.err_insecure" if url.lower().startswith("http://") else
                   "add2.err_bad_url" if not is_https_url(url) else "")
        if problem:
            self._result, self._result_url = None, ""
            self._set_sub_state(ST_INPUT, field_error=tr(problem))
            self.sub_edit.setFocus()
            return
        self._result, self._result_url = None, ""
        self._set_sub_state(ST_LOADING)
        fetcher = self._make_fetcher(url, parent=self)
        fetcher.url = url
        # Bound methods, not lambdas: the answer arrives from the worker
        # thread and must be delivered on this widget's own.
        fetcher.succeeded.connect(self._on_fetched)
        fetcher.failed.connect(self._on_fetch_failed)
        if hasattr(fetcher, "crashed"):
            fetcher.crashed.connect(self._on_fetch_crashed)
        self._fetcher = fetcher
        fetcher.start()

    def _abandon_fetch(self) -> None:
        """Stop listening to a fetch in flight; whatever it brings back is for
        a form that has moved on."""
        self._fetcher = None

    def _answer_is_wanted(self) -> bool:
        sender = self.sender()
        return sender is None or sender is self._fetcher

    def _on_fetched(self, result: SubscriptionResult) -> None:
        if not self._answer_is_wanted():
            return
        url = getattr(self._fetcher, "url", "") or self.sub_edit.text().strip()
        self._fetcher = None
        self._show_result(result, url=url)

    def _on_fetch_crashed(self, text: str) -> None:
        """The fetch job itself broke: still an answer — show it as a failure
        rather than loading forever."""
        from ..core.subscription import classify_fetch_error
        self._on_fetch_failed(classify_fetch_error(RuntimeError(text)))

    def _on_fetch_failed(self, info: FetchError) -> None:
        if not self._answer_is_wanted():
            return
        self._fetcher = None
        self._result, self._result_url = None, ""
        self.notice.set_content("danger", one_line(info.title, 120), (one_line(info.detail, 400),))
        if info.raw:
            self.notice.add_line(one_line(_without_link_secrets(info.raw), 200), "bannerRaw")
        self._set_sub_state(ST_ERROR)
        if info.suggest_manual:
            self.manual_btn.setFocus()

    def _show_result(self, result: SubscriptionResult, url: str = "") -> None:
        """`url`: where it was fetched from; empty for pasted text."""
        self._result, self._result_url = result, url
        if result.configs:
            lines = []
            if result.userinfo is not None and result.userinfo.summary():
                lines.append(tr("add2.userinfo", summary=one_line(result.userinfo.summary(), 120)))
            if result.errors:
                lines.append(tr("add2.skipped_lines", n=len(result.errors)))
            if result.placeholders:
                lines.append(tr("add2.skipped_stub", n=len(result.placeholders)))
            if not url:
                lines.append(tr("add2.from_paste"))
            elif result.via_proxy:
                lines.append(tr("add2.via_proxy"))
            self.notice.set_content("success", tr("add2.found", n=len(result.configs)), tuple(lines))
            self._set_sub_state(ST_RESULT)
            self.main_btn.setFocus()
        elif result.placeholders:
            # The panel is refusing us on purpose and only the provider can
            # lift that — so pass on its own words and contacts.
            self.notice.set_content("warning", tr("add2.stub_title"), (tr("add2.stub_text"),))
            if result.provider_note:
                self.notice.add_line(
                    tr("add2.stub_note", note=one_line(result.provider_note, 300)), "bannerQuote")
            self.notice.add_links([(tr("add2.support"), result.support_url),
                                   (tr("add2.account"), result.account_url)])
            self._set_sub_state(ST_MESSAGE)
        else:
            self.notice.set_content("danger", tr("add2.none_title"), (tr("add2.none_text"),))
            self._set_sub_state(ST_ERROR)

    def _on_cancel_result(self) -> None:
        self._result, self._result_url = None, ""
        self._set_sub_state(ST_INPUT)

    def _on_accept(self) -> None:
        result, url = self._result, self._result_url
        if result is None or not result.configs:
            return
        if url:
            try:
                self._remember_subscription(url, result)
            except Exception as e:
                # The servers matter more than the bookmark: an unwritable
                # settings file must not turn "Add to list" into a dead button.
                app_log.log(f"[subscription] link not remembered: {type(e).__name__}")
        configs = list(result.configs)
        self._result, self._result_url = None, ""
        self._set_sub_state(ST_INPUT)
        self.subscription_imported.emit(configs)

    @staticmethod
    def _remember_subscription(url: str, result: SubscriptionResult) -> None:
        """Keep the link (so "refresh subscriptions" can fetch it again) and
        what the provider said about traffic and expiry. Only for a link that
        was actually fetched: pasted text has no address to remember, and
        must not erase the one saved before."""
        saved = storage.load_settings()
        urls = [u for u in (saved.get("subscription_urls") or []) if is_https_url(u)]
        if url not in urls:
            urls.append(url)
        changes = {"subscription_url": url, "subscription_urls": urls,
                   "subscription_last_refresh": int(time.time())}
        if result.userinfo is not None:
            changes["subscription_userinfo"] = result.userinfo.to_dict()
        storage.update_settings(changes)

    # --- clipboard, QR, manual paste ---------------------------------------

    def _ingest_text(self, text: str) -> None:
        """Text from the clipboard or a QR code: a web link is fetched, a
        share link or a base64 body is parsed as it is."""
        text = (text or "").strip()
        if not text:
            show_toast(self.window(), tr("add2.source_empty"), kind="info")
            return
        if _is_web_link(text):
            self.sub_edit.setText(text)
            self._on_fetch()
            return
        self._show_result(result_from_body(text))

    def _on_paste_clipboard(self) -> None:
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if text and text.strip():
            self._ingest_text(text)
            return
        image = clipboard.image()
        if image is not None and not image.isNull():
            decoded = self._decode_qimage(image)
            if decoded:
                self._ingest_text(decoded)
            else:
                show_toast(self.window(), tr("add2.clipboard_qr_fail"), kind="error")
            return
        show_toast(self.window(), tr("add2.clipboard_empty"), kind="info")

    def _on_import_qr(self) -> None:
        from ..core import qr
        if not qr.decoder_available():
            show_toast(self.window(), tr("add2.qr_no_decoder"), kind="info", duration_ms=5000)
            return
        path, _sel = QFileDialog.getOpenFileName(
            self, tr("sub.qr_pick_image"), "", tr("sub.qr_image_filter"))
        if not path:
            return
        decoded = qr.decode_qr_image(path)
        if not decoded:
            show_toast(self.window(), tr("add2.qr_not_recognized"), kind="error")
            return
        self._ingest_text(decoded)

    @staticmethod
    def _decode_qimage(image) -> Optional[str]:
        """A QR code copied as a picture: save it to a temporary PNG and read
        it. None if there is no decoder or no code. Never raises."""
        from ..core import qr
        if not qr.decoder_available():
            return None
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".png", prefix="kt-qr-")
            os.close(fd)
            if not image.save(tmp, "PNG"):
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

    def _on_manual(self) -> None:
        dlg = PasteDialog(self)
        if dlg.ask() == "parse" and dlg.text():
            self._show_result(result_from_body(dlg.text()))
