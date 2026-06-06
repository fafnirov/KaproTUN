"""DNS reachability probe for TUN mode.

In TUN mode with DNS-leak protection the physical NIC's DNS is cleared, so
*every* name lookup has to travel through the tunnel's DNS path. If that path
is dead (tunnel up but resolver unreachable), the machine ends up "connected
but nothing resolves" — the classic failure that forces a manual adapter
restart. This module gives a cheap yes/no answer to "can the system resolve
names right now?" used in two places:

  * connect-time gate (controller): after raising the tunnel and clearing the
    physical DNS, confirm resolution works before committing; roll back if not.
  * runtime watchdog (GUI): periodically re-check; trigger a bounded self-heal
    on a sustained outage.

Design rules:
  * NEVER raises — every public function returns a bool. Callers must be able
    to treat a probe as advisory and never have it crash a connect/heal path.
  * Bounded wall-clock — getaddrinfo can hang far past socket timeouts on a
    broken resolver, so each lookup runs in a worker thread we abandon on
    timeout instead of trusting the C library to honour a deadline.
  * Uses the OS resolver (socket.getaddrinfo), so it exercises the exact same
    path real traffic uses — including whichever interface currently owns DNS.
"""
from __future__ import annotations

import socket
import urllib.request
# Import the executor eagerly (not via concurrent.futures.<attr> at first use).
# concurrent.futures lazily imports its .thread submodule on first attribute
# access, and that submodule calls threading._register_atexit() at import time —
# which raises "can't register atexit after shutdown" if the FIRST probe ever
# happens during interpreter teardown. Importing here, at app startup, does the
# registration early and once, so a probe fired from the shutdown path is safe.
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FuturesTimeout

# Well-known, highly-available hosts that are NOT in any RU-direct list, so in
# TUN mode they resolve *through the tunnel* — exactly the path we want to test.
# Multiple, so one provider's hiccup doesn't read as a tunnel outage.
_DEFAULT_HOSTS = ("cloudflare.com", "www.google.com", "github.com")


def _resolve_once(host: str, timeout: float) -> bool:
    """True if `host` resolves to at least one A record within `timeout`s.

    getaddrinfo has no timeout argument and can block well past any socket
    deadline when the resolver is black-holing queries, so we run it in a
    throwaway thread and give up on it after `timeout`. The abandoned thread
    dies on its own once the C call returns or errors; we never join it.
    """
    def _lookup() -> bool:
        try:
            socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            return True
        except OSError:
            return False
        except Exception:
            return False

    try:
        ex = ThreadPoolExecutor(max_workers=1)
    except Exception:
        # e.g. interpreter shutting down — treat as "can't resolve right now".
        return False
    try:
        fut = ex.submit(_lookup)
        try:
            return bool(fut.result(timeout=max(0.1, timeout)))
        except _FuturesTimeout:
            return False
        except Exception:
            return False
    finally:
        # Don't block on the possibly-stuck lookup thread.
        ex.shutdown(wait=False)


def probe(timeout: float = 2.0, attempts: int = 2,
          hosts: tuple[str, ...] | None = None) -> bool:
    """True if DNS resolution works right now.

    Returns as soon as ANY host in ANY attempt resolves (fast success path).
    Only returns False after every host has failed `attempts` times — so a
    single flaky lookup never reads as an outage. Worst-case wall-clock is
    bounded by `len(hosts) * attempts * timeout`.
    """
    host_list = hosts if hosts else _DEFAULT_HOSTS
    a = max(1, int(attempts))
    for _ in range(a):
        for h in host_list:
            try:
                if _resolve_once(h, timeout):
                    return True
            except Exception:
                # _resolve_once already swallows everything, but stay paranoid:
                # a probe must never escalate to an exception.
                continue
    return False


# Small, always-on captive-portal endpoints that answer 204 No Content over
# plain HTTP — tiny, no TLS-through-proxy CONNECT dance, and run by three
# different operators so one being down doesn't read as a dead tunnel.
_TUNNEL_PROBE_URLS = (
    "http://cp.cloudflare.com/",
    "http://www.gstatic.com/generate_204",
    "http://connectivitycheck.gstatic.com/generate_204",
)

_SYSTEM_TUN_PROBE_URLS = (
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://www.gstatic.com/generate_204",
)
_EGRESS_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


def singbox_outbound_probe(proxy_url: str, timeout: float = 2.5) -> bool:
    """Prove that sing-box's selected proxy outbound carries real traffic.

    The request enters a loopback-only mixed inbound whose route is pinned to
    outbound=proxy. It cannot accidentally pass through direct-domain or geoip
    routing and therefore cannot mark a dead VPN transport as connected.
    """
    return http_probe(proxy_url, timeout=timeout, urls=_SYSTEM_TUN_PROBE_URLS)


def _trace_egress_ip(proxy_url: str | None, timeout: float = 2.5) -> str:
    """Return IPv4 from Cloudflare trace through an explicit or system path."""
    try:
        proxies = ({"http": proxy_url, "https": proxy_url}
                   if proxy_url else {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        req = urllib.request.Request(
            _EGRESS_TRACE_URL,
            headers={"User-Agent": "KaproTUN-egress-health"},
        )
        with opener.open(req, timeout=max(0.3, timeout)) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
        for line in body.splitlines():
            if line.startswith("ip="):
                ip = line[3:].strip()
                if ip and ":" not in ip and ip.count(".") == 3:
                    return ip
    except Exception:
        pass
    return ""


def singbox_system_tun_healthy(proxy_url: str, timeout: float = 2.5) -> bool:
    """Verify both the VPN outbound and Windows' ordinary TUN data path.

    The loopback health inbound proves the selected outbound. The no-proxy
    request proves the normal system stack is captured by TUN. Requiring the
    same public IPv4 prevents a live local proxy from masking broken auto_route,
    WFP, DNS, MTU, or split-routing.
    """
    proxy_ip = _trace_egress_ip(proxy_url, timeout)
    if not proxy_ip:
        return False
    system_ip = _trace_egress_ip(None, timeout)
    return bool(system_ip and system_ip == proxy_ip)


def http_probe(proxy_url: str, timeout: float = 3.0,
               urls: tuple[str, ...] | None = None) -> bool:
    """True if an HTTP request succeeds THROUGH `proxy_url`.

    Unlike probe() (which exercises the OS resolver), this drives traffic
    through xray's local HTTP inbound, so the request is resolved by xray's own
    dns block and dialed over the proxy outbound — i.e. it tests the TUNNEL
    itself (transport + xray-side DNS), independent of the machine's DNS state.
    That makes it the liveness signal that still works when leak protection is
    OFF (where OS DNS goes direct and proves nothing about the tunnel) and the
    signal that turns a dead REALITY transport into a fast, clean connect
    failure instead of a "connected but nothing works" limbo.

    Returns True on the first 2xx/3xx (204 included). Never raises; worst-case
    wall-clock is len(urls) * timeout, only paid when the tunnel is actually
    dead. The proxy is reached over IPv4 loopback, so no resolution is needed
    to talk to it.
    """
    targets = urls if urls else _TUNNEL_PROBE_URLS
    try:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        opener = urllib.request.build_opener(handler)
    except Exception:
        return False
    for u in targets:
        try:
            req = urllib.request.Request(
                u, method="GET",
                headers={"User-Agent": "KaproTUN-healthprobe"})
            with opener.open(req, timeout=max(0.2, timeout)) as resp:
                code = getattr(resp, "status", None)
                if code is None:
                    code = resp.getcode()
                if code is not None and 200 <= int(code) < 400:
                    return True
        except Exception:
            continue
    return False
