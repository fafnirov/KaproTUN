"""Latency of every saved server, measured in the background.

A "ping" here is one TCP connect to the server's address — close enough to
round-trip time to pick a fast server by, without ICMP privileges or a real
proxy handshake. The results feed the Servers tab, the home screen's server
card and the tray's quick-connect list.
"""
from __future__ import annotations

import concurrent.futures
import socket
import time
from typing import Optional

from PySide6.QtCore import QThread, Signal

from ..core.parser import ProxyConfig


class PingerThread(QThread):
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
        # Interruptible (v3.4.1): poll the futures in short slices and check
        # isInterruptionRequested() between them, so requestInterruption()+wait()
        # from the owner returns within ~0.2 s. Without this the thread ran to
        # completion (each TCP ping up to 3 s); if the widget that PARENTS it was
        # destroyed meanwhile — e.g. the user pings then picks a server — the
        # running QThread was deleted and Qt aborted the whole app with
        # "QThread: Destroyed while thread is still running".
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=10)
        try:
            futures = {ex.submit(self._ping_one, c): c for c in self._configs}
            pending = set(futures)
            while pending and not self.isInterruptionRequested():
                done, pending = concurrent.futures.wait(
                    pending, timeout=0.2,
                    return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    if self.isInterruptionRequested():
                        return
                    cfg = futures[fut]
                    try:
                        ms = fut.result()
                    except Exception:
                        ms = None
                    self.pinged.emit(cfg.name, ms)
        finally:
            # Abandon in-flight connects instead of blocking the join on them
            # (cancel_futures skips ones not yet started).
            ex.shutdown(wait=False, cancel_futures=True)

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
