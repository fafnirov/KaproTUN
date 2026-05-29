"""Modal dialog for picking, adding, and removing proxy configs."""
from __future__ import annotations

import concurrent.futures
import socket
import time
from typing import Optional

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import storage
from ..core.parser import ProxyConfig
from . import flags, styles, world_map
from .config_dialog import AddConfigDialog
from .subscription_dialog import SubscriptionDialog


# Sort modes for the picker. Key = combo label, value = sort-key function
# name handled in _sorted_configs.
_SORT_SPEED, _SORT_NAME, _SORT_COUNTRY, _SORT_PROTO = range(4)
_SORT_LABELS = ["⚡ По скорости", "Имя", "Страна", "Протокол"]


class _PingerThread(QThread):
    """TCP-pings every config server in parallel and emits one result per config.

    A "ping" here is a single TCP connect to (server, port) with a 3-second
    timeout — close enough to RTT to be useful for picking a fast server,
    without needing ICMP privileges or a real proxy handshake.
    """
    pinged = Signal(str, object)  # config name, latency_ms (int) or None

    def __init__(self, configs: list[ProxyConfig], parent=None):
        super().__init__(parent)
        self._configs = configs

    def run(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(self._ping_one, c): c for c in self._configs}
            for fut in concurrent.futures.as_completed(futures):
                cfg = futures[fut]
                try:
                    ms = fut.result()
                except Exception:
                    ms = None
                self.pinged.emit(cfg.name, ms)

    @staticmethod
    def _ping_one(cfg: ProxyConfig) -> Optional[int]:
        server = cfg.outbound.get("server")
        port = cfg.outbound.get("server_port")
        if not server or not port:
            return None
        # UDP-only protocols (Hysteria2): a TCP-connect probe to their
        # endpoint port ALWAYS fails (port is closed for TCP), which
        # would falsely label the config "недоступен" even when the
        # server is fine. Sentinel -1 tells the UI "skip the ping
        # label, just show the protocol".
        if cfg.protocol in ("hysteria2", "hy2"):
            return -1
        try:
            t0 = time.monotonic()
            with socket.create_connection((server, int(port)), timeout=3.0):
                return int((time.monotonic() - t0) * 1000)
        except (socket.gaierror, OSError):
            return None


class _SubsRefreshThread(QThread):
    """Re-fetch every saved subscription URL off the UI thread.

    Each URL goes through the same direct→DPI-fallback path the import
    dialog uses. Per-URL failures are captured (not raised) so one dead
    provider doesn't abort refreshing the others. Emits one aggregated
    payload when all URLs have been tried.
    """
    done = Signal(object)  # dict: configs / userinfo / ok / errors / total

    def __init__(self, urls: list[str], listen_port: int, parent=None):
        super().__init__(parent)
        self._urls = urls
        self._listen_port = listen_port

    def run(self) -> None:
        from ..core.subscription import (
            classify_fetch_error,
            import_with_dpi_fallback,
        )
        configs: list[ProxyConfig] = []
        userinfo = None
        ok = 0
        errors: list[tuple] = []  # (url, FetchError)
        for url in self._urls:
            try:
                res = import_with_dpi_fallback(url, local_proxy_port=self._listen_port)
                configs.extend(res.configs)
                # Keep the most recent informative traffic/expiry summary.
                if res.userinfo is not None and res.userinfo.summary():
                    userinfo = res.userinfo
                ok += 1
            except Exception as e:
                errors.append((url, classify_fetch_error(e)))
        self.done.emit({
            "configs": configs,
            "userinfo": userinfo,
            "ok": ok,
            "errors": errors,
            "total": len(self._urls),
        })


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
        self.resize(440, 540)
        self._configs = list(configs)
        self._current_name = current_name
        self._chosen: Optional[ProxyConfig] = None
        self._pings: dict[str, Optional[int]] = {}  # name -> ms (None = unreachable)
        self._pinger: Optional[_PingerThread] = None
        self._subs_refresher: Optional[_SubsRefreshThread] = None
        self._ping_labels: dict[str, QLabel] = {}   # name -> ping-pill QLabel (in-place updates)
        self._sort_mode = _SORT_SPEED

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        title = QLabel("Конфиги")
        title.setObjectName("h2")
        header_row.addWidget(title)
        # Count label updates as the search filters in — "12 из 47"
        # is way clearer feedback than the list silently shrinking.
        self.count_label = QLabel("")
        self.count_label.setObjectName("dim")
        header_row.addWidget(self.count_label)
        header_row.addStretch(1)
        # Sort selector — speed (default) / name / country / protocol.
        header_row.addWidget(QLabel("Сорт:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(_SORT_LABELS)
        self.sort_combo.setCurrentIndex(self._sort_mode)
        self.sort_combo.setToolTip("Сортировка списка серверов")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        header_row.addWidget(self.sort_combo)
        self.refresh_ping_btn = QPushButton("↻ Пинг")
        self.refresh_ping_btn.setToolTip("Перепроверить задержку до каждого сервера")
        self.refresh_ping_btn.clicked.connect(self._start_pings)
        header_row.addWidget(self.refresh_ping_btn)
        layout.addLayout(header_row)

        # Search box — substring match across name + server + port +
        # protocol. Live-filtered as you type, clearable via the built-in
        # X button on the right. v1.12.0. Critical when a subscription
        # gives you 20-50 servers and finding "the German one" by scroll
        # is annoying.
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "Поиск (имя, IP, протокол, порт)…"
        )
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_input)

        self.list_widget = QListWidget()
        # The global QListWidget::item has padding:12px — fine for plain-text
        # items, but with our setItemWidget rows it inflates the item rect
        # around the widget, so the selection / active highlight renders as a
        # misaligned halo spilling onto neighbouring rows. Zero it for THIS
        # list; the row widget's own contentsMargins give the inner spacing.
        self.list_widget.setStyleSheet("QListWidget::item { padding: 0px; margin: 2px; }")
        self.list_widget.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.list_widget, stretch=1)

        # Empty-state label — shown only when the search query yields
        # zero matches. Without this the list silently goes blank and
        # the user doesn't know if the picker is broken or just filtered.
        self.empty_label = QLabel("Ничего не найдено")
        self.empty_label.setObjectName("dim")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        add_btn = QPushButton("＋ Добавить")
        add_btn.clicked.connect(self._on_add)
        sub_btn = QPushButton("📥 Подписка")
        sub_btn.setToolTip("Импортировать сразу много конфигов из URL подписки")
        sub_btn.clicked.connect(self._on_import_subscription)
        # Re-fetch every saved subscription and pull in new/updated servers.
        self.refresh_subs_btn = QPushButton("🔄 Обновить")
        self.refresh_subs_btn.setToolTip(
            "Заново скачать все сохранённые подписки и добавить новые серверы"
        )
        self.refresh_subs_btn.clicked.connect(self._on_refresh_subscriptions)
        remove_btn = QPushButton("Удалить")
        remove_btn.setObjectName("danger")
        remove_btn.clicked.connect(self._on_remove)

        button_row.addWidget(add_btn)
        button_row.addWidget(sub_btn)
        button_row.addWidget(self.refresh_subs_btn)
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
        self._start_pings()

    # --- helpers ----------------------------------------------------------

    def _on_sort_changed(self, idx: int) -> None:
        self._sort_mode = idx
        self._refresh()

    @staticmethod
    def _name_key(cfg: ProxyConfig) -> str:
        """Sort key for names: strip a leading flag emoji + spaces so
        "🇳🇱 Нидерланды" sorts by 'нидерланды', not by emoji codepoint."""
        n = cfg.name.lstrip()
        i = 0
        while i < len(n) and ("\U0001F1E6" <= n[i] <= "\U0001F1FF" or n[i].isspace()):
            i += 1
        return (n[i:].strip() or n).casefold()

    def _sorted_configs(self) -> list[ProxyConfig]:
        cfgs = list(self._configs)
        mode = self._sort_mode
        if mode == _SORT_NAME:
            cfgs.sort(key=lambda c: self._name_key(c))
        elif mode == _SORT_PROTO:
            cfgs.sort(key=lambda c: (c.protocol.lower(), self._name_key(c)))
        elif mode == _SORT_COUNTRY:
            cfgs.sort(key=lambda c: (self._country_key(c), self._name_key(c)))
        else:  # _SORT_SPEED: reachable (ms asc) → UDP/pending → unreachable
            cfgs.sort(key=lambda c: (self._speed_rank(c), self._name_key(c)))
        return cfgs

    def _speed_rank(self, cfg: ProxyConfig) -> tuple:
        v = self._pings.get(cfg.name, "pending")
        if isinstance(v, int) and v >= 0:
            return (0, v)
        if v == -1 or v == "pending":   # UDP-only / not yet pinged
            return (1, 0)
        return (2, 0)                   # None = unreachable → last

    @staticmethod
    def _country_key(cfg: ProxyConfig) -> tuple:
        try:
            code = flags.country_code(cfg.name) or (world_map.country_code_from_flag(cfg.name) or "")
        except Exception:
            code = flags.country_code(cfg.name) or ""
        return (0, code) if code else (1, "")

    def _refresh(self) -> None:
        self.list_widget.clear()
        self._ping_labels.clear()
        for cfg in self._sorted_configs():
            item = QListWidgetItem()
            item.setData(Qt.UserRole, cfg)
            self.list_widget.addItem(item)
            row = self._make_row(cfg)
            # Use the row's own DPI-aware minimum height (set in _make_row
            # from font metrics) — reliable across display scaling, unlike
            # a fixed px floor.
            item.setSizeHint(QSize(row.sizeHint().width(), row.minimumHeight()))
            self.list_widget.setItemWidget(item, row)
            if cfg.name == self._current_name:
                self.list_widget.setCurrentItem(item)
        if self.list_widget.currentRow() < 0 and self.list_widget.count():
            self.list_widget.setCurrentRow(0)
        # Re-apply the active search filter to the rebuilt list — if
        # the user added/removed/imported configs while a query was in
        # the search box, fresh items would otherwise all show.
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Hide list items whose config doesn't match the search query.

        Match is case-insensitive substring across name, server hostname,
        port number, and protocol — so the user can type "fi" (country
        flag prefix), "144.31" (server IP block), "vless" (protocol),
        or "443" (port) and get the expected hits.
        """
        q = self.search_input.text().strip().lower() if hasattr(self, "search_input") else ""
        visible = 0
        total = self.list_widget.count()
        for i in range(total):
            item = self.list_widget.item(i)
            cfg = item.data(Qt.UserRole)
            if not q or self._matches(cfg, q):
                item.setHidden(False)
                visible += 1
            else:
                item.setHidden(True)
        # Empty-state visibility — only "no match" when the user IS
        # filtering (a literally empty config list isn't a search failure).
        self.empty_label.setVisible(bool(q) and total > 0 and visible == 0)
        # Count label — silent when no filter is active to avoid
        # redundant "47 из 47", informative once the user starts typing.
        if q:
            self.count_label.setText(f"{visible} из {total}")
        else:
            self.count_label.setText("")
        # If the currently-selected row got hidden by the filter, move
        # selection to the first visible item so Enter / "Использовать"
        # still does something sensible.
        cur = self.list_widget.currentItem()
        if cur is not None and cur.isHidden():
            for i in range(total):
                if not self.list_widget.item(i).isHidden():
                    self.list_widget.setCurrentRow(i)
                    break

    def _on_search_changed(self, _text: str) -> None:
        self._apply_filter()

    @staticmethod
    def _matches(cfg: ProxyConfig, q: str) -> bool:
        haystack = " ".join([
            cfg.name.lower(),
            str(cfg.outbound.get("server") or "").lower(),
            str(cfg.outbound.get("server_port") or ""),
            cfg.protocol.lower(),
        ])
        return q in haystack

    def _make_row(self, cfg: ProxyConfig) -> QWidget:
        """A themed two-line row: name (+active marker) over
        protocol-badge · server:port · colour-coded ping pill."""
        p = styles.get_active_palette()
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(6)
        name = QLabel(flags.prefix_with_flag(cfg))  # name already carries the flag emoji
        name.setStyleSheet(f"color:{p.TEXT}; font-weight:600;")
        top.addWidget(name)
        top.addStretch(1)
        if cfg.name == self._current_name:
            active = QLabel("● активен")
            active.setStyleSheet(f"color:{p.ACCENT}; font-size:8pt; font-weight:600;")
            top.addWidget(active)
        v.addLayout(top)

        bot = QHBoxLayout()
        bot.setSpacing(6)
        proto = QLabel(cfg.protocol.upper())
        proto.setStyleSheet(
            f"color:{p.ACCENT_DIM_TEXT}; background:{p.ACCENT_DIM};"
            f"border-radius:4px; padding:1px 6px; font-size:8pt; font-weight:600;")
        bot.addWidget(proto)
        srv = QLabel(f"{cfg.outbound.get('server','?')}:{cfg.outbound.get('server_port','?')}")
        srv.setStyleSheet(f"color:{p.TEXT_MUTED}; font-size:9pt;")
        bot.addWidget(srv)
        bot.addStretch(1)
        pill = QLabel()
        self._ping_labels[cfg.name] = pill
        self._style_pill(pill, self._pings.get(cfg.name, "pending"))
        bot.addWidget(pill)
        v.addLayout(bot)

        # DPI-aware row height. The old fixed 56px floor clipped glyphs at
        # 125%/150% Windows display scaling — font metrics scale with DPI,
        # a hardcoded pixel count doesn't. Derive height from the real
        # line heights + margins + generous slack.
        line1 = name.fontMetrics().height()
        line2 = max(srv.fontMetrics().height(),
                    proto.sizeHint().height(), pill.sizeHint().height())
        w.setMinimumHeight(6 + line1 + 3 + line2 + 6 + 16)  # +16 slack: never clip glyphs
        return w

    def _style_pill(self, pill: QLabel, value) -> None:
        """Colour-code the ping pill: green<100 / amber<250 / red ≥250 or
        unreachable / grey for UDP-only & pending."""
        p = styles.get_active_palette()
        if value == "pending":
            text, color = "…", p.TEXT_MUTED
        elif value == -1:
            text, color = "UDP", p.TEXT_MUTED   # UDP-only (hy2/wg): can't TCP-probe
        elif value is None:
            text, color = "недоступен", p.DANGER
        elif isinstance(value, int):
            text = f"{value} мс"
            color = p.SUCCESS if value < 100 else (p.ACCENT if value < 250 else p.DANGER)
        else:
            text, color = "…", p.TEXT_MUTED
        pill.setText(text)
        pill.setStyleSheet(
            f"color:{color}; font-size:9pt; font-weight:600;"
            f"border:1px solid {color}; border-radius:8px; padding:1px 8px;")

    # --- pinger lifecycle -------------------------------------------------

    def _start_pings(self) -> None:
        if self._pinger is not None and self._pinger.isRunning():
            return  # already running
        # Reset all to "pending" so the UI shows progress
        for cfg in self._configs:
            self._pings[cfg.name] = "pending"
            pill = self._ping_labels.get(cfg.name)
            if pill is not None:
                self._style_pill(pill, "pending")
        self.refresh_ping_btn.setEnabled(False)
        self._pinger = _PingerThread(self._configs, parent=self)
        self._pinger.pinged.connect(self._on_pinged)
        self._pinger.finished.connect(self._on_pings_done)
        self._pinger.start()

    def _on_pinged(self, name: str, ms) -> None:
        self._pings[name] = ms
        pill = self._ping_labels.get(name)
        if pill is not None:
            self._style_pill(pill, ms)

    def _on_pings_done(self) -> None:
        self.refresh_ping_btn.setEnabled(True)
        # Re-order now that latencies are in (only affects the speed sort).
        if self._sort_mode == _SORT_SPEED:
            self._refresh()

    def _selected_cfg(self) -> Optional[ProxyConfig]:
        item = self.list_widget.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    # --- actions ----------------------------------------------------------

    def _on_add(self) -> None:
        dlg = AddConfigDialog(self)
        rc = dlg.exec()
        # Auto-redirect: if the user pasted a subscription URL into the
        # "add single config" form, AddConfigDialog asks them whether to
        # switch and bails with this sentinel. We open the subscription
        # dialog pre-filled so they don't have to paste again.
        if rc == AddConfigDialog.SWITCH_TO_SUBSCRIPTION:
            self._on_import_subscription(prefill_url=dlg.pending_subscription_url())
            return
        if rc != AddConfigDialog.Accepted:
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

    def _on_import_subscription(self, prefill_url: Optional[str] = None) -> None:
        dlg = SubscriptionDialog(self, prefill_url=prefill_url)
        if dlg.exec() != SubscriptionDialog.Accepted:
            return
        imported = dlg.imported_configs()
        if not imported:
            return
        # Merge — name conflicts overwrite existing entries
        existing_by_name = {c.name: i for i, c in enumerate(self._configs)}
        added = replaced = 0
        for cfg in imported:
            if cfg.name in existing_by_name:
                self._configs[existing_by_name[cfg.name]] = cfg
                replaced += 1
            else:
                self._configs.append(cfg)
                existing_by_name[cfg.name] = len(self._configs) - 1
                added += 1
        storage.save_configs(self._configs)
        self._refresh()
        self._start_pings()
        QMessageBox.information(
            self,
            "Импорт завершён",
            f"Добавлено новых: {added}\nЗаменено существующих: {replaced}",
        )

    def _all_subscription_urls(self) -> list[str]:
        """Every subscription URL we've imported from.

        Falls back to the single legacy `subscription_url` for installs
        that predate the list, and de-dupes while preserving order.
        """
        s = storage.load_settings()
        urls = [u for u in (s.get("subscription_urls") or []) if u]
        if not urls:
            one = s.get("subscription_url")
            if one:
                urls = [one]
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _on_refresh_subscriptions(self) -> None:
        """Re-fetch all saved subscriptions and merge in new/updated servers."""
        if self._subs_refresher is not None and self._subs_refresher.isRunning():
            return
        urls = self._all_subscription_urls()
        if not urls:
            QMessageBox.information(
                self,
                "Подписки",
                "Нет сохранённых подписок.\n"
                "Добавь хотя бы одну через «📥 Подписка».",
            )
            return
        self.refresh_subs_btn.setEnabled(False)
        self.refresh_subs_btn.setText("⏳ Обновляю…")
        listen_port = int(storage.load_settings().get("listen_port", 2080))
        self._subs_refresher = _SubsRefreshThread(urls, listen_port, parent=self)
        self._subs_refresher.done.connect(self._on_subs_refreshed)
        self._subs_refresher.start()

    def _on_subs_refreshed(self, agg: dict) -> None:
        self.refresh_subs_btn.setEnabled(True)
        self.refresh_subs_btn.setText("🔄 Обновить")

        # Merge: add new servers by name, refresh existing ones (providers
        # rotate IPs/keys). Never delete — a single failed or partial fetch
        # must not wipe a working server list. Placeholders (0.0.0.0 stubs)
        # are already filtered out upstream in result_from_body.
        existing_by_name = {c.name: i for i, c in enumerate(self._configs)}
        added = updated = 0
        for cfg in agg["configs"]:
            idx = existing_by_name.get(cfg.name)
            if idx is not None:
                self._configs[idx] = cfg
                updated += 1
            else:
                self._configs.append(cfg)
                existing_by_name[cfg.name] = len(self._configs) - 1
                added += 1

        if added or updated:
            storage.save_configs(self._configs)
            # Persist refreshed traffic/expiry info so Settings stays current.
            if agg["userinfo"] is not None:
                s = storage.load_settings()
                s["subscription_userinfo"] = agg["userinfo"].to_dict()
                storage.save_settings(s)
            self._refresh()
            self._start_pings()

        # Report — counts + any per-subscription failures.
        lines = [
            f"Подписок обновлено: {agg['ok']} из {agg['total']}",
            f"Добавлено новых серверов: {added}",
            f"Обновлено существующих: {updated}",
        ]
        errors = agg["errors"]
        if errors:
            lines.append("")
            lines.append(f"Не удалось обновить ({len(errors)}):")
            for url, info in errors[:5]:
                short = url if len(url) <= 48 else url[:45] + "…"
                lines.append(f"• {short} — {info.title}")
            if len(errors) > 5:
                lines.append(f"…и ещё {len(errors) - 5}")
        QMessageBox.information(self, "Обновление подписок", "\n".join(lines))

    def _on_remove(self) -> None:
        cfg = self._selected_cfg()
        if cfg is None:
            return
        confirm = QMessageBox.question(
            self, "Удалить", f"Удалить конфиг «{cfg.name}»?"
        )
        if confirm != QMessageBox.Yes:
            return
        # Remove by identity — list order no longer matches _configs (sort).
        self._configs = [c for c in self._configs if c is not cfg]
        storage.save_configs(self._configs)
        self._refresh()

    def _on_use(self) -> None:
        cfg = self._selected_cfg()
        if cfg is None:
            QMessageBox.information(self, "Конфиг", "Выбери конфиг из списка.")
            return
        self._chosen = cfg
        self.accept()

    def _on_double_click(self, _item) -> None:
        self._on_use()

    # --- result -----------------------------------------------------------

    def selected_config(self) -> Optional[ProxyConfig]:
        return self._chosen
