"""Run sing-box as a managed subprocess — the v3.0.0 primary TUN dataplane.

Unlike the classic engine (tun2socks.exe forwarding into xray.exe over a local
SOCKS bridge), sing-box is a SINGLE process that owns the TUN device, does its
own routing, and dials the upstream proxy itself. No 127.0.0.1:<socks> bridge,
so no loopback ephemeral-port exhaustion under load.

Command: `sing-box run -c <runtime-config.json>`. On Windows it uses the WinTUN
driver (wintun.dll, shared with the classic engine in tun_dir()), so we run it
with cwd=tun_dir() to keep the DLL on the loader's search path.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from typing import Callable, Optional

from . import paths

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LogSink = Callable[[str], None]


class SingBoxProcess:
    """Wraps a sing-box subprocess (start/stop/is_running/pid/recent_logs)."""

    def __init__(self, on_log: Optional[LogSink] = None, log_buffer: int = 500):
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._on_log = on_log
        self._recent: deque[str] = deque(maxlen=log_buffer)
        self._lock = threading.Lock()

    def _build_args(self, exe, config_path: str) -> list[str]:
        """The sing-box command line. Split out so it's unit-testable without
        spawning a real TUN device (which needs admin)."""
        return [str(exe), "run", "-c", str(config_path)]

    def start(self, config_path: str) -> None:
        if self.is_running():
            raise RuntimeError("sing-box is already running")
        exe = paths.sing_box_exe()
        if not exe.is_file():
            raise FileNotFoundError(f"sing-box binary not found at {exe}")
        if sys.platform == "win32" and not paths.wintun_dll().is_file():
            raise FileNotFoundError(f"wintun.dll not found at {paths.wintun_dll()}")

        # cwd = tun_dir() so the WinTUN DLL (shared with the classic engine) is
        # on the loader search path on Windows.
        cwd = paths.tun_dir() if sys.platform == "win32" else paths.sing_box_dir()
        self._proc = subprocess.Popen(
            self._build_args(exe, config_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            cwd=str(cwd),
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

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

    def pid(self) -> Optional[int]:
        proc = self._proc
        return proc.pid if (proc is not None and proc.poll() is None) else None

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
