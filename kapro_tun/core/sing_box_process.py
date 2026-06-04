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


# --- log classification ----------------------------------------------------
# sing-box logs per-connection network churn at ERROR level even though it's
# harmless: a remote peer reset a socket, a direct/LAN/private address timed
# out, a download/upload stream closed. Surfacing every one of these to the
# user's Logs page is scary noise ("[sing-box] ERROR …" on a perfectly healthy
# tunnel). We keep them in recent_logs() for diagnostics but DON'T forward them
# to the UI sink. Genuinely fatal / startup / config / driver / permission
# errors are NEVER classified as benign — they always reach the user.

# Per-connection socket churn — harmless, hide from the UI.
_BENIGN_NOISE = (
    "an existing connection was forcibly closed",  # Windows WSAECONNRESET
    "forcibly closed by the remote host",
    "i/o timeout",
    "connection download closed",
    "connection upload closed",
    "connection reset by peer",
    "use of closed network connection",
    "broken pipe",
    "context canceled",
    "context deadline exceeded",
    "wsarecv:",
    "wsasend:",
    "no route to host",
    "network is unreachable",
    "host is unreachable",
)

# Markers that ALWAYS keep a line visible, even if it also contains a benign
# substring (e.g. a fatal error that mentions "timeout"). sing-box logs real
# fatals at FATAL level, so the level word itself is a strong signal.
_NEVER_HIDE = (
    " fatal",        # sing-box level word: "… FATAL …"
    "fatal:",
    "panic",
    "failed to start",
    "start command",
    "permission denied",
    "access is denied",
    "operation not permitted",
    "wintun",        # TUN driver problems
    "tun device",
    "create tun",
    "configure tun",
    "decode config",
    "parse config",
    "invalid config",
    "unmarshal",
    "address already in use",
)


def is_benign_noise(line: str) -> bool:
    """True for sing-box per-connection network noise that should be kept out of
    the user-facing Logs page (still retained in recent_logs() for diagnostics).
    Never True for fatal / startup / config / driver / permission errors."""
    low = line.lower()
    if any(p in low for p in _NEVER_HIDE):
        return False
    return any(p in low for p in _BENIGN_NOISE)


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
            # Always retain the full stream for diagnostics (connect-time tail,
            # recent_logs()), but keep benign per-connection churn out of the
            # user-facing sink so a healthy tunnel doesn't spam scary ERRORs.
            with self._lock:
                self._recent.append(line)
            if self._on_log and not is_benign_noise(line):
                try:
                    self._on_log(line)
                except Exception:
                    pass
