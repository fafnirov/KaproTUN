"""The leak self-test dialog (Settings → "Check for leaks").

Four probes — IPv4, IPv6, DNS, WebRTC — run together off the UI thread; the
dialog shows a line per probe as soon as the report is in. If a leak comes
from a protection that is switched off, it offers to switch it on; if IPv6
leaks although it should be caught, it offers to copy read-only firewall
diagnostics for a support request.

Everything a probe returns (addresses, country names, resolver host names) is
what remote services said, and is shown as plain text.

The probes cannot be interrupted, so they run as a background job that nobody
waits for (background.py): closing the dialog early is immediate and safe.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget

from ..core import leak_test
from ..core.i18n import tr
from . import kit
from .background import Background
from .merge_prompt import one_line

_WATCHDOG_MS = 25_000

# The probes' blocking calls get a process-wide socket timeout while they
# run. Runs can overlap (the dialog closed and opened again before the first
# finished), so the timeout is set by the first and put back by the last —
# not saved and restored by each, which could leave it set for good.
_timeout_lock = threading.Lock()
_timeout_users = 0
_timeout_before: Optional[float] = None


def _probe_timeout(enter: bool) -> None:
    global _timeout_users, _timeout_before
    import socket
    with _timeout_lock:
        if enter:
            if _timeout_users == 0:
                _timeout_before = socket.getdefaulttimeout()
                socket.setdefaulttimeout(8.0)
            _timeout_users += 1
        else:
            _timeout_users = max(0, _timeout_users - 1)
            if _timeout_users == 0:
                socket.setdefaulttimeout(_timeout_before)


class _LeakTestRun(Background):
    finished = Signal(object)   # leak_test.LeakTestReport

    def __init__(self, socks_proxy: Optional[str]):
        super().__init__()
        self._socks_proxy = socks_proxy

    def _work(self) -> Callable[[], None]:
        _probe_timeout(True)
        try:
            report = leak_test.run_full_leak_test(self._socks_proxy)
        except Exception as e:      # never let the job die silently
            report = leak_test.LeakTestReport()
            report.ipv4 = leak_test.IPv4Result(error=f"{type(e).__name__}: {e}")
        finally:
            _probe_timeout(False)
        return lambda: self.finished.emit(report)


class _ResultRow(kit.ResultLine):
    """A probe's line, with the verdicts named the way the dialog thinks."""

    def __init__(self, name: str, parent: Optional[QWidget] = None):
        super().__init__(name, parent)
        self._detail = self.text

    def set_pass(self, detail: str) -> None:
        self.set_result("ok", detail)

    def set_warn(self, detail: str) -> None:
        self.set_result("warn", detail)

    def set_fail(self, detail: str) -> None:
        self.set_result("fail", detail)

    def set_waiting(self) -> None:
        self.set_result("wait", "…")


class LeakTestDialog(kit.OverlayDialog):
    def __init__(self, socks_proxy: Optional[str], manager=None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent, wide=True)
        self._socks_proxy = socks_proxy
        self._manager = manager
        self._fixable: list = []
        self._action: Optional[str] = None  # "enable" | "diag"
        self._run: Optional[_LeakTestRun] = None
        self._started_at = 0.0

        self.head("droplet", tr("leak.title"), " ", tone="accent")
        self._progress = kit.Progress()
        self.add_widget(self._progress)

        self._row_ipv4 = _ResultRow("IPv4")
        self._row_ipv6 = _ResultRow("IPv6")
        self._row_dns = _ResultRow("DNS")
        self._row_webrtc = _ResultRow("WebRTC")
        self._rows = (self._row_ipv4, self._row_ipv6, self._row_dns, self._row_webrtc)
        from PySide6.QtWidgets import QVBoxLayout
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        for row in self._rows:
            box.addWidget(row)
        self._row_webrtc.setProperty("last", "true")
        self.body.addLayout(box)

        # Which resolvers answered, when a DNS leak is suspected.
        self._dns_detail = kit.Report(height=92)
        self._dns_detail.setVisible(False)
        self.add_widget(self._dns_detail)

        self._fix_caption = self.add_text("")
        self._fix_caption.setVisible(False)
        self._fix_btn = kit.Button(tr("leak.fix_enable_btn"), "secondary", icon="shield-check")
        self._fix_btn.setVisible(False)
        self._fix_btn.clicked.connect(self._on_action_clicked)
        self.add_widget(self._fix_btn)

        self.add_actions(
            [("again", tr("leak.again_btn"), "secondary"), ("close", tr("leak.close_btn"), "primary")],
            default="close", icons={"again": "refresh"},
            handlers={"again": self._start, "close": self.accept})
        self._again_btn = self.buttons["again"]

        # The probes have their own timeouts, but a hung resolver can outlast
        # them: after this long, stop the spinner and say so.
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval(_WATCHDOG_MS)
        self._watchdog.timeout.connect(self._on_watchdog_fire)
        self._start()

    def _connected(self) -> bool:
        """In TUN mode there is no proxy address to pass, so a missing one
        does not mean "no VPN": ask the manager."""
        try:
            if self._manager is not None:
                return bool(self._manager.is_connected())
        except Exception:
            pass
        return self._socks_proxy is not None

    def _start(self) -> None:
        self._again_btn.setEnabled(False)
        self._action, self._fixable = None, []
        for w in (self._dns_detail, self._fix_caption, self._fix_btn):
            w.setVisible(False)
        self._fix_btn.setEnabled(True)
        for row in self._rows:
            row.set_waiting()
        self.set_head_text(tr("leak.caption_running" if self._connected() else "leak.caption_no_vpn"))
        self._progress.setVisible(True)
        self._started_at = time.monotonic()
        self._run = _LeakTestRun(self._socks_proxy)
        self._run.finished.connect(self._on_report)
        self._run.start()
        self._watchdog.start()

    def _on_report(self, report: leak_test.LeakTestReport) -> None:
        if self.sender() is not None and self.sender() is not self._run:
            return      # an earlier run, overtaken by "check again"
        self._run = None
        self._watchdog.stop()
        self._progress.setVisible(False)
        self._again_btn.setEnabled(True)
        secs = max(1, round(time.monotonic() - self._started_at))
        self.set_head_text(tr("leak.done_vpn" if self._connected() else "leak.done_no_vpn", s=secs))

        v4 = report.ipv4
        if v4.ok:
            where = f"{v4.ip} ({v4.country})" if v4.country else f"{v4.ip}"
            self._row_ipv4.set_pass(one_line(where, 120))
        else:
            self._row_ipv4.set_fail(one_line(v4.error, 200) if v4.error else tr("leak.ipv4_no_ip"))

        v6 = report.ipv6
        if v6.ipv6_blocked:
            self._row_ipv6.set_pass(tr("leak.ipv6_blocked"))
        else:
            self._row_ipv6.set_fail(tr("leak.ipv6_leak", ip=one_line(v6.ip, 60)))

        dns = report.dns
        if dns.error:
            self._row_dns.set_fail(tr("leak.dns_error", error=one_line(dns.error, 200)))
        elif not dns.resolvers:
            self._row_dns.set_warn(tr("leak.dns_no_resolvers"))
        elif dns.suspected_leak:
            self._row_dns.set_fail(tr("leak.dns_suspected", n=len(dns.resolvers)))
            self._dns_detail.setPlainText(self._format_dns_resolvers(dns))
            self._dns_detail.setVisible(True)
        else:
            self._row_dns.set_pass(tr("leak.dns_clean", n=len(dns.resolvers)))

        if report.webrtc.stun_blocked:
            self._row_webrtc.set_pass(tr("leak.webrtc_blocked"))
        else:
            self._row_webrtc.set_fail(tr("leak.webrtc_leak"))

        # A leak caused by a protection that is switched off can be fixed
        # from here. If nothing is switched off and IPv6 still leaks, the
        # cause is outside the settings — offer diagnostics instead.
        if self._manager is not None:
            self._fixable = leak_test.fixable_protections(report, self._manager.settings)
            if self._fixable:
                self._action = "enable"
                names = ", ".join(label for _, label in self._fixable)
                self._fix_caption.setText(tr("leak.fix_off_caption", names=names))
                self._fix_btn.setText(tr("leak.fix_enable_named", names=names))
                self._fix_btn.set_icon("shield-check")
            elif not report.ipv6.ipv6_blocked:
                self._action = "diag"
                self._fix_caption.setText(tr("leak.diag_caption"))
                self._fix_btn.setText(tr("leak.diag_copy_btn"))
                self._fix_btn.set_icon("copy")
            if self._action:
                self._fix_caption.setVisible(True)
                self._fix_btn.setVisible(True)

    def _on_action_clicked(self) -> None:
        if self._action == "enable":
            self._enable_protections()
        elif self._action == "diag":
            self._copy_diagnostics()

    def _enable_protections(self) -> None:
        """Switch on what was off (saved through the manager). The firewall
        rules are armed at connect time, so the user is told to reconnect."""
        if self._manager is None or not self._fixable:
            return
        for key, _label in self._fixable:
            self._manager.update_settings(**{key: True})
        names = ", ".join(label for _, label in self._fixable)
        self._fix_btn.setEnabled(False)
        self._fix_btn.setText(tr("leak.fix_enabled_btn", names=names))
        self._fix_caption.setText(tr("leak.fix_enabled_caption", names=names))

    def _copy_diagnostics(self) -> None:
        """Copy firewall diagnostics for a support request. The commands only
        read firewall state; nothing is changed."""
        from ..core import ipv6_block
        try:
            diag = ipv6_block.diagnostics()
        except Exception as e:  # noqa: BLE001 — copying must not take the dialog down
            diag = tr("leak.diag_collect_fail", error=e)
        QApplication.clipboard().setText(diag)
        self._fix_btn.setEnabled(False)
        self._fix_btn.setText(tr("leak.diag_copied_btn"))
        self._fix_caption.setText(tr("leak.diag_copied_caption"))

    def _format_dns_resolvers(self, dns: leak_test.DnsResult) -> str:
        lines = []
        for entry in dns.resolvers_meta:
            ip = entry.get("ip", "?")
            country = entry.get("country_name") or entry.get("country", "")
            tail = " · ".join(p for p in (country, entry.get("asn") or "",
                                          entry.get("hostname") or "") if p)
            lines.append(one_line(f"{ip}    {tail}" if tail else f"{ip}", 160))
        return "\n".join(lines) if lines else tr("leak.dns_no_data")

    def _on_watchdog_fire(self) -> None:
        """The run is still going; it can finish later and will simply redraw."""
        self._progress.setVisible(False)
        self._again_btn.setEnabled(True)
        self.set_head_text("")
        self._row_ipv4.set_fail(tr("leak.timeout_ipv4"))
        self._row_ipv6.set_fail(tr("leak.timeout"))
        self._row_dns.set_fail(tr("leak.timeout_dns"))
        self._row_webrtc.set_fail(tr("leak.timeout"))

    def done(self, result: int) -> None:
        self._watchdog.stop()
        super().done(result)
