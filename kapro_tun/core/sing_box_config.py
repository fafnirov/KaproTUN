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
import re
import socket
import struct
import subprocess
import sys
from typing import Any, Optional

from . import geoip_ru, paths

# Linux runs sing-box with auto_route OFF (the kernel-7.0 netlink incompat breaks
# it) and replicates routing via iproute2 — see core/linux_tun_route.py.
_IS_LINUX = sys.platform.startswith("linux")

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
# TUN network stack. "gvisor" runs the whole L3→L4 in userspace; "mixed" uses
# the kernel TCP stack (only UDP via gVisor) and is faster ON PAPER — but on
# real Windows machines (drivers / AV / 3rd-party network filters) the kernel
# path through WinTUN frequently carries NO traffic at all: the tunnel comes up,
# the egress IP flips to the VPN, yet every app request dies with a connection
# error (v3.1.3 field repro: `mixed`/`system` → 100% URLError, `gvisor` →
# Google/Telegram load fine). gVisor is the universally-working path, so we
# default to it for reliability over a throughput optimisation that doesn't
# deliver when it doesn't connect. (A future Settings toggle could let advanced
# users pick `mixed` where their NIC supports it.)
TUN_STACK = "gvisor"
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
            "Shadowsocks-плагины (obfs/v2ray-plugin) не поддерживаются. "
            "Используй сервер без плагина.")


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
            "Транспорт XHTTP/splithttp не поддерживается этим клиентом. "
            "Возьми сервер на обычном TCP / WS / gRPC.")
    if network in _TCP_LIKE_NETWORKS or network in _SING_BOX_TRANSPORTS:
        return
    raise UnsupportedBySingBox(
        f"Транспорт «{network}» не поддерживается этим клиентом. "
        f"Возьми сервер на обычном TCP / WS / gRPC.")


def _dns_block() -> dict[str, Any]:
    """DNS is ALWAYS the system resolver (v3.1.1).

    The previous custom DoH / smart-split resolver was DPI-throttled on many
    RU networks: the direct DoH exchange to a public resolver timed out
    ("dns: exchange failed ... context deadline exceeded"), which black-holed
    DNS *and* failed the connect-gate's egress trace — so a perfectly good VPN
    transport got reported as "doesn't pass real traffic". A `type: local`
    server resolves through the OS resolver over the physical NIC (the user's
    already-working ISP/router DNS), so DNS can never be the thing that blocks a
    connect. App :53 is still hijacked into this module (see the route rules),
    so every lookup follows one consistent, reliable path.

    Trade-off the user explicitly chose: queries reach the system DNS as plain
    UDP/53 (no DoH), i.e. the ISP can see requested domains — reliability over
    query-content hiding. `ipv4_only` keeps apps off AAAA, matching the
    in-tunnel 2000::/3 reject so no app wastes time on an IPv6 attempt.

    No loop: `local` does NOT ride a sing-box outbound — it dials the OS
    resolver, which sing-box binds to the default physical interface
    (auto_detect_interface). On Windows that means the stub at 127.0.0.1:53 and
    any upstream it forwards to leave via the real NIC, NOT back through the TUN,
    so the hijacked-:53 → local-resolve path can't re-capture itself."""
    if _IS_LINUX:
        # On Linux the OS resolver is systemd-resolved (127.0.0.53). A `type:
        # local` server would ask IT, but systemd-resolved forwards back through
        # the TUN where hijack-dns re-captures the query → an infinite loop that
        # black-holes DNS (the classic systemd-resolved + hijack-dns regression).
        # So resolve through an explicit UDP upstream — the system's real
        # upstream when we can read it, else a public fallback. sing-box dials it
        # with route.default_mark set, so the query leaves via the physical NIC,
        # not back into the tunnel.
        return {
            "servers": [{"type": "udp", "server": _linux_upstream_dns(),
                         "tag": "local"}],
            "final": "local",
            "strategy": "ipv4_only",
        }
    return {
        "servers": [{"type": "local", "tag": "local"}],
        "final": "local",
        "strategy": "ipv4_only",
    }


def _linux_upstream_dns() -> str:
    """The system's real upstream DNS on Linux (for the udp resolver above).

    Reads `resolvectl dns`, drops the 127.x stub (systemd-resolved itself), and
    returns the first real server. Falls back to a public resolver if that can't
    be read. Called once per connect at config-generation time."""
    try:
        out = subprocess.run(["resolvectl", "dns"], capture_output=True,
                             text=True, timeout=5).stdout
        for ip in re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", out):
            if not ip.startswith("127."):
                return ip
    except (OSError, subprocess.SubprocessError):
        pass
    return "8.8.8.8"


def build_config(
    proxy,
    direct_domains: list[str],
    *,
    server_ip: str = "",
    dns_option: str = "system",
    dns_leak_protection: bool = True,
    block_ads: bool = False,
    route_ru_direct: bool = False,
    high_speed: bool = False,
    log_level: str = "warn",
    on_log=None,
) -> dict[str, Any]:
    """Full sing-box config dict for TUN mode. Raises UnsupportedBySingBox if
    the proxy can't be faithfully reproduced. `on_log` (optional) receives
    human notices about limitations (e.g. ad-block).

    `high_speed` (v3.3.0): False → gvisor stack (userspace, universally works);
    True → "mixed" stack (kernel TCP, much faster, but carries no traffic on
    some Windows setups). Opt-in "Turbo" — see TUN_STACK.

    `dns_option` / `dns_leak_protection` are accepted for call-site
    compatibility but IGNORED as of v3.1.1: DNS is always the system resolver
    (see _dns_block). They stay in the signature so older callers/tests don't
    break, not because they do anything."""
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
    # Hijack all DNS (:53) into the dns module, which resolves via the system
    # resolver (type: local) over the physical NIC. No resolver-IP carve-out
    # rule is needed any more: `local` dials the OS resolver directly instead of
    # riding a sing-box outbound, so there is nothing to re-hijack into a loop.
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
    _ = (block_ads, on_log, dns_option, dns_leak_protection)

    dns_block = _dns_block()
    # Platform-split TUN inbound. Windows/macOS: native auto_route owns routing
    # and captures both v4+v6. Linux (kernel 7.0+): auto_route's netlink calls
    # fail with "add route 0: invalid argument", so we run it OFF and lay routes
    # by hand (core/linux_tun_route.py); we also stay IPv4-only there to keep the
    # manual route table simple (IPv6 leak handling on Linux is a follow-up).
    tun_inbound: dict[str, Any] = {
        "type": "tun",
        "tag": "tun-in",
        "interface_name": TUN_DEVICE_NAME,
        "address": [TUN_INET4] if _IS_LINUX else [TUN_INET4, TUN_INET6],
        "mtu": TUN_MTU,
        "auto_route": not _IS_LINUX,
        "strict_route": False,
        # v3.3.0 Turbo: opt into the kernel-TCP "mixed" stack for much higher
        # throughput. Default stays gvisor (TUN_STACK) — universally works. Not
        # applied on Linux, whose manual-route path is tuned around gvisor.
        "stack": ("mixed" if (high_speed and not _IS_LINUX) else TUN_STACK),
        # Full-cone NAT for the gVisor UDP path: QUIC/HTTP3 (YouTube/Google video)
        # and WebRTC reuse one mapping instead of per-destination sessions. On
        # Linux this is also REQUIRED for plain UDP (incl. DNS) to traverse the
        # manually-routed TUN at all.
        "endpoint_independent_nat": True,
    }
    route_block: dict[str, Any] = {
        "rules": rules,
        "final": "proxy",
        # Resolve domains (for `direct` traffic, route rules) via the same
        # tunnelled resolver — required by sing-box 1.12+ when any outbound
        # may see a domain target.
        "default_domain_resolver": {"server": dns_block["final"]},
        # Send `direct` traffic out the real NIC automatically → direct
        # traffic can't loop back into the TUN.
        "auto_detect_interface": True,
    }
    if _IS_LINUX:
        # fwmark on every sing-box-originated connection so the manual ip-rule
        # can send sing-box's OWN egress (proxy + direct) out the physical NIC
        # instead of looping it back into the TUN. This is what auto_route would
        # set automatically; with auto_route off we set it ourselves.
        route_block["default_mark"] = 1
    return {
        "log": {"level": log_level, "timestamp": True},
        "dns": dns_block,
        "inbounds": [tun_inbound, {
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
        "route": route_block,
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
    high_speed: bool = False,
    on_log=None,
) -> str:
    """Build + atomically write the runtime config (user-only perms; it carries
    the server UUID/password). Deleted on disconnect via
    paths.remove_runtime_configs(). NEVER log its contents."""
    config = build_config(
        proxy, direct_domains,
        server_ip=server_ip, dns_option=dns_option,
        dns_leak_protection=dns_leak_protection, block_ads=block_ads,
        route_ru_direct=route_ru_direct, high_speed=high_speed, on_log=on_log,
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
