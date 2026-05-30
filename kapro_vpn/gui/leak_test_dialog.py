"""Modal dialog: 'Проверка утечек' — runs leak_test.run_full_leak_test
in a worker thread, displays a 4-row pass/fail table.

Triggered from SettingsPage 'Проверить утечки' button. Probes can
take 10-15 s total (network calls + 1 s settle for bash.ws DNS test),
so we MUST run them off the GUI thread — otherwise the window freezes
and Windows shows "Не отвечает" in the titlebar.

UI shape:
    +-------------------------------------------+
    | Проверка утечек                       [x] |
    +-------------------------------------------+
    |                                           |
    |  Проверяем IPv4, IPv6, DNS, WebRTC…       |  ← while running
    |  [spinner]                                |
    |                                           |
    +-------------------------------------------+
                       ↓ done ↓
    +-------------------------------------------+
    | Проверка утечек                       [x] |
    +-------------------------------------------+
    |  ● IPv4   46.17.101.82 (Финляндия)        |
    |  ● IPv6   заблокирован                    |
    |  ⚠ DNS    обнаружен ISP-резолвер!         |
    |       (показать список ↓)                 |
    |  ● WebRTC STUN заблокирован                |
    |                                           |
    |              [Закрыть]                    |
    +-------------------------------------------+

Colour convention:
  ●  green   — pass / clean
  ⚠  amber   — warning / suspected leak
  ✗  red     — fail / definite leak / probe error
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core import leak_test
from . import styles


class _LeakTestWorker(QObject):
    """Runs the probes off the GUI thread.

    v1.16.10: probes now run in parallel inside run_full_leak_test
    (ThreadPoolExecutor with 4 workers). The worker just delegates
    and emits the final report — no per-step progress because all
    probes are in flight simultaneously and "finish in arbitrary
    order".
    """
    finished = Signal(object)  # leak_test.LeakTestReport

    def __init__(self, socks_proxy: Optional[str]):
        super().__init__()
        self._socks_proxy = socks_proxy

    def run(self) -> None:
        # Belt-and-braces global socket timeout. requests honours its
        # own `timeout=`, but underlying socket ops can still block
        # longer at SOCKS handshake / DNS chain edge cases.
        # setdefaulttimeout caps every blocking call thread-wide.
        import socket as _socket
        _orig_timeout = _socket.getdefaulttimeout()
        _socket.setdefaulttimeout(8.0)

        try:
            report = leak_test.run_full_leak_test(self._socks_proxy)
        except Exception as e:  # safety net — never let worker crash silently
            report = leak_test.LeakTestReport()
            report.ipv4 = leak_test.IPv4Result(
                error=f"{type(e).__name__}: {e}"
            )
        finally:
            _socket.setdefaulttimeout(_orig_timeout)
        self.finished.emit(report)


class LeakTestDialog(QDialog):
    """The Settings → 'Проверить утечки' dialog."""

    def __init__(self, socks_proxy: Optional[str], manager=None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Проверка утечек")
        self.setModal(True)
        self.setMinimumWidth(440)
        # Manager lets us flip a protection setting in-place when the test
        # finds a leak that's only leaking because its toggle is off.
        self._manager = manager
        self._fixable: list = []
        self._action: Optional[str] = None  # "enable" | "diag"

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(20, 20, 20, 20)
        self._layout.setSpacing(12)

        # ----- Running state: caption + indeterminate progress -----
        # v1.16.10: caption updates per step ("Проверяем IPv4…" → "IPv6…"
        # → "DNS…" → "WebRTC…") so user can see exactly which probe
        # is currently in flight. If something hangs, the caption tells
        # us which probe is at fault — vital for debugging since the
        # spinner alone is opaque.
        connected = socks_proxy is not None
        if not connected:
            initial_caption = (
                "⚠ VPN не подключён. Проверяю IPv4/IPv6/DNS/WebRTC через "
                "обычный канал — результат покажет «как видно без защиты».\n"
                "Это займёт до 25 секунд."
            )
        else:
            initial_caption = (
                "Проверяем IPv4, IPv6, DNS, WebRTC через активный VPN…\n"
                "Это займёт до 20 секунд."
            )
        self._running_caption = QLabel(initial_caption)
        self._running_caption.setWordWrap(True)
        self._layout.addWidget(self._running_caption)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate "barber pole"
        self._layout.addWidget(self._progress)

        # ----- Result rows (created hidden, revealed when worker done) -----
        self._row_ipv4 = _ResultRow("IPv4")
        self._row_ipv6 = _ResultRow("IPv6")
        self._row_dns = _ResultRow("DNS")
        self._row_webrtc = _ResultRow("WebRTC STUN")
        for row in (self._row_ipv4, self._row_ipv6,
                    self._row_dns, self._row_webrtc):
            row.setVisible(False)
            self._layout.addWidget(row)

        # DNS detail panel — folds out under the DNS row when leak found.
        self._dns_detail = QTextEdit()
        self._dns_detail.setReadOnly(True)
        self._dns_detail.setFixedHeight(110)
        self._dns_detail.setVisible(False)
        self._layout.addWidget(self._dns_detail)

        # ----- One-click fix (v1.19.3) — shown only when a detected leak is
        # leaking *because its protection toggle is off*, so enabling it
        # actually fixes it. Turns the leak test from "reports a problem"
        # into "fixes the problem".
        self._fix_caption = QLabel("")
        self._fix_caption.setWordWrap(True)
        self._fix_caption.setVisible(False)
        self._layout.addWidget(self._fix_caption)
        self._fix_btn = QPushButton("🛡 Включить защиту")
        self._fix_btn.setObjectName("primary")
        self._fix_btn.setVisible(False)
        self._fix_btn.clicked.connect(self._on_action_clicked)
        self._layout.addWidget(self._fix_btn)

        # ----- Close button at the bottom -----
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._close_btn = QPushButton("Закрыть")
        self._close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._close_btn)
        self._layout.addLayout(btn_row)

        # ----- Kick off the worker -----
        self._thread = QThread(self)
        self._worker = _LeakTestWorker(socks_proxy)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_report)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

        # ----- Watchdog (v1.16.9) ----------------------------------------
        # Probes have wallclock budgets in code: IPv4 ≤12 s (2 endpoints
        # × 6 s), IPv6 ≤4 s, DNS ≤10×2 s lookups + 2 s settle + 8 s for
        # bash.ws GET, WebRTC ≤2 s. Worst-case ≈48 s, typical ≈12 s.
        # User v1.16.8 report: dialog stuck at "Проверяем…" for 30+ s
        # — root cause was bare gethostbyname hanging without timeout
        # (now fixed in leak_test._resolve_with_hard_timeout). Belt-and-
        # braces: this watchdog fires after 35 s and forcibly shows a
        # timeout error so the dialog never wedges forever even if some
        # future probe regression introduces another sleep-style hang.
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        # v1.16.10: 25 s budget. Per-probe caps: IPv4 ≤8 s × 2 endpoints,
        # IPv6 ≤4 s, DNS ≤10×2 s lookups + 2 s settle + 8 s bash.ws =
        # ≤30 s worst case if everything's at the limit. We use 25 s
        # as the dialog watchdog because typical run is 8-12 s and a
        # genuine hang past 25 s means probes are wedged.
        self._watchdog.setInterval(25_000)
        self._watchdog.timeout.connect(self._on_watchdog_fire)
        self._watchdog.start()

    # ----- result handling -------------------------------------------------

    def _on_report(self, report: leak_test.LeakTestReport) -> None:
        # Worker finished cleanly — disarm the watchdog so it doesn't
        # try to overwrite the results 35 s later.
        if self._watchdog.isActive():
            self._watchdog.stop()
        # Swap the "Проверяем…" caption + progress for the results.
        self._running_caption.setVisible(False)
        self._progress.setVisible(False)

        # --- IPv4 ---
        v4 = report.ipv4
        if v4.ok:
            country_text = f" ({v4.country})" if v4.country else ""
            self._row_ipv4.set_pass(f"{v4.ip}{country_text}")
        else:
            self._row_ipv4.set_fail(v4.error or "не удалось получить IP")

        # --- IPv6 ---
        v6 = report.ipv6
        if v6.ipv6_blocked:
            self._row_ipv6.set_pass("заблокирован (нет утечки)")
        else:
            # We got an IPv6 — that means traffic went out the real ISP.
            self._row_ipv6.set_fail(
                f"утечка: {v6.ip} прошёл мимо туннеля"
            )

        # --- DNS ---
        dns = report.dns
        if dns.error:
            self._row_dns.set_fail(f"ошибка теста: {dns.error}")
        elif not dns.resolvers:
            self._row_dns.set_warn(
                "bash.ws не зарегистрировал ни одного резолвера "
                "(возможно, заблокирован VPN-провайдером)"
            )
        elif dns.suspected_leak:
            self._row_dns.set_fail(
                f"подозрение на утечку — {len(dns.resolvers)} резолверов, "
                f"один из них похож на ISP"
            )
            self._dns_detail.setText(self._format_dns_resolvers(dns))
            self._dns_detail.setVisible(True)
        else:
            self._row_dns.set_pass(
                f"{len(dns.resolvers)} резолверов, все похожи на VPN/CDN"
            )

        # --- WebRTC ---
        wr = report.webrtc
        if wr.stun_blocked:
            note = "stun.l.google.com:19302 заблокирован firewall'ом"
            self._row_webrtc.set_pass(note)
        else:
            self._row_webrtc.set_fail(
                "STUN ответил — браузер может узнать реальный IP "
                "(включи 'Защита от WebRTC leak' в настройках)"
            )

        # Reveal the rows.
        for row in (self._row_ipv4, self._row_ipv6,
                    self._row_dns, self._row_webrtc):
            row.setVisible(True)

        # One-click fix: if a leak is leaking only because its protection
        # toggle is off, offer to flip it right here.
        if self._manager is not None:
            self._fixable = leak_test.fixable_protections(
                report, self._manager.settings)
            if self._fixable:
                self._action = "enable"
                names = ", ".join(label for _, label in self._fixable)
                self._fix_caption.setText(
                    f"Защита для {names} выключена в настройках — поэтому "
                    f"утечка. Можно включить прямо сейчас:"
                )
                self._fix_btn.setText(f"🛡 Включить защиту ({names})")
                self._fix_caption.setVisible(True)
                self._fix_btn.setVisible(True)
            elif (not report.ipv6.ipv6_blocked
                  and self._manager.settings.get("ipv6_leak_protection", True)):
                # Leaking even though the toggle is ON — the firewall rule
                # didn't take effect on this system (the rare "protection on
                # but still leaks" case). Offer a diagnostics bundle for
                # support instead of a toggle (it's already on).
                self._action = "diag"
                self._fix_caption.setText(
                    "Защита IPv6 включена, но правило firewall не сработало в "
                    "твоей системе (редкий случай — сторонний firewall или "
                    "отключённая фильтрация IPv6). Собери диагностику для "
                    "поддержки:"
                )
                self._fix_btn.setText("📋 Скопировать диагностику")
                self._fix_caption.setVisible(True)
                self._fix_btn.setVisible(True)

        # Resize the dialog to fit the new content.
        self.adjustSize()

    def _on_action_clicked(self) -> None:
        if self._action == "enable":
            self._enable_protections()
        elif self._action == "diag":
            self._copy_diagnostics()

    def _enable_protections(self) -> None:
        """Enable the off-but-needed protection toggles via the manager
        (updates in-memory settings AND persists), then tell the user to
        reconnect so the firewall rules actually get armed."""
        if self._manager is None or not self._fixable:
            return
        for key, _label in self._fixable:
            self._manager.update_settings(**{key: True})
        names = ", ".join(label for _, label in self._fixable)
        self._fix_btn.setEnabled(False)
        self._fix_btn.setText(f"✓ Защита {names} включена")
        self._fix_caption.setText(
            f"Защита {names} включена. Переподключись (выключи и снова включи "
            f"VPN), чтобы применить, затем запусти проверку заново."
        )
        self.adjustSize()

    def _copy_diagnostics(self) -> None:
        """Copy a read-only firewall diagnostics bundle to the clipboard so
        the user can paste it into a support request. The commands only
        READ firewall state — nothing is modified."""
        from PySide6.QtWidgets import QApplication

        from ..core import ipv6_block
        try:
            diag = ipv6_block.diagnostics()
        except Exception as e:  # noqa: BLE001 — never let copy crash the dialog
            diag = f"(не удалось собрать диагностику: {e})"
        QApplication.clipboard().setText(diag)
        self._fix_btn.setEnabled(False)
        self._fix_btn.setText("✓ Скопировано в буфер обмена")
        self._fix_caption.setText(
            "Диагностика скопирована — вставь её в письмо в поддержку. "
            "(Команды только читают состояние firewall, ничего не меняют.)"
        )
        self.adjustSize()

    def _format_dns_resolvers(self, dns: leak_test.DnsResult) -> str:
        """Pretty list of DNS resolvers + their geo for the detail panel."""
        lines = []
        for entry in dns.resolvers_meta:
            ip = entry.get("ip", "?")
            country = entry.get("country_name") or entry.get("country", "")
            asn = entry.get("asn") or ""
            hostname = entry.get("hostname") or ""
            tail = " · ".join(p for p in (country, asn, hostname) if p)
            lines.append(f"• {ip}    {tail}" if tail else f"• {ip}")
        return "\n".join(lines) if lines else "(нет данных)"

    def _on_watchdog_fire(self) -> None:
        """Probes exceeded the 35 s overall budget — show timeout error.

        Worker thread might still be churning (DNS resolver hang etc.).
        We can't safely kill a QThread, but we CAN stop showing the
        spinner and present a useful error. Worker becomes a daemon —
        if it later emits finished, _on_report will harmlessly redraw.
        """
        self._running_caption.setVisible(False)
        self._progress.setVisible(False)
        # Show all rows as ✗ with a generic timeout message — the user
        # gets something actionable instead of an indefinite spinner.
        self._row_ipv4.set_fail("таймаут — VPN отключён или сеть тормозит?")
        self._row_ipv6.set_fail("таймаут")
        self._row_dns.set_fail(
            "таймаут — DNS не отвечает за 35 с. Проверь VPN/настройки."
        )
        self._row_webrtc.set_fail("таймаут")
        for row in (self._row_ipv4, self._row_ipv6,
                    self._row_dns, self._row_webrtc):
            row.setVisible(True)
        self.adjustSize()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Disarm watchdog so it doesn't fire on a half-dead dialog.
        if self._watchdog.isActive():
            self._watchdog.stop()
        # Make sure the worker thread tears down cleanly before the dialog
        # is destroyed — otherwise Qt warns "QThread destroyed while still
        # running" on exit.
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)


# ===== Helper widget: one result row ====================================

class _ResultRow(QWidget):
    """A single ●/⚠/✗ label + caption + detail row."""

    def __init__(self, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._badge = QLabel("●")
        self._badge.setFixedWidth(20)
        self._badge.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._badge)

        self._name = QLabel(name)
        self._name.setFixedWidth(96)
        self._name.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._name)

        self._detail = QLabel("…")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail, stretch=1)

    def set_pass(self, detail: str) -> None:
        self._badge.setText("●")
        self._badge.setStyleSheet(f"color: {styles.ACCENT}; font-size: 14pt;")
        self._detail.setText(detail)
        self._detail.setStyleSheet("")

    def set_warn(self, detail: str) -> None:
        self._badge.setText("⚠")
        # Use ACCENT (amber) — matches the brand and reads as "caution"
        self._badge.setStyleSheet(f"color: {styles.ACCENT}; font-size: 12pt;")
        self._detail.setText(detail)
        self._detail.setStyleSheet("")

    def set_fail(self, detail: str) -> None:
        self._badge.setText("✗")
        self._badge.setStyleSheet(f"color: {styles.DANGER}; font-size: 12pt; font-weight: bold;")
        self._detail.setText(detail)
        self._detail.setStyleSheet(f"color: {styles.DANGER};")
