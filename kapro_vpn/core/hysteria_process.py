"""Run the Hysteria2 client as a managed subprocess exposing a local SOCKS5.

Xray-core can't speak Hysteria2. For hy2 configs we run `hysteria` in
client mode with a local SOCKS5 listener, then point xray's `proxy`
outbound at it (see xray_config.proxy_to_xray_outbound). xray still does
all the split-routing + DNS-leak hardening; hysteria is just the
encrypted transport to the server. Same helper-subprocess pattern as
tun2socks_process.

The client config is written as JSON into a .yaml file — valid JSON is
valid YAML, so hysteria's loader reads it and we avoid a YAML dependency.
"""
from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

from . import paths

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LogSink = Callable[[str], None]

# Local SOCKS5 port the hysteria client listens on; xray chains to it.
# Picked clear of xray's 2080 (http-in) / 2081 (socks-in) and the stats API.
HYSTERIA_SOCKS_PORT = 2089


def build_client_config(outbound: dict[str, Any],
                        socks_port: int = HYSTERIA_SOCKS_PORT) -> dict[str, Any]:
    """Map a parsed hy2 ProxyConfig.outbound to a hysteria client config.

    Pure + JSON-serialisable so it can be unit-tested without the binary.
    outbound keys (from parser.parse_hysteria2): server, server_port,
    password, tls{server_name, insecure}, optional obfs{type, password}.
    """
    server = str(outbound.get("server", "")).strip()
    port = outbound.get("server_port") or 443
    cfg: dict[str, Any] = {
        "server": f"{server}:{int(port)}",
        "auth": str(outbound.get("password", "")),
        "socks5": {"listen": f"127.0.0.1:{int(socks_port)}"},
    }
    tls_in = outbound.get("tls") or {}
    tls: dict[str, Any] = {}
    sni = tls_in.get("server_name")
    if sni:
        tls["sni"] = sni
    if tls_in.get("insecure"):
        tls["insecure"] = True
    if tls:
        cfg["tls"] = tls
    obfs = outbound.get("obfs")
    if obfs and obfs.get("type"):
        otype = str(obfs["type"])
        # Salamander shape: obfs.type + obfs.<type>.password
        cfg["obfs"] = {"type": otype, otype: {"password": str(obfs.get("password", ""))}}
    return cfg


def write_client_config(outbound: dict[str, Any],
                        socks_port: int = HYSTERIA_SOCKS_PORT) -> Path:
    cfg = build_client_config(outbound, socks_port)
    path = paths.hysteria_config_file()
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


class HysteriaProcess:
    """Wraps the hysteria client as a subprocess (mirror of Tun2socksProcess)."""

    def __init__(self, on_log: Optional[LogSink] = None, log_buffer: int = 500):
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._on_log = on_log
        self._recent: "deque[str]" = deque(maxlen=log_buffer)
        self._lock = threading.Lock()

    def start(self, config_path: str) -> None:
        if self.is_running():
            raise RuntimeError("hysteria is already running")
        exe = paths.hysteria_exe()
        if not exe.is_file():
            raise FileNotFoundError(f"hysteria binary not found at {exe}")
        # `client` is hysteria's default mode, so `hysteria -c <file>` runs
        # the client. cwd in the hysteria dir for log/temp consistency.
        self._proc = subprocess.Popen(
            [str(exe), "-c", config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            cwd=str(paths.hysteria_dir()),
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def wait_until_listening(self, socks_port: int = HYSTERIA_SOCKS_PORT,
                             timeout: float = 8.0) -> bool:
        """Block until the SOCKS5 port accepts connections, or timeout.
        Returns False if hysteria died during startup."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_running():
                return False
            try:
                with socket.create_connection(("127.0.0.1", socks_port), timeout=0.3):
                    return True
            except OSError:
                time.sleep(0.15)
        return False

    def stop(self, timeout: float = 3.0) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        self._proc = None

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc else None

    def recent_logs(self) -> list[str]:
        with self._lock:
            return list(self._recent)

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            with self._lock:
                self._recent.append(line)
            if self._on_log:
                try:
                    self._on_log(line)
                except Exception:
                    pass
