"""Query Xray-core's runtime traffic stats via its built-in gRPC StatsService.

Xray exposes per-inbound / per-outbound byte counters when the config has
a `stats` block and an `api` inbound. We can read them out via the CLI
helper `xray api stats`, which prints JSON, so we don't have to bring in
a grpc Python dependency.

Subprocess overhead is ~50 ms on Windows — fine for once-per-second
polling, which is what the UI does.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from . import paths

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

API_LISTEN_HOST = "127.0.0.1"
API_LISTEN_PORT = 10085


@dataclass
class TrafficStats:
    """Cumulative byte counters for the proxy outbound at one point in time."""
    uplink_bytes: int = 0
    downlink_bytes: int = 0
    timestamp: float = 0.0

    def delta_rate(self, prev: "TrafficStats") -> tuple[float, float]:
        """(upload_bytes_per_sec, download_bytes_per_sec) since `prev`."""
        dt = self.timestamp - prev.timestamp
        if dt <= 0:
            return 0.0, 0.0
        up = max(0, self.uplink_bytes - prev.uplink_bytes) / dt
        down = max(0, self.downlink_bytes - prev.downlink_bytes) / dt
        return up, down


def query_stats() -> Optional[TrafficStats]:
    """Query Xray's StatsService for `outbound>>>proxy>>>traffic`.

    Returns None if xray isn't running, the API inbound isn't up, or the
    subprocess errors out — caller treats that as "no data yet".
    """
    exe = paths.xray_exe()
    if not exe.is_file():
        return None
    api_addr = f"{API_LISTEN_HOST}:{API_LISTEN_PORT}"
    try:
        result = subprocess.run(
            [str(exe), "api", "stats", f"-server={api_addr}", "-reset=false"],
            capture_output=True, text=True, timeout=2,
            creationflags=CREATE_NO_WINDOW,
            cwd=str(paths.xray_dir()),
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None

    stats = TrafficStats(timestamp=time.time())
    for entry in data.get("stat", []):
        name = entry.get("name", "")
        try:
            value = int(entry.get("value", 0))
        except (TypeError, ValueError):
            continue
        if name == "outbound>>>proxy>>>traffic>>>uplink":
            stats.uplink_bytes = value
        elif name == "outbound>>>proxy>>>traffic>>>downlink":
            stats.downlink_bytes = value
    return stats


def query_tun_iface_stats(iface_name: str) -> Optional[TrafficStats]:
    """Read cumulative byte counters straight off the TUN network interface.

    Why this exists (v1.15.4): the `xray api stats` subprocess turned out
    to be unreliable in the field — on some user setups it returns
    nothing for minutes or never (likely a Windows-side issue with the
    gRPC dokodemo-door inbound + Defender/firewall interactions on
    127.0.0.1:10085). Reading the OS-level interface counters via
    psutil is rock-solid by comparison: the kernel already maintains
    those numbers for `ipconfig` / Task Manager.

    The trade-off: this only works in TUN mode, where ALL VPN traffic
    flows through our named TUN device (KaproTun on Windows, utun? on
    macOS, kaprotun on Linux). In HTTP-proxy mode the traffic is mixed
    with everything else on the physical interface — no clean way to
    isolate it without xray's outbound-tagged counters.

    Caller (controller / main_window) should pick:
      - MODE_TUN  → query_tun_iface_stats(TUN_DEVICE_NAME)
      - MODE_HTTP → query_stats() (xray api stats subprocess)

    Returns None when:
      - psutil isn't available (shouldn't happen — it's in requirements,
        but we tolerate import errors so a missing wheel never breaks
        the connect flow)
      - the named interface isn't present yet (TUN device takes ~100 ms
        to register after tun2socks launch — caller will see None on
        the very first sample, then real data)
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        per_nic = psutil.net_io_counters(pernic=True)
    except OSError:
        return None
    nic = per_nic.get(iface_name)
    if nic is None:
        return None
    # psutil semantics: bytes_sent = bytes leaving us via this iface
    # (uplink), bytes_recv = bytes arriving via this iface (downlink).
    # Same direction convention as our TrafficStats.
    return TrafficStats(
        uplink_bytes=int(nic.bytes_sent),
        downlink_bytes=int(nic.bytes_recv),
        timestamp=time.time(),
    )


def format_rate(bps: float) -> str:
    """Bytes/second → human-readable string."""
    if bps < 1024:
        return f"{bps:.0f} Б/с"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} КБ/с"
    return f"{bps / (1024 * 1024):.1f} МБ/с"


def format_bytes(total: int) -> str:
    """Cumulative bytes → human-readable string."""
    if total < 1024:
        return f"{total} Б"
    if total < 1024 * 1024:
        return f"{total / 1024:.1f} КБ"
    if total < 1024 * 1024 * 1024:
        return f"{total / (1024 * 1024):.1f} МБ"
    return f"{total / (1024 * 1024 * 1024):.2f} ГБ"


def query_clash_totals(host: str, port: int, secret: str,
                       timeout: float = 0.8) -> Optional[TrafficStats]:
    """Session byte totals from sing-box's local Clash API (proxy mode).

    Proxy mode has no TUN interface, so there are no kernel counters to read:
    the traffic shares the physical NIC with everything else. sing-box keeps
    its own running totals and serves them on a loopback, secret-protected
    endpoint; one small GET per second replaces the psutil read.

    Counts everything sing-box relayed, direct and proxied alike — the same
    scope as the TUN counters, which also see both. Returns None on any
    failure (API not up yet, request timed out), exactly like the TUN reader,
    so callers treat the two sources identically.
    """
    try:
        import requests
        session = requests.Session()
        # Never route this through a system/env proxy: in proxy mode the system
        # proxy IS sing-box, and asking it for its own control API through
        # itself would be absurd at best.
        session.trust_env = False
        r = session.get(
            f"http://{host}:{port}/connections",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return TrafficStats(
            uplink_bytes=int(data.get("uploadTotal", 0)),
            downlink_bytes=int(data.get("downloadTotal", 0)),
            timestamp=time.time(),
        )
    except Exception:
        return None
