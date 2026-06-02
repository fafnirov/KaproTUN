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


# --- TCP buffer presets (v2.1.6) -------------------------------------------
#
# gVisor's userspace netstack buffers each TCP flow up to (sndbuf + rcvbuf).
# `-tcp-auto-tuning` means a flow only GROWS toward what its bandwidth-delay
# product needs — but these values are the CEILING. With many concurrent flows
# (a busy browser, a torrent, several apps) the old 4 MiB/4 MiB ceiling let
# tun2socks balloon to multiple GB of private memory. Lowering the default
# ceiling caps that blow-up while auto-tuning keeps everyday throughput high;
# only links that are both very fast AND high-latency lose the top end, and
# those users can opt into the "speed" preset.
#
#   economy  512k/512k — lowest memory; fine on typical home links
#   balanced 1m/1m     — default; good speed, sane memory (was 4m/4m)
#   speed    4m/4m     — max throughput for fast high-RTT servers, high memory
BUFFER_PRESETS: dict[str, tuple[str, str]] = {
    "economy": ("512k", "512k"),
    "balanced": ("1m", "1m"),
    "speed": ("4m", "4m"),
}
DEFAULT_BUFFER_PRESET = "balanced"


def resolve_buffer_preset(name: Optional[str]) -> tuple[str, str]:
    """(sndbuf, rcvbuf) for a preset name. Unknown / empty → the safe default
    (balanced), never the memory-hungry 4m/4m — a typo in settings must not
    silently re-enable the blow-up."""
    return BUFFER_PRESETS.get(str(name or "").lower().strip(),
                              BUFFER_PRESETS[DEFAULT_BUFFER_PRESET])


# --- idle UDP-session timeout per preset (v2.1.7) --------------------------
#
# THE primary lever against the UDP/session runaway the TCP-buffer fix alone
# couldn't stop. tun2socks holds a goroutine + a SOCKS UDP association per
# distinct UDP 5-tuple; apps that spray UDP to many endpoints (QUIC/HTTP3,
# WebRTC, BitTorrent DHT, Telegram) open thousands of them, and at the old 30s
# idle window the abandoned ones piled up into GBs of memory + tens of
# thousands of handles/threads (xray mirrors each as its own flow). A shorter
# idle window reaps abandoned flows far sooner. ACTIVE flows keep receiving
# packets, so they're never idle → never reaped → live calls/streams are
# unaffected.
#   economy  5s   — most aggressive
#   balanced 10s  — default (was 30s)
#   speed    30s  — old behaviour
UDP_TIMEOUTS: dict[str, str] = {
    "economy": "5s",
    "balanced": "10s",
    "speed": "30s",
}
DEFAULT_UDP_TIMEOUT = UDP_TIMEOUTS[DEFAULT_BUFFER_PRESET]


def resolve_udp_timeout(name: Optional[str]) -> str:
    """Idle UDP-session timeout for a preset (unknown/empty → balanced 10s)."""
    return UDP_TIMEOUTS.get(str(name or "").lower().strip(), DEFAULT_UDP_TIMEOUT)


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

    # Throughput tuning (v1.19.2). `-tcp-auto-tuning` lets gVisor's TCP window
    # grow toward the bandwidth-delay product; the buffers below are the CEILING
    # it can grow to. v2.1.6: the default ceiling is the "balanced" preset
    # (1m/1m), NOT the old 4m/4m which let private memory balloon to multiple
    # GB under many concurrent flows. start() overrides these from the user's
    # performance_preset; they remain instance attributes so _build_args stays
    # a pure, unit-testable function.
    TCP_SNDBUF, TCP_RCVBUF = BUFFER_PRESETS[DEFAULT_BUFFER_PRESET]

    # How long an IDLE UDP session is kept in gVisor's netstack. This is the
    # primary lever against UDP-session runaway (see UDP_TIMEOUTS). v2.1.7
    # lowered the default from 30s to 10s (balanced); start() overrides it from
    # the user's preset. Instance attribute so _build_args stays pure/testable.
    UDP_TIMEOUT = DEFAULT_UDP_TIMEOUT

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
              mtu: int = 1500, loglevel: str = "warn",
              buffer_preset: Optional[str] = None) -> None:
        if self.is_running():
            raise RuntimeError("tun2socks is already running")
        # Resolve the per-flow TCP buffer ceiling AND the idle UDP-session
        # timeout from the chosen preset (None → balanced default). Set on the
        # instance so _build_args picks them up.
        self.TCP_SNDBUF, self.TCP_RCVBUF = resolve_buffer_preset(buffer_preset)
        self.UDP_TIMEOUT = resolve_udp_timeout(buffer_preset)
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

    def pid(self) -> Optional[int]:
        """OS pid of the running tun2socks process, or None. Used by the
        runtime memory watchdog to sample its private memory / handle count."""
        proc = self._proc
        return proc.pid if (proc is not None and proc.poll() is None) else None

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
