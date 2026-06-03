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
TUN_MTU = 1500

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

# Leak-protected system DNS upstreams (shared idea with the classic engine):
# plain DNS sent THROUGH the tunnel, so the ISP sees only encrypted bytes.
_SYSTEM_DNS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


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


def _dns_block(dns_option: str, dns_leak_protection: bool) -> dict[str, Any]:
    opt = dns_options.get(dns_option)
    # Plain-DNS upstream IP (no DoH → no TLS-in-tunnel stalls; matches the
    # classic v1.19.1 lesson). Named option uses its plain server; system uses
    # the diverse default.
    upstream = opt.plain_servers[0] if opt.plain_servers else _SYSTEM_DNS[0]
    # sing-box 1.12+ DNS server format: typed servers ({"type":"udp","server":
    # "1.1.1.1"}). The legacy {"address": "1.1.1.1"} grammar is deprecated in
    # 1.12 and FATAL in 1.13 — we must emit the new shape so current binaries
    # accept the config.
    if dns_leak_protection:
        # DNS rides the tunnel (detour=proxy) → ISP can't see queries.
        return {
            "servers": [
                {"type": "udp", "tag": "dns-remote", "server": upstream,
                 "detour": "proxy"},
            ],
            "final": "dns-remote",
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
        # Hijack all DNS to the dns module (replaces the old dns-out outbound).
        {"protocol": "dns", "action": "hijack-dns"},
        # Private / LAN / Docker / multicast — never tunnel.
        {"ip_cidr": list(PRIVATE_CIDRS), "action": "route", "outbound": "direct"},
    ]
    cleaned_domains = sorted({d.strip().lower() for d in direct_domains if d.strip()})
    if cleaned_domains:
        rules.append({"domain_suffix": cleaned_domains, "action": "route",
                      "outbound": "direct"})
    if route_ru_direct:
        ru = _ru_cidrs()
        if ru:
            rules.append({"ip_cidr": ru, "action": "route", "outbound": "direct"})
    if block_ads and on_log:
        # geosite-based ad-block needs the geosite DB; not shipped on sing-box
        # yet. Surface the limitation honestly instead of pretending.
        on_log("[sing-box] Блокировка рекламы (block_ads) пока работает только "
               "в legacy-движке — в sing-box она не активна.")

    dns_block = _dns_block(dns_option, dns_leak_protection)
    return {
        "log": {"level": log_level, "timestamp": True},
        "dns": dns_block,
        "inbounds": [{
            "type": "tun",
            "tag": "tun-in",
            "interface_name": TUN_DEVICE_NAME,
            "address": [TUN_INET4],
            "mtu": TUN_MTU,
            "auto_route": True,
            "strict_route": False,
            "stack": "gvisor",
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
