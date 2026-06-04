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
# sing-box logs a lot at ERROR/WARN that is harmless per-connection churn on a
# perfectly healthy tunnel. We keep EVERYTHING in recent_logs() for diagnostics,
# but only forward user-relevant lines to the UI sink. Three buckets:
#
#   _NEVER_HIDE     fatal / startup / config / driver / permission — always show.
#   _ALWAYS_NOISE   pure per-connection socket churn + the ICMP-not-supported
#                   WARN — never a health signal, always hidden from the UI.
#   _TRANSIENT_NET  ambiguous network conditions (missing default interface, no
#                   route, i/o timeout): a CONNECT failure at startup, but
#                   transient churn once the tunnel is already live. Shown while
#                   starting up (surfaced as a connect diagnostic) and hidden
#                   once the session is confirmed live (see SingBoxProcess._live
#                   / mark_live()).
#
# A line is matched case-insensitively. _NEVER_HIDE wins over the noise buckets,
# so e.g. a fatal that mentions "timeout" still shows.

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
    "bind:",                 # listen/bind failure
    "start dns",             # DNS server start failure
    "dns: start",
    "create service",
)

# Pure per-connection socket churn — never a health signal. Always hidden.
_ALWAYS_NOISE = (
    "an existing connection was forcibly closed",  # Windows WSAECONNRESET
    "forcibly closed by the remote host",
    "connection download closed",
    "connection upload closed",
    "connection reset by peer",
    "read: connection reset",
    "write: connection reset",
    "use of closed network connection",
    "broken pipe",
    "wsarecv:",
    "wsasend:",
    # sing-box WARNs on every ICMP packet (ping) that enters the TUN, because
    # the proxy outbound can't carry ICMP. Pure noise on a healthy tunnel.
    "icmp is not supported by default outbound",
    "link icmp connection",
)

# Ambiguous: a connect failure at startup, transient churn once live.
_TRANSIENT_NET = (
    "missing default interface",
    "no route to internet",
    "no route to host",
    "network is unreachable",
    "host is unreachable",
    "i/o timeout",
    "context canceled",
    "context deadline exceeded",
)


def is_benign_noise(line: str, live: bool = True) -> bool:
    """True if `line` is benign noise that should be kept OUT of the user-facing
    Logs page (it's always retained in recent_logs() for diagnostics).

    `live` — whether the tunnel is already confirmed up (steady state). Default
    True. During the connect/startup window callers pass live=False so that
    ambiguous network errors (missing default interface, no route, i/o timeout)
    are surfaced as a connect diagnostic instead of being swallowed.

    Fatal / startup / config / driver / permission errors are never benign."""
    low = line.lower()
    if any(p in low for p in _NEVER_HIDE):
        return False
    if any(p in low for p in _ALWAYS_NOISE):
        return True
    if any(p in low for p in _TRANSIENT_NET):
        return live      # benign only once the tunnel is already live
    return False


class SingBoxProcess:
    """Wraps a sing-box subprocess (start/stop/is_running/pid/recent_logs)."""

    def __init__(self, on_log: Optional[LogSink] = None, log_buffer: int = 500):
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._on_log = on_log
        self._recent: deque[str] = deque(maxlen=log_buffer)
        self._lock = threading.Lock()
        # False during the connect/startup window, True once the controller
        # confirms the tunnel is live (mark_live()). Controls whether ambiguous
        # network errors are surfaced (startup) or treated as transient noise.
        self._live = False

    def mark_live(self) -> None:
        """Called by the controller once the connect-time liveness check passes.
        Switches the log filter from 'startup' (surface ambiguous network errors
        as a connect diagnostic) to 'live' (treat them as transient noise)."""
        self._live = True

    def _build_args(self, exe, config_path: str) -> list[str]:
        """The sing-box command line. Split out so it's unit-testable without
        spawning a real TUN device (which needs admin)."""
        return [str(exe), "run", "-c", str(config_path)]

    def start(self, config_path: str) -> None:
        if self.is_running():
            raise RuntimeError("sing-box is already running")
        self._live = False  # each session starts in the 'startup' log mode
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
            # recent_logs()), but keep benign churn out of the user-facing sink
            # so a healthy tunnel doesn't spam scary ERRORs. While starting up
            # (not yet live) ambiguous network errors ARE forwarded so a connect
            # failure is visible; once live they're treated as transient noise.
            with self._lock:
                self._recent.append(line)
            if self._on_log and not is_benign_noise(line, live=self._live):
                try:
                    self._on_log(line)
                except Exception:
                    pass
