"""Run tun2socks as a managed subprocess (cross-platform).

tun2socks creates the OS-level TUN device and forwards all IP traffic
from it into xray's SOCKS5 inbound. Combined with default-route changes,
this gives system-wide tunneling for every app (Telegram, Steam, games).

Per-OS device specification:
  Windows  -device wintun://KaproTun    (uses WinTUN driver, dll alongside)
  macOS    -device utun                 (let kernel auto-assign utunN)
  Linux    -device tun://kaprotun       (we choose the name, kernel creates it)

The interface naming is what we look up later via the platform's route
manager to set IP / metric / DNS on it.
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

# Friendly name for our TUN interface. On Windows + Linux we choose it,
# on macOS the kernel picks (we capture the chosen utunN name from tun2socks'
# startup log lines).
TUN_DEVICE_NAME = "KaproTun" if sys.platform == "win32" else "kaprotun"


def _is_noise_line(line: str) -> bool:
    """True for known-benign tun2socks spam not worth surfacing on the
    user's Logs page.

    Today that's UDP relay failures for broadcast / multicast datagrams —
    Steam LAN discovery (udp/27036 → x.x.x.255), SSDP, mDNS — which on
    Windows hit WSAENOBUFS ("...lacked sufficient buffer space ... or
    because a queue was full"). These datagrams can't be proxied through a
    SOCKS tunnel anyway and there's nothing the user can act on. We still
    keep them in the in-memory ring (recent_logs) for deep debugging — just
    don't stream them live to the UI.
    """
    if "[UDP]" not in line:
        return False
    low = line.lower()
    return "buffer space" in low or "queue was full" in low


def _device_arg() -> str:
    """The right -device URI for our OS.

    Windows: `tun://NAME` — tun2socks auto-uses WinTUN when wintun.dll
    is alongside the binary. The seemingly-more-correct `wintun://NAME`
    scheme exists too, but with it tun2socks doesn't always register
    the interface under our chosen NAME — leaving `find_interface_by_name`
    to time out. `tun://` has been the working syntax since v0.x, so
    we stick with it.

    macOS: `utun` — kernel insists on assigning utunN itself; a fixed
    name would error. We discover the actual name later via getifaddrs.

    Linux: `tun://NAME` — kernel creates the device under the name we
    pick.
    """
    if sys.platform == "darwin":
        return "utun"
    return f"tun://{TUN_DEVICE_NAME}"


class Tun2socksProcess:
    """Wraps tun2socks as a subprocess."""

    # Throughput tuning (v1.19.2). gVisor's userspace netstack defaults to a
    # small, fixed TCP receive window; on a link with non-trivial latency
    # that caps throughput well below the line rate (window < bandwidth-delay
    # product). `-tcp-auto-tuning` lets the window grow toward the BDP, and
    # these explicit buffers raise the ceiling for fast / high-RTT servers.
    # 4 MiB covers ~1 Gbps at 30 ms RTT with headroom; auto-tuning only grows
    # to what a flow needs, so idle connections don't pay it.
    TCP_SNDBUF = "4m"
    TCP_RCVBUF = "4m"

    # v2.0.1: how long an IDLE UDP session is kept in gVisor's netstack. A
    # UDP-heavy app (Telegram calls/QUIC, a game, a torrent's DHT) opens many
    # short-lived flows; at the upstream default (~60s) idle sessions pile up,
    # pressure the netstack (the WSAENOBUFS lines we already filter are a
    # symptom) and can wedge the whole tunnel under a "UDP storm". 30s reaps
    # idle flows ~2x sooner. ACTIVE flows keep receiving packets, so they're
    # never idle -> never reaped -> live Telegram calls/QUIC are unaffected.
    UDP_TIMEOUT = "30s"

    def __init__(self, on_log: Optional[LogSink] = None, log_buffer: int = 500):
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._on_log = on_log
        self._recent: deque[str] = deque(maxlen=log_buffer)
        self._lock = threading.Lock()
        # macOS-only: the kernel-assigned utunN name, captured from logs
        # once the process starts. None until tun2socks announces it.
        self._mac_device_name: Optional[str] = None

    def _build_args(self, exe, socks_addr: str, mtu: int, loglevel: str) -> list[str]:
        """The tun2socks command line. Split out so it's unit-testable
        without spawning a real TUN device (which needs admin)."""
        return [
            str(exe),
            "-device", _device_arg(),
            "-proxy", f"socks5://{socks_addr}",
            "-loglevel", loglevel,
            "-mtu", str(mtu),
            # --- throughput tuning (v1.19.2) ---
            "-tcp-auto-tuning",
            "-tcp-sndbuf", self.TCP_SNDBUF,
            "-tcp-rcvbuf", self.TCP_RCVBUF,
            # --- UDP-storm soft guard (v2.0.1) ---
            "-udp-timeout", self.UDP_TIMEOUT,
        ]

    def start(self, socks_addr: str = "127.0.0.1:2081",
              mtu: int = 1500, loglevel: str = "warn") -> None:
        if self.is_running():
            raise RuntimeError("tun2socks is already running")
        exe = paths.tun2socks_exe()
        if not exe.is_file():
            raise FileNotFoundError(f"tun2socks binary not found at {exe}")
        if sys.platform == "win32" and not paths.wintun_dll().is_file():
            raise FileNotFoundError(f"wintun.dll not found at {paths.wintun_dll()}")

        # tun2socks needs to find WinTUN.dll alongside on Windows; we set
        # cwd to its directory. On Unix cwd doesn't matter for binary
        # resolution but we keep it for log/temp file consistency.
        self._proc = subprocess.Popen(
            self._build_args(exe, socks_addr, mtu, loglevel),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            cwd=str(paths.tun_dir()),
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
        self._mac_device_name = None

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc else None

    def recent_logs(self) -> list[str]:
        with self._lock:
            return list(self._recent)

    def mac_device_name(self) -> Optional[str]:
        """macOS-only: actual utunN name the kernel assigned, or None
        until tun2socks has announced it.
        """
        return self._mac_device_name

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            with self._lock:
                self._recent.append(line)
            # Capture the macOS-assigned device name from log lines like
            #   "INFO[0000] [STACK] tun://utun5 <-> socks5://127.0.0.1:2081"
            if sys.platform == "darwin" and self._mac_device_name is None:
                marker = "utun"
                idx = line.find(marker)
                while idx != -1:
                    # extract a contiguous "utun" + digits
                    j = idx + len(marker)
                    while j < len(line) and line[j].isdigit():
                        j += 1
                    name = line[idx:j]
                    if name != "utun":  # must have a number
                        self._mac_device_name = name
                        break
                    idx = line.find(marker, idx + 1)
            if self._on_log and not _is_noise_line(line):
                try:
                    self._on_log(line)
                except Exception:
                    pass
