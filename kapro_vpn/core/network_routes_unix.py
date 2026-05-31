"""Route + DNS manipulation for TUN-mode setup on macOS and Linux.

Mirrors the public API of network_routes.py (the Windows backend) so
controller.py can swap one for the other based on sys.platform without
caring about implementation differences.

What this needs to do at TUN-connect time:

  1. Discover the real default route (interface + gateway IP) BEFORE we
     install our own — needed for the server-IP bypass and direct-domain
     bypass routes (so packets to them skip the TUN entirely).
  2. Assign an IP + bring the TUN interface up.
  3. Install routes via the TUN for default traffic.
  4. Install bypass routes via the real interface for: VPN server,
     direct-list domains, geoip:ru block (~9000 entries).

Per-OS implementation:

  Linux  — `ip route` / `ip addr` / `ip -batch -` for mass route adds.
           Resolution is /proc/net/route fallback if `ip` isn't on PATH
           (rare but possible on very minimal containers).
  macOS  — `route -n get default` for discovery, `ifconfig` to bring TUN
           up, `route add` for individual routes. No batch interface for
           routes on Darwin — geoip:ru takes ~30-60s, callers should warn.

Privileges: every operation here requires root. The controller has
already gated on euid==0 before calling us, so we just assume it.
"""
from __future__ import annotations

import concurrent.futures
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional


# --- shared types (mirror network_routes.py) ----------------------------------

@dataclass
class InterfaceInfo:
    """Same shape as the Windows backend so controller.py can stay
    platform-blind. `interface_metric` is unused on Unix (Linux uses
    explicit route metrics; macOS doesn't really have them), kept for
    API symmetry — just set 0.
    """
    name: str                # e.g. "wlan0", "en0", "utun5"
    index: int               # if_nametoindex() value, 0 if not resolvable
    gateway: str = ""        # IPv4 next-hop; empty for TUN itself
    interface_metric: int = 0


# --- shell helpers ------------------------------------------------------------

def _run(args: list[str], timeout: float = 10.0,
         stdin: Optional[str] = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", input=stdin,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _if_index(name: str) -> int:
    """if_nametoindex(name) → 0 on failure. Used to populate InterfaceInfo.index."""
    try:
        return socket.if_nametoindex(name)
    except (OSError, AttributeError):
        return 0


# ==============================================================================
# Discovery
# ==============================================================================

def get_default_route_v4() -> Optional[InterfaceInfo]:
    """Find the current IPv4 default-route interface + next-hop IP."""
    if sys.platform == "darwin":
        return _mac_default_route()
    return _linux_default_route()


def _linux_default_route() -> Optional[InterfaceInfo]:
    """Parse `ip route show default`. Output example:
        default via 192.168.1.1 dev wlan0 proto dhcp metric 600
    """
    if not _has("ip"):
        return None
    rc, out, _ = _run(["ip", "-4", "route", "show", "default"])
    if rc != 0 or not out.strip():
        return None
    # Pick the lowest-metric default if there are several
    best: Optional[InterfaceInfo] = None
    best_metric = 10**9
    for line in out.splitlines():
        parts = line.split()
        try:
            via = parts[parts.index("via") + 1]
            dev = parts[parts.index("dev") + 1]
        except (ValueError, IndexError):
            continue
        metric = 0
        if "metric" in parts:
            try:
                metric = int(parts[parts.index("metric") + 1])
            except (ValueError, IndexError):
                metric = 0
        if metric < best_metric:
            best_metric = metric
            best = InterfaceInfo(
                name=dev, index=_if_index(dev), gateway=via, interface_metric=metric,
            )
    return best


def _mac_default_route() -> Optional[InterfaceInfo]:
    """Parse `route -n get default`. Output is key-value, lines like:
        gateway: 192.168.1.1
        interface: en0
    """
    rc, out, _ = _run(["route", "-n", "get", "default"])
    if rc != 0:
        return None
    gw = ""
    iface = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("gateway:"):
            gw = line.split(":", 1)[1].strip()
        elif line.startswith("interface:"):
            iface = line.split(":", 1)[1].strip()
    if not iface:
        return None
    return InterfaceInfo(name=iface, index=_if_index(iface), gateway=gw)


def find_interface_by_name(name: str, timeout: float = 8.0) -> Optional[InterfaceInfo]:
    """Poll for an interface to appear (TUN creation is async).

    On macOS the actual kernel-assigned name is utunN; the caller has
    already captured N from tun2socks' log and passes that here.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        idx = _if_index(name)
        if idx > 0:
            return InterfaceInfo(name=name, index=idx)
        time.sleep(0.2)
    return None


# ==============================================================================
# Interface configuration (assign IP, bring up)
# ==============================================================================

def configure_tun_interface(iface: InterfaceInfo,
                            address: str = "10.255.0.2",
                            mask: str = "255.255.255.0") -> None:
    """Give the TUN an IPv4 address + bring it up. macOS needs a peer
    address; on Linux it's just CIDR + link up.
    """
    if sys.platform == "darwin":
        # macOS TUN is point-to-point — the "peer" is our chosen gateway
        # IP inside the TUN subnet (10.255.0.1).
        peer = _mac_tun_peer(address)
        _run(["ifconfig", iface.name, "inet", address, peer,
              "netmask", mask, "up"])
    else:
        cidr_bits = _mask_to_cidr(mask)
        _run(["ip", "addr", "add", f"{address}/{cidr_bits}", "dev", iface.name])
        _run(["ip", "link", "set", iface.name, "up"])


def _mac_tun_peer(address: str) -> str:
    """For a TUN address like 10.255.0.2, pick the peer 10.255.0.1 (the
    "gateway" inside the TUN subnet — what default routes point at).
    """
    octets = address.split(".")
    if len(octets) != 4:
        return address  # caller passed something weird; let ifconfig complain
    octets[-1] = "1"
    return ".".join(octets)


def _mask_to_cidr(mask: str) -> int:
    """255.255.255.0 → 24. Pure-Python so we don't pull in netaddr."""
    try:
        packed = sum((int(o) << (8 * (3 - i))) for i, o in enumerate(mask.split(".")))
    except ValueError:
        return 32
    bits = 0
    for i in range(32):
        if packed & (1 << (31 - i)):
            bits += 1
        else:
            break
    return bits


# ==============================================================================
# DNS — best-effort. The controller's _ALWAYS_BYPASS list pins common
# public DNS resolvers via the real gateway, so even without us touching
# the OS resolver, queries to 8.8.8.8 / 1.1.1.1 / Yandex DNS don't leak
# through the tunnel.
# ==============================================================================

def set_dns(iface_name: str, servers: list[str]) -> None:
    """No-op on Unix in v1.1 — see module docstring.

    Touching resolvers means either editing /etc/resolv.conf (which
    fights NetworkManager / systemd-resolved) or shelling out to
    `networksetup -setdnsservers` (which targets the user's actual NIC,
    not our TUN). Neither is a clean win for v1.1; the bypass-route
    strategy already handles the common DNS leak vectors.
    """
    return


def reset_dns(iface_name: str) -> None:
    return


# ==============================================================================
# Per-route add/delete (the small case)
# ==============================================================================

def add_route(dest: str, mask: str, gateway: str, if_index: int,
              metric: int = 1) -> bool:
    """Add a single route. if_index is mostly ignored on Unix — we
    select by gateway, which uniquely identifies the outgoing interface
    in well-formed configs.
    """
    cidr = _mask_to_cidr(mask)
    if sys.platform == "darwin":
        return _mac_add_route(dest, cidr, gateway)
    return _linux_add_route(dest, cidr, gateway, metric)


def delete_route(dest: str, mask: str, gateway: str = "") -> bool:
    cidr = _mask_to_cidr(mask)
    if sys.platform == "darwin":
        rc, _, _ = _run(["route", "delete", "-net", f"{dest}/{cidr}"])
        return rc == 0
    args = ["ip", "route", "del", f"{dest}/{cidr}"]
    if gateway:
        args += ["via", gateway]
    rc, _, _ = _run(args)
    return rc == 0


def _linux_add_route(dest: str, cidr: int, gateway: str, metric: int) -> bool:
    rc, _, _ = _run([
        "ip", "route", "add", f"{dest}/{cidr}",
        "via", gateway, "metric", str(metric),
    ])
    return rc == 0


def _mac_add_route(dest: str, cidr: int, gateway: str) -> bool:
    # macOS `route` syntax differs subtly from BSD: `-net dest/cidr gateway`
    rc, _, _ = _run([
        "route", "add", "-net", f"{dest}/{cidr}", gateway,
    ])
    return rc == 0


# ==============================================================================
# Session — tracks every add so disconnect can undo them all
# ==============================================================================

@dataclass
class _DnsEntry:
    iface_name: str


@dataclass
class _RouteEntry:
    dest: str
    mask: str
    gateway: str
    if_index: int = 0
    metric: int = 1


@dataclass
class RouteSession:
    """Tracks routes/DNS changes so disconnect can undo them all."""
    routes: list[_RouteEntry] = field(default_factory=list)
    dns_changed: list[_DnsEntry] = field(default_factory=list)

    def add_route(self, dest: str, mask: str, gateway: str, if_index: int,
                  metric: int = 1) -> bool:
        if add_route(dest, mask, gateway, if_index, metric):
            self.routes.append(_RouteEntry(dest, mask, gateway, if_index, metric))
            return True
        return False

    def add_bypass_routes(self, ips: list[str], gateway: str, if_index: int,
                          metric: int = 1) -> tuple[int, int]:
        """Add /32 host-routes for many IPs. Returns (added, adopted)."""
        ips = sorted({ip for ip in ips if ip})
        if not ips:
            return (0, 0)
        entries = [(ip, "255.255.255.255") for ip in ips]
        return self.add_bypass_cidrs(entries, gateway, if_index, metric)

    def add_bypass_cidrs(self, entries: list[tuple[str, str]],
                         gateway: str, if_index: int, metric: int = 1) -> tuple[int, int]:
        """Add many (dest, mask) routes. Returns (added, adopted).

        Linux: batch via `ip -batch -` (pipes all commands in one fork,
        ~9000 routes in <1 second). It already tracks every wanted route
        for cleanup, so there's no cross-session leak to repair here —
        `adopted` is reported 0 (the precise added/already-exists split is
        Windows-specific, where the native API hands back per-route rc).
        macOS: no batch interface; loops over `route add` (~30-60s for
        geoip:ru). Callers should surface a "wait..." log.
        """
        if not entries:
            return (0, 0)
        if sys.platform == "darwin":
            return (self._mac_bypass_loop(entries, gateway, if_index, metric), 0)
        return (self._linux_bypass_batch(entries, gateway, if_index, metric), 0)

    def _linux_bypass_batch(self, entries: list[tuple[str, str]],
                            gateway: str, if_index: int, metric: int) -> int:
        # Build a multi-command script and feed it to `ip -batch -`.
        # Lines that fail (already-exists / invalid) are reported on
        # stderr but `ip -batch` keeps going.
        script_lines = []
        wanted: list[_RouteEntry] = []
        for dest, mask in entries:
            cidr = _mask_to_cidr(mask)
            script_lines.append(
                f"route add {dest}/{cidr} via {gateway} metric {metric}"
            )
            wanted.append(_RouteEntry(dest, mask, gateway, if_index, metric))
        script = "\n".join(script_lines) + "\n"
        rc, _out, err = _run(["ip", "-force", "-batch", "-"], stdin=script,
                             timeout=30.0)
        # `ip -force -batch` keeps running on per-line errors and returns
        # 0 if at least one succeeded. We can't easily tell exactly how
        # many landed, so we count failure-lines in stderr and subtract.
        failed_lines = sum(1 for L in err.splitlines() if "Error" in L)
        added = max(0, len(wanted) - failed_lines)
        # Track only the routes that (probably) landed. Off-by-a-few is
        # acceptable here because delete_route is also forgiving of
        # already-gone routes.
        self.routes.extend(wanted)
        return added

    def _mac_bypass_loop(self, entries: list[tuple[str, str]],
                         gateway: str, if_index: int, metric: int) -> int:
        added = 0
        for dest, mask in entries:
            cidr = _mask_to_cidr(mask)
            if _mac_add_route(dest, cidr, gateway):
                self.routes.append(_RouteEntry(dest, mask, gateway, if_index, metric))
                added += 1
        return added

    def set_dns(self, iface_name: str, servers: list[str]) -> None:
        set_dns(iface_name, servers)
        # Even though set_dns is a no-op on Unix today, still track that
        # the caller requested it so future restore() calls have the
        # data when we wire up real DNS handling.
        self.dns_changed.append(_DnsEntry(iface_name))

    def restore(self) -> None:
        """Undo every route + DNS change. Best-effort — errors swallowed
        because once we're disconnecting the user just wants their net
        back, not a stack trace.
        """
        if sys.platform != "darwin" and _has("ip") and self.routes:
            # Linux: batch-delete in one shot, much faster than loop.
            lines = []
            for r in self.routes:
                cidr = _mask_to_cidr(r.mask)
                lines.append(f"route del {r.dest}/{cidr} via {r.gateway}")
            _run(["ip", "-force", "-batch", "-"], stdin="\n".join(lines) + "\n",
                 timeout=30.0)
        else:
            # macOS or Linux without `ip` — loop.
            for r in reversed(self.routes):
                try:
                    delete_route(r.dest, r.mask, r.gateway)
                except Exception:
                    pass
        self.routes.clear()
        for d in self.dns_changed:
            try:
                reset_dns(d.iface_name)
            except Exception:
                pass
        self.dns_changed.clear()


# ==============================================================================
# Misc — kept for parity with the Windows backend
# ==============================================================================

def _get_interface_metric_v4(if_index: int) -> int:
    """Always returns 0 on Unix — kept for API parity with Windows.

    Used by the controller to pick a route metric. On Unix the metric
    we pass to `ip route add` is what gets stored verbatim; there's no
    "interface metric" floor we need to clear like Windows has.
    """
    return 0


def resolve_domains_parallel(domains: list[str], workers: int = 32,
                             timeout: float = 3.0) -> dict[str, list[str]]:
    """Resolve domains to IPv4 addresses concurrently — pure stdlib,
    identical to the Windows version. Duplicated here so callers can
    `from .network_routes_unix import resolve_domains_parallel` without
    needing the Windows module to import cleanly first.
    """
    def _resolve_one(d: str) -> tuple[str, list[str]]:
        try:
            socket.setdefaulttimeout(timeout)
            infos = socket.getaddrinfo(d, None, socket.AF_INET)
            return d, sorted({info[4][0] for info in infos})
        except (socket.gaierror, socket.timeout, OSError):
            return d, []
        finally:
            socket.setdefaulttimeout(None)

    results: dict[str, list[str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for domain, ips in ex.map(_resolve_one, domains):
            results[domain] = ips
    return results
