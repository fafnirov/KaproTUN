"""Generate a sing-box runtime config for TUN mode (v3.0.0 primary engine).

The big win over the classic xray+tun2socks dataplane: sing-box owns the TUN
device natively and proxies/routes itself, so there is NO local SOCKS bridge
(127.0.0.1:2081) and NO separate tun2socks process — the loopback ephemeral-port
exhaustion that wedged the classic engine simply cannot happen. sing-box's
`auto_route` + `route.auto_detect_interface` also send `direct` traffic straight
out the physical NIC, so the freedom→TUN routing loop is impossible too.

Protocol mapping is almost free: core/parser.py already emits sing-box-shaped
outbound dicts (that's what ProxyConfig.outbound is — xray_config converts the
OTHER way). We copy it, tag it `proxy`, and pin its `server` to the
already-resolved IP so sing-box needs no DNS bootstrap.

Anything sing-box can't faithfully do raises UnsupportedBySingBox so the caller
can offer the legacy engine — never a silent wrong behaviour.
"""
from __future__ import annotations

import json
import socket
import struct
from typing import Any, Optional

from . import dns_options, geoip_ru, paths

# TUN device + addressing — mirrors the classic engine so nothing else changes.
TUN_DEVICE_NAME = "KaproTun"
TUN_INET4 = "10.255.0.2/30"
# v3.0.9: give the TUN a ULA IPv6 address so auto_route ALSO captures ::/0 into
# the tunnel. Without this the TUN was IPv4-only, native IPv6 stayed on the
# physical NIC, and IPv6-leak protection had to firewall-block 2000::/3 — which
# returns WSAEACCES → the browser's ERR_NETWORK_ACCESS_DENIED. With v6 captured
# in-tunnel we instead REJECT global-unicast v6 with a clean TCP RST (see the
# route rules), so Happy-Eyeballs falls back to IPv4 instantly and v6 never leaks.
TUN_INET6 = "fdfe:dcba:9876::1/126"
# Conservative internet-safe MTU. A jumbo 9000-byte TUN MTU made tiny probes
# succeed while larger TLS/WebSocket/video flows stalled on paths where PMTUD or
# fragmentation was filtered. 1400 leaves room for VLESS/REALITY and other
# encapsulation overhead without depending on ICMP fragmentation feedback.
TUN_MTU = 1400
# v3.0.9: "mixed" = kernel/system TCP stack (fast — web/video/downloads) + gVisor
# only for UDP. This is sing-box's OWN default on Windows; the prior "gvisor"
# (whole L3→L4 in userspace) was the documented worst case for Windows throughput
# and CPU and was the main cause of the slow tunnel.
TUN_STACK = "mixed"
HEALTH_PROXY_HOST = "127.0.0.1"
HEALTH_PROXY_PORT = 2082

# Private / LAN / Docker / link-local / loopback + multicast/broadcast that must
# always stay OFF the tunnel (routed to `direct`, which exits the physical NIC).
PRIVATE_CIDRS: list[str] = [
    "10.0.0.0/8",
    "172.16.0.0/12",      # Docker/WSL 172.19.x lives here
    "192.168.0.0/16",
    "169.254.0.0/16",
    "127.0.0.0/8",
    "224.0.0.0/4",        # multicast
    "255.255.255.255/32",  # limited broadcast
]

# IPv6 LAN / ULA / link-local / multicast — kept DIRECT (NAS, printers, local
# discovery keep working) and, crucially, NOT rejected by the global-v6 reject
# rule below. Everything else in global unicast (2000::/3) is rejected in-tunnel.
PRIVATE_CIDRS6: list[str] = [
    "fc00::/7",     # Unique Local Addresses (incl. our own TUN_INET6)
    "fe80::/10",    # link-local
    "ff00::/8",     # multicast
]

# Leak-protected system DNS upstreams (shared idea with the classic engine):
# plain DNS sent THROUGH the tunnel, so the ISP sees only encrypted bytes.
_SYSTEM_DNS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]

# Globally-restricted services that must ALWAYS ride the proxy, even with
# route_ru_direct on. Their CDNs frequently resolve to IPs that land in
# geoip:ru (Cloudflare / Fastly RU edge), and a geoip:ru rule would then send
# them out the real IP — which breaks the (geo-restricted) service: ChatGPT
# loads but files.oaiusercontent.com images hang. Matched by SNIFFED SNI (a
# sub-resource like files.oaiusercontent.com is forced through proxy regardless
# of its IP), so this rule must sit BEFORE any direct/bypass rule.
_ALWAYS_PROXY_SUFFIXES = [
    # Deterministic system-TUN health endpoint. The same Cloudflare trace is
    # requested through the loopback health proxy and through Windows' normal
    # network stack; both must report the same VPN egress IP.
    "www.cloudflare.com",
    # OpenAI / ChatGPT
    "openai.com",
    "chatgpt.com",
    "oaistatic.com",
    "oaiusercontent.com",   # covers files.oaiusercontent.com, *.oaiusercontent.com
    # YouTube + its CDNs — these often land in geoip:ru (Google Global Cache /
    # GGC nodes hosted by RU ISPs), which would otherwise pull them out the real
    # IP and either geo-restrict or kill throughput. The user expects YouTube
    # "through the VPN", so force it. NOTE: we list the YouTube-specific
    # googleapis host only (youtubei.googleapis.com), NOT bare googleapis.com —
    # that would drag every Google API + RU services that legitimately use it.
    "youtube.com",
    "youtu.be",
    "googlevideo.com",      # *.googlevideo.com — the actual video byte streams
    "ytimg.com",            # i.ytimg.com thumbnails
    "ggpht.com",            # YouTube avatars/thumbs
    "youtubei.googleapis.com",
]


def _dns_resolver_ips(dns_option: str) -> list[str]:
    """The plain upstream resolver IP(s) the leak-OFF DNS dials. Used to route
    that DNS traffic straight out the physical NIC (fast, ISP-visible) instead
    of letting it crawl through the proxy."""
    opt = dns_options.get(dns_option)
    ips = list(opt.plain_servers) if opt.plain_servers else list(_SYSTEM_DNS)
    out: list[str] = []
    for ip in ips:
        ip = str(ip).strip()
        if ip and ip not in out:
            out.append(ip)
    return out[:3]


class UnsupportedBySingBox(Exception):
    """The parsed config uses a feature sing-box can't faithfully reproduce.
    The caller should offer the legacy (xray + tun2socks) engine, NOT silently
    switch or produce wrong behaviour."""


def _mask_to_prefix(mask: str) -> int:
    return bin(struct.unpack(">I", socket.inet_aton(mask))[0]).count("1")


def _ru_cidrs() -> list[str]:
    """geoip:ru as CIDR strings for a sing-box ip_cidr rule (from the same
    cached source the classic engine uses for its kernel routes)."""
    out: list[str] = []
    try:
        for net, mask in geoip_ru.load_cidrs():
            out.append(f"{net}/{_mask_to_prefix(mask)}")
    except Exception:
        pass
    return out


def ensure_supported(outbound: dict[str, Any]) -> None:
    """Raise UnsupportedBySingBox if `outbound` (a parser-produced sing-box
    outbound) uses something we won't ship on sing-box yet. Never raises for
    the common vless/vmess/trojan/shadowsocks/hysteria2 cases."""
    otype = str(outbound.get("type", ""))
    supported = {"vless", "vmess", "trojan", "shadowsocks", "hysteria2"}
    if otype not in supported:
        raise UnsupportedBySingBox(
            f"Протокол «{otype or '?'}» пока не поддержан в движке sing-box.")
    # Shadowsocks SIP003 plugins are configured differently in sing-box than in
    # the xray-shaped parser output — refuse rather than mis-handshake.
    if otype == "shadowsocks" and outbound.get("plugin"):
        raise UnsupportedBySingBox(
            "Shadowsocks-плагины (obfs/v2ray-plugin) пока поддержаны только в "
            "legacy-движке.")


# Transports sing-box implements as a v2ray-transport. Anything outside this
# set that isn't plain TCP (e.g. XHTTP / splithttp — Xray-only) cannot be
# faithfully reproduced; sing-box would silently fall back to plain TCP and
# mis-handshake the REALITY/TLS server ("unknown version: N" on the data
# channel). We must reject such configs, not ship a half-working outbound.
_SING_BOX_TRANSPORTS = {"ws", "grpc", "h2", "http", "httpupgrade"}
_TCP_LIKE_NETWORKS = {"", "tcp", "raw", "none"}


def ensure_transport_supported(proxy) -> None:
    """Raise UnsupportedBySingBox if the config's transport can't be faithfully
    reproduced by sing-box. The parser records the raw `network` (type=) on the
    ProxyConfig; plain-TCP and the v2ray transports sing-box implements pass,
    everything else (XHTTP/splithttp and any future Xray-only transport) is
    refused with a clear 'switch to legacy' message — NEVER silently downgraded
    to TCP."""
    network = str(getattr(proxy, "network", "") or "").strip().lower()
    # Belt-and-suspenders: XHTTP / splithttp are Xray-only and must NEVER reach
    # sing-box (they'd become a plain-TCP outbound that mis-handshakes). Catch
    # them from the raw share URL too, in case a parse path didn't populate
    # .network (e.g. a config carried over from an older build).
    raw = str(getattr(proxy, "raw_url", "") or "").lower()
    if ("xhttp" in network or "splithttp" in network
            or "type=xhttp" in raw or "type=splithttp" in raw):
        raise UnsupportedBySingBox(
            "Транспорт XHTTP/splithttp поддержан только в Xray. Переключи движок "
            "на «Legacy (Xray + tun2socks)» в Настройках и подключись снова.")
    if network in _TCP_LIKE_NETWORKS or network in _SING_BOX_TRANSPORTS:
        return
    raise UnsupportedBySingBox(
        f"Транспорт «{network}» поддержан только в Xray. Переключи движок на "
        f"«Legacy (Xray + tun2socks)» в Настройках и подключись снова.")


def _dns_block(dns_option: str, dns_leak_protection: bool,
               direct_domains: Optional[list[str]] = None) -> dict[str, Any]:
    opt = dns_options.get(dns_option)
    upstream = opt.plain_servers[0] if opt.plain_servers else _SYSTEM_DNS[0]
    # sing-box 1.12+ DNS server format: typed servers. Legacy {"address": ...}
    # is FATAL in 1.13.
    if dns_leak_protection:
        # v3.0.13: encrypted DNS must not depend on the selected VPN transport.
        # A dead/invalid Hysteria2 or a flaky VLESS connection previously took
        # the shared DoH connection down with it, blacking out the whole machine
        # even though DNS leak protection was supposed to improve reliability.
        #
        # Direct DoH still protects query contents: the ISP sees an HTTPS
        # connection to the configured public resolver, not plaintext UDP/53 or
        # the requested domain. The resolver endpoint is routed DIRECT by an
        # explicit route rule below, while resolved application traffic still
        # follows forced-proxy/direct/geoip rules normally.
        _ = direct_domains
        return {
            "servers": [
                {"type": "https", "tag": "dns-secure-direct",
                 "server": upstream, "connect_timeout": "4s"},
            ],
            "final": "dns-secure-direct",
            "strategy": "ipv4_only",
        }
    # Opt-out: DNS goes direct (ISP-visible) — matches the classic leak-off UX.
    # NO detour here: sing-box 1.13 rejects `"detour": "direct"` because the
    # `direct` outbound is empty ("detour to an empty direct outbound makes no
    # sense"). A DNS server with no detour already resolves over the default
    # (direct) path via auto_detect_interface, which is exactly the leak-off
    # behaviour we want.
    return {
        "servers": [
            {"type": "udp", "tag": "dns-direct", "server": upstream},
        ],
        "final": "dns-direct",
        "strategy": "ipv4_only",
    }


def build_config(
    proxy,
    direct_domains: list[str],
    *,
    server_ip: str = "",
    dns_option: str = "system",
    dns_leak_protection: bool = True,
    block_ads: bool = False,
    route_ru_direct: bool = False,
    log_level: str = "warn",
    on_log=None,
) -> dict[str, Any]:
    """Full sing-box config dict for TUN mode. Raises UnsupportedBySingBox if
    the proxy can't be faithfully reproduced. `on_log` (optional) receives
    human notices about limitations (e.g. ad-block)."""
    outbound = dict(proxy.outbound)
    ensure_supported(outbound)
    # Transport gate: reject XHTTP/splithttp etc. that the parser can't render
    # as a sing-box transport (it would otherwise become a plain-TCP outbound
    # that mis-handshakes the server). Raises UnsupportedBySingBox → 'use legacy'.
    ensure_transport_supported(proxy)
    outbound["tag"] = "proxy"
    # Pin the connect target to the resolved IP so sing-box needs no DNS to
    # reach the server (TLS/REALITY SNI stays in outbound["tls"]). Falls back to
    # the parsed host if we weren't given an IP.
    if server_ip:
        outbound["server"] = server_ip

    # --- routing rules (first match wins) — sing-box 1.12+ action grammar ---
    # The legacy `{"outbound": "..."}` rule shape and the `block`/`dns` outbound
    # types are deprecated (1.11) and removed/fatal (1.13). The modern form uses
    # explicit actions: `sniff` (detect domain from SNI/Host), `hijack-dns`
    # (answer :53 from the dns module — no `dns` outbound needed), and
    # `route` → outbound for everything else.
    rules: list[dict[str, Any]] = [
        # Sniff first so domain_suffix rules below can match TLS SNI / HTTP Host.
        {"action": "sniff"},
        {"inbound": ["health-probe"], "action": "route", "outbound": "proxy"},
    ]
    # Resolver egress must never fall through to final=proxy:
    #   leak ON  -> encrypted DoH direct on TCP/443
    #   leak OFF -> plaintext DNS direct on UDP/TCP 53
    # This rule is before hijack-dns so sing-box's own upstream exchange cannot
    # be re-hijacked into a loop.
    resolver_ips = _dns_resolver_ips(dns_option)
    if resolver_ips:
        rules.append({
            "ip_cidr": [f"{ip}/32" for ip in resolver_ips],
            "port": [443] if dns_leak_protection else [53],
            "action": "route",
            "outbound": "direct",
        })
    # Hijack all OTHER DNS to the dns module (replaces the old dns-out outbound).
    rules.append({"protocol": "dns", "action": "hijack-dns"})
    # Private / LAN / Docker / multicast — never tunnel.
    rules.append({"ip_cidr": list(PRIVATE_CIDRS), "action": "route", "outbound": "direct"})
    # IPv6 (v3.0.9): the TUN now carries an inet6 address, so auto_route captures
    # ::/0 into the tunnel. LAN/ULA/link-local/multicast v6 stays DIRECT (NAS,
    # printers, local discovery). All GLOBAL-UNICAST v6 (2000::/3) is REJECTED
    # in-tunnel with a TCP RST (method=default → RST for TCP, ICMP unreachable for
    # UDP): the browser's IPv6 attempt fails instantly and Happy-Eyeballs falls
    # back to IPv4 — NO firewall WSAEACCES, hence no ERR_NETWORK_ACCESS_DENIED — and
    # v6 never egresses the physical NIC (no leak). LAN-direct MUST precede reject.
    rules.append({"ip_cidr": list(PRIVATE_CIDRS6), "action": "route", "outbound": "direct"})
    rules.append({"ip_cidr": ["2000::/3"], "action": "reject", "method": "default"})
    # Always-proxy critical geo-restricted services BEFORE any direct/bypass
    # rule, so a CDN IP in geoip:ru can't pull them out the real interface.
    rules.append({"domain_suffix": list(_ALWAYS_PROXY_SUFFIXES),
                  "action": "route", "outbound": "proxy"})
    cleaned_domains = sorted({d.strip().lower() for d in direct_domains if d.strip()})
    if cleaned_domains:
        rules.append({"domain_suffix": cleaned_domains, "action": "route",
                      "outbound": "direct"})
    if route_ru_direct:
        ru = _ru_cidrs()
        if ru:
            rules.append({"ip_cidr": ru, "action": "route", "outbound": "direct"})
    # NOTE: block_ads is intentionally NOT honoured here — geosite:category-ads-all
    # needs the geosite DB, which we don't ship for sing-box. The Settings UI
    # disables the ad-block checkbox + shows a 'legacy only' note when sing-box
    # is the active engine, so the limitation is surfaced there ONCE rather than
    # spamming the Logs page on every (re)connect (v3.0.3). `on_log` is kept in
    # the signature for callers/future use.
    _ = (block_ads, on_log)

    dns_block = _dns_block(dns_option, dns_leak_protection, direct_domains)
    return {
        "log": {"level": log_level, "timestamp": True},
        "dns": dns_block,
        "inbounds": [{
            "type": "tun",
            "tag": "tun-in",
            "interface_name": TUN_DEVICE_NAME,
            # Both families → auto_route captures 0.0.0.0/0 AND ::/0 into the TUN,
            # so native IPv6 can't bypass the tunnel on the physical NIC.
            "address": [TUN_INET4, TUN_INET6],
            "mtu": TUN_MTU,
            "auto_route": True,
            "strict_route": False,
            "stack": TUN_STACK,
            # Full-cone NAT for the gVisor UDP half of "mixed": QUIC/HTTP3
            # (YouTube/Google video) and WebRTC reuse one mapping instead of
            # spawning per-destination sessions → less churn, better UDP throughput.
            "endpoint_independent_nat": True,
        }, {
            # Health checks only. User traffic still enters through native TUN;
            # this does not recreate the old tun2socks/SOCKS bridge.
            "type": "mixed",
            "tag": "health-probe",
            "listen": HEALTH_PROXY_HOST,
            "listen_port": HEALTH_PROXY_PORT,
        }],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": rules,
            "final": "proxy",
            # Resolve domains (for `direct` traffic, route rules) via the same
            # tunnelled resolver — required by sing-box 1.12+ when any outbound
            # may see a domain target.
            "default_domain_resolver": {"server": dns_block["final"]},
            # Send `direct` traffic out the real NIC automatically → direct
            # traffic can't loop back into the TUN.
            "auto_detect_interface": True,
        },
    }


def write_config(
    proxy,
    direct_domains: list[str],
    *,
    server_ip: str = "",
    dns_option: str = "system",
    dns_leak_protection: bool = True,
    block_ads: bool = False,
    route_ru_direct: bool = False,
    on_log=None,
) -> str:
    """Build + atomically write the runtime config (user-only perms; it carries
    the server UUID/password). Deleted on disconnect via
    paths.remove_runtime_configs(). NEVER log its contents."""
    config = build_config(
        proxy, direct_domains,
        server_ip=server_ip, dns_option=dns_option,
        dns_leak_protection=dns_leak_protection, block_ads=block_ads,
        route_ru_direct=route_ru_direct, on_log=on_log,
    )
    target = paths.write_secure_text(
        paths.sing_box_runtime_config_file(),
        json.dumps(config, indent=2, ensure_ascii=False),
    )
    return str(target)


def check_config(config_path: str) -> tuple[bool, str]:
    """Run `sing-box check -c <path>`. Returns (ok, message). Used to validate
    the generated config before starting the real process."""
    import subprocess
    exe = paths.sing_box_exe()
    if not exe.is_file():
        return False, f"sing-box not found at {exe}"
    try:
        result = subprocess.run(
            [str(exe), "check", "-c", config_path],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=str(paths.tun_dir()),
        )
        if result.returncode == 0:
            return True, "OK"
        return False, (result.stderr or result.stdout or "Unknown error").strip()
    except Exception as e:
        return False, str(e)
