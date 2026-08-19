"""One-shot network diagnostics snapshot for the Network Diagnostics screen.

Answers "what does my networking actually look like right now?" without making
the user run half a dozen PowerShell commands — and without the traps that make
manual diagnosis wrong while a TUN VPN is up (see ICMP_NOTE).

Design rules:
  * Pure data + plain strings, NO Qt — unit-testable, safe on a worker thread.
  * Every probe is bounded and best-effort: a missing psutil, a hostile
    firewall or an unplugged adapter must degrade to "n/a", never raise.
  * Read-only. Nothing here changes routes, DNS or firewall state.
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

from . import sing_box_config

# Why `ping`/`tracert` lie while the VPN is up. The default TUN stack is gVisor
# — a USERSPACE TCP/IP stack. It answers ICMP echo itself instead of forwarding
# it, so `ping 8.8.8.8` reports <1 ms with TTL=64 (a locally generated reply)
# and `tracert` shows the destination one hop away. That is expected for this
# stack: it is not a bug and not evidence that traffic is hijacked — real
# TCP/UDP still traverses the tunnel normally. Diagnose with the TCP/UDP tests
# below, which measure the actual data path, instead of ping.
ICMP_NOTE = (
    "ping/tracert недостоверны при включённом VPN: userspace-стек (gVisor) "
    "отвечает на ICMP локально (<1 мс, TTL=64), не отправляя пакет наружу. "
    "Это ожидаемое поведение стека, а не подмена трафика — реальные TCP/UDP "
    "идут через туннель как обычно. Для диагностики используйте тесты TCP/UDP."
)


@dataclass
class IfaceInfo:
    name: str = ""
    index: int = 0
    ipv4: str = ""
    gateway: str = ""
    metric: int = 0
    mtu: int = 0


@dataclass
class ProbeResult:
    ok: bool = False
    ms: float = 0.0
    detail: str = ""


@dataclass
class Snapshot:
    physical: IfaceInfo = field(default_factory=IfaceInfo)
    tun: IfaceInfo = field(default_factory=IfaceInfo)
    default_routes: list = field(default_factory=list)
    server_host: str = ""
    server_ip: str = ""
    server_route: str = ""
    outbound_iface: str = ""
    singbox_running: bool = False
    singbox_version: str = ""
    tun_stack: str = ""
    tun_mtu: int = 0
    tcp: ProbeResult = field(default_factory=ProbeResult)
    udp: ProbeResult = field(default_factory=ProbeResult)
    proxy_latency: ProbeResult = field(default_factory=ProbeResult)
    icmp_note: str = ICMP_NOTE
    probes_ran: bool = False
    errors: list = field(default_factory=list)


def _ps(cmd: str, timeout: float = 8.0) -> str:
    """Run a PowerShell one-liner, return stdout ('' on any failure)."""
    import subprocess
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.stdout or ""
    except Exception:
        return ""


def tcp_probe(host: str = "www.cloudflare.com", port: int = 443,
              timeout: float = 4.0) -> ProbeResult:
    """Plain TCP connect — exercises the real data path (unlike ICMP)."""
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ProbeResult(True, (time.monotonic() - t0) * 1000.0,
                               f"{host}:{port}")
    except Exception as e:
        return ProbeResult(False, 0.0, f"{host}:{port}: {type(e).__name__}")


def udp_probe(host: str = "8.8.8.8", port: int = 53,
              timeout: float = 4.0) -> ProbeResult:
    """Round-trip a real DNS query over UDP.

    This is the test that matters for games: it proves UDP survives the tunnel,
    which ICMP cannot tell you (gVisor answers ping locally). Hand-built 17-byte
    query for the root zone so no DNS library is needed."""
    query = (b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
             b"\x00\x00\x02\x00\x01")
    s = None
    t0 = time.monotonic()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(query, (host, port))
        data, _ = s.recvfrom(512)
        if len(data) < 4 or data[:2] != query[:2]:
            return ProbeResult(False, 0.0, "malformed UDP reply")
        return ProbeResult(True, (time.monotonic() - t0) * 1000.0,
                           f"{host}:{port}")
    except Exception as e:
        return ProbeResult(False, 0.0, f"{host}:{port}: {type(e).__name__}")
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def _iface_mtu(name: str) -> int:
    try:
        import psutil
        st = psutil.net_if_stats().get(name)
        return int(getattr(st, "mtu", 0) or 0)
    except Exception:
        return 0


def _iface_ipv4(name: str) -> str:
    try:
        import psutil
        for a in psutil.net_if_addrs().get(name, []):
            if getattr(a, "family", None) == socket.AF_INET:
                return str(a.address)
    except Exception:
        pass
    return ""


def _default_routes() -> list:
    cmd = (
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | ForEach-Object { "
        "'ifIndex=' + $_.ifIndex + ' via ' + $_.NextHop + "
        "' metric=' + $_.RouteMetric }"
    )
    return [l.strip() for l in _ps(cmd).splitlines() if l.strip()]


def _server_route(server_ip: str) -> str:
    cmd = (
        "Find-NetRoute -RemoteIPAddress '" + server_ip + "' "
        "-ErrorAction SilentlyContinue | Where-Object { $_.NextHop } | "
        "Select-Object -First 1 | ForEach-Object { "
        "'ifIndex=' + $_.InterfaceIndex + ' via ' + $_.NextHop }"
    )
    out = _ps(cmd).strip()
    return out.splitlines()[0].strip() if out else ""


def collect(manager=None, quick: bool = False) -> Snapshot:
    """Gather the whole picture. `quick=True` skips the network probes.

    `manager` (ConnectionManager) is optional — with it we can report the live
    server/engine state; without it the adapter/route half still works."""
    snap = Snapshot()
    try:
        snap.tun_stack = sing_box_config.TUN_STACK
        snap.tun_mtu = sing_box_config.TUN_MTU
        tun_name = sing_box_config.TUN_DEVICE_NAME
        snap.tun = IfaceInfo(name=tun_name, ipv4=_iface_ipv4(tun_name),
                             mtu=_iface_mtu(tun_name))
    except Exception as e:
        snap.errors.append(f"tun: {type(e).__name__}")

    try:
        from . import network_routes
        info = network_routes.get_default_route_v4()
        if info is not None:
            snap.physical = IfaceInfo(
                name=info.name, index=info.index, gateway=info.gateway,
                metric=getattr(info, "interface_metric", 0),
                ipv4=_iface_ipv4(info.name), mtu=_iface_mtu(info.name))
            snap.outbound_iface = info.name
    except Exception as e:
        snap.errors.append(f"physical: {type(e).__name__}")

    try:
        snap.default_routes = _default_routes()
    except Exception as e:
        snap.errors.append(f"routes: {type(e).__name__}")

    if manager is not None:
        try:
            snap.singbox_running = bool(manager.sing_box_process.is_running())
            snap.server_ip = getattr(manager, "_server_ip", "") or ""
            cfg = manager.active_config()
            if cfg is not None:
                snap.server_host = str(cfg.outbound.get("server", ""))
        except Exception as e:
            snap.errors.append(f"manager: {type(e).__name__}")
    try:
        from . import sing_box_installer
        snap.singbox_version = sing_box_installer.get_installed_version() or ""
    except Exception:
        pass

    if snap.server_ip:
        try:
            snap.server_route = _server_route(snap.server_ip)
        except Exception as e:
            snap.errors.append(f"server_route: {type(e).__name__}")

    if not quick:
        snap.probes_ran = True
        snap.tcp = tcp_probe()
        snap.udp = udp_probe()
        if snap.singbox_running:
            try:
                from . import dns_health
                url = (f"http://{sing_box_config.HEALTH_PROXY_HOST}:"
                       f"{sing_box_config.HEALTH_PROXY_PORT}")
                t0 = time.monotonic()
                ok = dns_health.singbox_outbound_probe(url, timeout=5.0)
                snap.proxy_latency = ProbeResult(
                    ok, (time.monotonic() - t0) * 1000.0,
                    "через health-inbound (outbound=proxy)")
            except Exception as e:
                snap.errors.append(f"proxy_probe: {type(e).__name__}")
    return snap


def _wrap(text: str, width: int) -> list:
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def format_report(snap: Snapshot) -> str:
    """Human-readable, copy-pasteable report — what the user sends us."""
    def ms(p: ProbeResult) -> str:
        if not snap.probes_ran:
            return "—"
        return f"{p.ms:.0f} мс" if p.ok else "—"

    def verdict(p: ProbeResult) -> str:
        # A probe that never ran is not a failure — saying FAIL there would be
        # a lie the user would act on.
        if not snap.probes_ran:
            return "н/в"
        return "OK" if p.ok else "FAIL"

    L = ["=== KaproTUN — сетевая диагностика ===", ""]
    L.append("[Физический адаптер]")
    L.append(f"  Имя:      {snap.physical.name or 'н/д'}")
    L.append(f"  Индекс:   {snap.physical.index or 'н/д'}")
    L.append(f"  IPv4:     {snap.physical.ipv4 or 'н/д'}")
    L.append(f"  Шлюз:     {snap.physical.gateway or 'н/д'}")
    L.append(f"  Метрика:  {snap.physical.metric or 'н/д'}")
    L.append(f"  MTU:      {snap.physical.mtu or 'н/д'}")
    L.append("")
    L.append("[TUN-адаптер]")
    L.append(f"  Имя:      {snap.tun.name or 'н/д'}")
    L.append(f"  IPv4:     {snap.tun.ipv4 or 'не поднят'}")
    L.append(f"  MTU:      {snap.tun.mtu or snap.tun_mtu} "
             f"(в конфиге: {snap.tun_mtu})")
    L.append(f"  Стек:     {snap.tun_stack}")
    L.append("")
    L.append("[Маршруты по умолчанию]")
    for r in (snap.default_routes or ["н/д"]):
        L.append(f"  {r}")
    L.append("")
    L.append("[VPN-сервер]")
    L.append(f"  Хост:     {snap.server_host or 'не подключён'}")
    L.append(f"  IP:       {snap.server_ip or 'н/д'}")
    L.append(f"  Маршрут:  {snap.server_route or 'н/д'}")
    L.append(f"  Выход:    {snap.outbound_iface or 'н/д'}")
    L.append("")
    L.append("[Движок]")
    L.append(f"  sing-box: {'запущен' if snap.singbox_running else 'остановлен'}")
    L.append(f"  Версия:   {snap.singbox_version or 'н/д'}")
    L.append("")
    L.append("[Тесты канала]")
    L.append(f"  TCP:    {verdict(snap.tcp):>4}  {ms(snap.tcp):>8}   {snap.tcp.detail}")
    L.append(f"  UDP:    {verdict(snap.udp):>4}  {ms(snap.udp):>8}   {snap.udp.detail}")
    L.append(f"  Прокси: {verdict(snap.proxy_latency):>4}  "
             f"{ms(snap.proxy_latency):>8}   {snap.proxy_latency.detail}")
    L.append("")
    L.append("[ICMP]")
    for line in _wrap(snap.icmp_note, 70):
        L.append(f"  {line}")
    if snap.errors:
        L.append("")
        L.append("[Ошибки сбора]")
        for e in snap.errors:
            L.append(f"  {e}")
    return "\n".join(L)
