"""Generate Xray-core JSON configuration with split routing.

The converter re-parses the share URL stored on each ProxyConfig (rather than
re-using the sing-box-formatted outbound dict) so that Xray-specific fields
like xhttp transport, REALITY spiderX, etc. are preserved without forcing
the sing-box-format outbound to know about them.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import dns_options, paths
from .parser import ProxyConfig

DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 2080

# ---- query-string helpers (small dupes from parser.py to keep this self-contained)

def _first(qs: dict[str, list[str]], *keys: str, default: str = "") -> str:
    for k in keys:
        if k in qs and qs[k]:
            return qs[k][0]
    return default


def _split_csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()] if value else []


def _truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


def _b64_decode_padded(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    return base64.b64decode(s + "=" * pad)


# ---- stream settings (shared by vless/vmess/trojan) ----------------------

_KNOWN_NETWORKS = {"tcp", "raw", "ws", "grpc", "h2", "http", "xhttp", "httpupgrade"}


def _build_stream_settings(
    qs: dict[str, list[str]],
    server_fallback: str,
    default_network: str = "tcp",
) -> dict[str, Any]:
    """Common streamSettings block for vless/vmess/trojan."""
    network = _first(qs, "type", default=default_network).lower()
    if network == "raw":
        network = "tcp"
    if network == "http":
        network = "h2"
    if network not in _KNOWN_NETWORKS:
        network = "tcp"

    security = _first(qs, "security", default="none").lower()
    sni = _first(qs, "sni", "peer", default=server_fallback)
    alpn = _split_csv(_first(qs, "alpn"))
    fp = _first(qs, "fp")
    insecure = _truthy(_first(qs, "allowInsecure", "insecure", default="0"))

    stream: dict[str, Any] = {"network": network}

    # --- security layer ---
    if security == "tls":
        tls: dict[str, Any] = {"serverName": sni}
        if alpn:
            tls["alpn"] = alpn
        if fp:
            tls["fingerprint"] = fp
        if insecure:
            tls["allowInsecure"] = True
        stream["security"] = "tls"
        stream["tlsSettings"] = tls
    elif security == "reality":
        reality = {
            "serverName": sni,
            "publicKey": _first(qs, "pbk"),
            "shortId": _first(qs, "sid"),
            "fingerprint": fp or "chrome",
        }
        spx = _first(qs, "spx")
        if spx:
            reality["spiderX"] = spx
        stream["security"] = "reality"
        stream["realitySettings"] = reality
    else:
        stream["security"] = "none"

    # --- transport layer ---
    if network == "ws":
        ws: dict[str, Any] = {"path": _first(qs, "path", default="/")}
        host = _first(qs, "host", default=server_fallback)
        if host:
            ws["headers"] = {"Host": host}
        stream["wsSettings"] = ws
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": _first(qs, "serviceName", "servicename", "path", default="")
        }
    elif network == "h2":
        hosts = _split_csv(_first(qs, "host", default=server_fallback)) or [server_fallback]
        stream["httpSettings"] = {
            "host": hosts,
            "path": _first(qs, "path", default="/"),
        }
    elif network == "xhttp":
        xhttp: dict[str, Any] = {
            "path": _first(qs, "path", default="/"),
            "mode": _first(qs, "mode", default="auto"),
        }
        host = _first(qs, "host")
        if host:
            xhttp["host"] = host
        stream["xhttpSettings"] = xhttp
    elif network == "httpupgrade":
        stream["httpupgradeSettings"] = {
            "path": _first(qs, "path", default="/"),
            "host": _first(qs, "host", default=server_fallback),
        }
    # plain "tcp" — no extra settings block needed
    return stream


# ---- protocol-specific converters ----------------------------------------

def _vless_to_xray(url: str) -> dict[str, Any]:
    u = urlparse(url)
    qs = parse_qs(u.query)
    user_block: dict[str, Any] = {
        "id": u.username,
        "encryption": _first(qs, "encryption", default="none"),
    }
    flow = _first(qs, "flow")
    if flow:
        user_block["flow"] = flow
    return {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": u.hostname,
                "port": u.port,
                "users": [user_block],
            }],
        },
        "streamSettings": _build_stream_settings(qs, server_fallback=u.hostname or ""),
    }


def _vmess_to_xray(url: str) -> dict[str, Any]:
    payload = url[len("vmess://"):]
    data = json.loads(_b64_decode_padded(payload).decode("utf-8", errors="replace"))

    # Build a query-string-like dict to feed _build_stream_settings, so the
    # transport branch is shared with vless/trojan.
    qs: dict[str, list[str]] = {
        "type": [str(data.get("net") or "tcp")],
        "security": [("tls" if str(data.get("tls") or "").lower() == "tls" else "none")],
        "sni": [str(data.get("sni") or data.get("host") or data.get("add") or "")],
        "alpn": [str(data.get("alpn") or "")],
        "fp": [str(data.get("fp") or "")],
        "path": [str(data.get("path") or "/")],
        "host": [str(data.get("host") or "")],
        "serviceName": [str(data.get("path") or "")],
    }
    return {
        "tag": "proxy",
        "protocol": "vmess",
        "settings": {
            "vnext": [{
                "address": str(data.get("add") or ""),
                "port": int(data.get("port") or 0),
                "users": [{
                    "id": str(data.get("id") or ""),
                    "alterId": int(data.get("aid") or 0),
                    "security": str(data.get("scy") or "auto"),
                }],
            }],
        },
        "streamSettings": _build_stream_settings(qs, server_fallback=str(data.get("add") or "")),
    }


def _trojan_to_xray(url: str) -> dict[str, Any]:
    u = urlparse(url)
    qs = parse_qs(u.query)
    # Trojan defaults to TLS — if no `security` param, force tls.
    if "security" not in qs:
        qs["security"] = ["tls"]
    return {
        "tag": "proxy",
        "protocol": "trojan",
        "settings": {
            "servers": [{
                "address": u.hostname,
                "port": u.port,
                "password": unquote(u.username or ""),
            }],
        },
        "streamSettings": _build_stream_settings(qs, server_fallback=u.hostname or ""),
    }


def _ss_to_xray(url: str) -> dict[str, Any]:
    after = url[len("ss://"):]
    if "#" in after:
        after, _ = after.split("#", 1)
    query = ""
    if "?" in after:
        after, query = after.split("?", 1)

    method = password = host = ""
    port = 0
    if "@" in after:
        userinfo, hostport = after.rsplit("@", 1)
        try:
            decoded = _b64_decode_padded(userinfo).decode("utf-8")
        except Exception:
            decoded = unquote(userinfo)
        if ":" in decoded:
            method, password = decoded.split(":", 1)
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)
    else:
        decoded = _b64_decode_padded(after).decode("utf-8")
        cred, hostport = decoded.rsplit("@", 1)
        method, password = cred.split(":", 1)
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)

    qs = parse_qs(query)
    server: dict[str, Any] = {
        "address": host,
        "port": port,
        "method": method,
        "password": password,
    }
    return {
        "tag": "proxy",
        "protocol": "shadowsocks",
        "settings": {"servers": [server]},
        "streamSettings": _build_stream_settings(qs, server_fallback=host, default_network="tcp"),
    }


# ---- dispatcher ----------------------------------------------------------

def proxy_to_xray_outbound(cfg: ProxyConfig,
                           hysteria_socks_port: Optional[int] = None) -> dict[str, Any]:
    """Convert a parsed ProxyConfig into an Xray-core outbound dict.

    Hysteria2 isn't a native Xray protocol — Xray can't dial it. Instead
    the ConnectionManager runs the standalone `hysteria` client as a local
    SOCKS5 proxy and passes its port here; we then emit a plain `socks`
    outbound pointing at it, so xray keeps doing split-routing while the
    actual encrypted transport to the server is hysteria's job. Without a
    port (e.g. a bare build_config call in tests) hy2 still raises, since
    there'd be nothing to chain to.
    """
    scheme = cfg.raw_url.split("://", 1)[0].lower()
    if scheme == "vless":
        return _vless_to_xray(cfg.raw_url)
    if scheme == "vmess":
        return _vmess_to_xray(cfg.raw_url)
    if scheme == "trojan":
        return _trojan_to_xray(cfg.raw_url)
    if scheme == "ss":
        return _ss_to_xray(cfg.raw_url)
    if scheme in ("hysteria2", "hy2"):
        if hysteria_socks_port is None:
            raise NotImplementedError(
                "Hysteria2 идёт через локальный hysteria-клиент (chain по SOCKS). "
                "Подключайся через ConnectionManager, который его поднимает."
            )
        return {
            "tag": "proxy",
            "protocol": "socks",
            "settings": {
                "servers": [{
                    "address": "127.0.0.1",
                    "port": int(hysteria_socks_port),
                }],
            },
        }
    raise ValueError(f"Unknown protocol scheme: {scheme}")


# ---- full config ---------------------------------------------------------

def build_config(
    proxy: ProxyConfig,
    direct_domains: list[str],
    listen_host: str = DEFAULT_LISTEN_HOST,
    listen_port: int = DEFAULT_LISTEN_PORT,
    log_level: str = "warning",
    dns_option: str = "system",
    dns_leak_protection: bool = True,
    block_ads: bool = False,
    route_ru_direct: bool = False,
    hysteria_socks_port: Optional[int] = None,
) -> dict[str, Any]:
    """Build a complete Xray-core client config with split routing.

    The proxy outbound is first in the outbounds list — Xray uses the first
    outbound as the default for non-matching traffic, so domains not in the
    direct list go through `proxy` automatically.

    Two independent axes control DNS behavior:

      dns_option (core/dns_options.py)
        WHICH resolver to use. "system" keeps the OS chain. Named options
        (adguard/cloudflare/quad9) point xray's `dns` block at the
        provider's DoH endpoint for xray's own resolution work.

      dns_leak_protection (v1.16.8, bool)
        WHETHER to tunnel DNS through VPN. When True:
          - port :53 (TCP/UDP) gets hijacked to a `dns-out` outbound that
            re-resolves via a plain-DNS upstream (the provider's plain IPs
            if non-system, else Cloudflare 1.1.1.1 as a safe fallback)
          - transport of the upstream query rides the VPN tunnel (the
            outbound dns uses proxy outbound by default)
        When False:
          - port :53 goes direct (out the physical NIC) — the v1.8.0
            "DNS-leak hardening" legacy behavior. Pi-hole / corporate /
            locally-pinned DNS keeps working. ISP sees domain queries.
    """
    proxy_outbound = proxy_to_xray_outbound(proxy, hysteria_socks_port)
    cleaned = sorted({d.strip().lower() for d in direct_domains if d.strip()})
    dns_opt = dns_options.get(dns_option)

    # The upstream resolvers used when leak protection tunnels DNS. For a named
    # option (adguard/cloudflare/quad9) it's that provider's plain IPs; for
    # "System" it's the diverse Cloudflare+Google+Quad9 set (v2.1.5) — three
    # operators so no single resolver being unreachable through the tunnel kills
    # all resolution (the pre-v2.1.5 single-1.1.1.1 funnel was the SPOF behind
    # "connected but nothing resolves").
    leak_upstreams = (
        list(dns_opt.plain_servers)
        if dns_opt.plain_servers
        else list(dns_options.LEAK_PROTECTED_SYSTEM_UPSTREAMS)
    )
    # dns-out (the :53 hijack re-resolver) takes a single address; the real
    # failover lives in the routing carve-out + dns block below, which list all
    # of leak_upstreams. This is just the first-choice for any :53 traffic that
    # isn't aimed at one of the carved-through upstreams.
    hijack_upstream = leak_upstreams[0]

    # `domain:foo.bar` in Xray matches foo.bar AND any *.foo.bar
    domain_rules = [f"domain:{d}" for d in cleaned]

    rules: list[dict[str, Any]] = [
        # Drop the link-local discovery noise FIRST. When Windows sees
        # our new TUN interface it floods NetBIOS Name Service (UDP 137)
        # broadcasts to the TUN subnet's broadcast address — observed
        # at hundreds of packets/sec on a clean machine. Without this
        # block, tun2socks captures every one, hands it to xray's
        # socks-in, xray sends it to `direct` (private IP), and the
        # OS may loop it right back into the TUN. Result: real traffic
        # diagnostics drown in the noise and the loop wastes CPU.
        #
        # Same treatment for mDNS (5353), SSDP/UPnP (1900), and the
        # rest of the NetBIOS suite (138/139). None of these belong in
        # a VPN tunnel under any circumstances — they're LAN-scope.
        {"type": "field", "outboundTag": "block",
         "port": "137-139,1900,5353"},
        # Multicast (224.0.0.0/4) and limited broadcast (255.255.255.255)
        # also belong on the link, not in a tunnel.
        {"type": "field", "outboundTag": "block",
         "ip": ["224.0.0.0/4", "255.255.255.255/32"]},
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
    ]
    # v1.16.8: DNS handling now controlled by the dns_leak_protection
    # toggle, NOT by which DNS option is selected. The two axes are
    # independent — see build_config docstring above.
    if dns_leak_protection:
        # v1.19.1 — break the dns-out loop. The hijack below sends ALL :53
        # traffic to the dns-out re-resolver, but dns-out ITSELF forwards
        # the query to the upstream resolver on :53 — that forwarded packet
        # matches the hijack again and loops, and xray's own domainStrategy
        # resolution (the `dns` block) gets the same detour. On some networks
        # (observed on MTS RU) both stall ~11-19s, so every new domain hung.
        # Carve out queries TO the upstream resolver: route them straight
        # through the tunnel (proxy) so they resolve directly — small UDP/53
        # over the tunnel returns in ~0.4s. App DNS to any OTHER address
        # still gets the leak-protection hijack below.
        upstream_ips = leak_upstreams
        rules.append({
            "type": "field",
            "ip": [f"{ip}/32" for ip in upstream_ips],
            "port": "53",
            "outboundTag": "proxy",
        })
        # Hijack the REST of :53 traffic to dns-out, which re-resolves
        # through the upstream and routes transport via proxy (VPN tunnel).
        # ISP sees encrypted VPN bytes, not domain queries.
        rules.append({
            "type": "field",
            "outboundTag": "dns-out",
            "network": "tcp,udp",
            "port": "53",
        })
    else:
        # Opt-out: legacy "direct :53" behavior for users with Pi-hole,
        # corporate split-DNS, or any locally-pinned resolver they
        # need to actually use. ISP sees queries — that's the trade-
        # off the user accepted by turning leak-protection off.
        rules.append({"type": "field", "outboundTag": "direct",
                      "network": "udp", "port": "53"})
        rules.append({"type": "field", "outboundTag": "direct",
                      "network": "tcp", "port": "53"})
        # Belt-and-braces: queries to common public resolvers also
        # go direct, in case some app hardcoded one.
        rules.append({"type": "field", "outboundTag": "direct", "ip": [
            "1.1.1.1/32", "1.0.0.1/32",      # Cloudflare
            "8.8.8.8/32", "8.8.4.4/32",      # Google
            "9.9.9.9/32",                     # Quad9
            "77.88.8.8/32", "77.88.8.1/32",  # Yandex
            "77.88.8.88/32", "77.88.8.7/32", # Yandex safe/family
        ]})

    # Ad/tracker block via xray routing. This rule looks at the SNI / HTTP
    # CONNECT host on every outbound request and drops anything matching the
    # bundled geosite "category-ads-all" list (~10k+ known ad/tracker
    # domains, maintained by the v2fly community). Works regardless of which
    # DNS the app uses — unlike a DoH blocklist, which any app with its own
    # DoH (Chrome "Secure DNS" → 1.1.1.1) silently bypasses.
    #
    # v1.19.0: now driven by an explicit `block_ads` setting so users get
    # ad-block on ANY DNS, not just AdGuard. AdGuard still implies it
    # (back-compat — those users relied on this rule), so the effective
    # condition is "block_ads OR DNS is AdGuard".
    if block_ads or dns_opt.key == "adguard":
        # First — allow-list our own public-IP probe endpoints. v1.10.1
        # user discovered AdGuard's blocklist NXDOMAIN's ipinfo.io
        # (classed as a tracker). At the DNS layer we can't fight that —
        # the resolver answers before xray sees the packet. But at the
        # xray routing layer we can force them to "proxy" outbound BEFORE
        # the geosite:category-ads-all block kicks in. Rule order matters
        # — first match wins.
        rules.append({
            "type": "field",
            "outboundTag": "proxy",
            "domain": [
                "domain:myip.com",       # v1.10.4 primary probe endpoint
                "domain:httpbin.org",    # v1.10.4 fallback
                "domain:ipinfo.io",
                "domain:ipify.org",
                "domain:ifconfig.co",
                "domain:ifconfig.me",
                "domain:icanhazip.com",
            ],
        })
        rules.append({
            "type": "field",
            "outboundTag": "block",
            "domain": ["geosite:category-ads-all"],
        })

    if domain_rules:
        rules.append({"type": "field", "domain": domain_rules, "outboundTag": "direct"})

    # v1.19.0: route ALL Russian-geolocated traffic directly (bypass VPN)
    # by geoip, not just the curated 168-domain direct-list. domainStrategy
    # is IPIfNonMatch, so a domain that doesn't hit the list above is
    # resolved and matched here by destination IP. Catches RU banks /
    # gosuslugi / marketplaces that geofence foreign IPs but aren't (yet)
    # in default_sites.json. Opt-in — broadens what bypasses the tunnel.
    if route_ru_direct:
        rules.append({"type": "field", "ip": ["geoip:ru"], "outboundTag": "direct"})

    # API inbound for runtime stats (read by core/xray_stats.py via the
    # `xray api stats` CLI helper). Routed to the dedicated `api` outbound
    # rather than the proxy/direct ones.
    from . import xray_stats as _stats

    # DNS block — only when the user picked a named service. For "system"
    # we leave xray's default behavior (resolve via the OS).
    #
    # v1.19.1: use the resolver's PLAIN IPs, not its DoH endpoint. xray
    # resolves every domain here (domainStrategy=IPIfNonMatch) to match the
    # geoip routing rules, and these queries ride the proxy outbound (the
    # VPN tunnel). A DoH endpoint means opening a fresh TLS connection to
    # 1.1.1.1:443 *through the tunnel* for resolution — on some networks
    # (observed on MTS RU) that TLS-in-tunnel setup stalls ~11s and times
    # out ("context deadline exceeded" in xray's log), so every new domain
    # hung. A plain UDP/53 query through the same tunnel returns in ~0.4s.
    # Privacy is unchanged from the user's ISP — the query is still inside
    # the encrypted tunnel; we only drop the (server-side) DoH leg. AdGuard's
    # plain IPs still filter ads, so the ad-block option keeps working.
    # v2.1.5: give xray's built-in resolver an explicit, MULTI-server upstream
    # list with failover whenever DNS is meant to ride the tunnel — for a named
    # option (its plain IPs) and for "System" with leak protection on (the
    # diverse fallback set). Listing several servers makes xray's own domain
    # resolution (domainStrategy=IPIfNonMatch, used to match geoip/routing
    # rules and to dial outbound hosts) fail over instead of dying on one dead
    # resolver — a second cause of "connected but no traffic". For "System"
    # with leak protection OFF we keep xray on the OS resolver (no dns block),
    # exactly as before — no behavioural change for that path.
    dns_block: dict[str, Any] | None = None
    if dns_opt.plain_servers:
        dns_block = {
            "servers": list(dns_opt.plain_servers),
            "queryStrategy": "UseIPv4",
        }
    elif dns_leak_protection:
        dns_block = {
            "servers": list(dns_options.LEAK_PROTECTED_SYSTEM_UPSTREAMS),
            "queryStrategy": "UseIPv4",
        }

    return {
        "log": {
            "loglevel": log_level,
            # Also write ERROR-level log to file so users can grab xray.log
            # after a crash/disconnect for diagnostics — the in-app Logs
            # tab only holds the last ~500 lines in RAM.
            "error": str(paths.log_file()),
            # Privacy: explicitly disable access-log. Without "access:
            # none" xray would write a line per connection (timestamp +
            # source + destination IP/host) — that's a full browsing
            # history on disk. We don't want users to ever accidentally
            # leak that to anyone with disk access.
            "access": "none",
        },
        "stats": {},
        "policy": {
            # v2.1.6: per-level connection timeouts + a per-connection buffer
            # cap to bound xray's memory / handle growth under load (observed
            # ~2.3 GB / ~59k handles). Level 0 is the default level for every
            # inbound/outbound connection (we never assign explicit user
            # levels), so this applies to all traffic. connIdle reaps idle
            # connections — the main driver of handle accumulation when peers
            # abandon a flow without a clean close — and the half-close
            # timeouts free FIN-WAIT connections promptly. bufferSize caps how
            # much each connection may buffer. This is pure resource policy: it
            # does NOT touch routing, and the stats API below is untouched, so
            # the UI's traffic graph keeps working.
            "levels": {
                # Level 0 is the DEFAULT level for every inbound/outbound (we
                # never assign explicit user levels), so these apply to all
                # traffic — TCP and the UDP flows tun2socks forwards. v2.1.7:
                # connIdle tightened 300→180 to reap idle/abandoned sessions
                # sooner (the handle/flow accumulation that ballooned xray);
                # still well above Telegram/websocket keepalive intervals
                # (<60 s) and active calls (never idle), so nothing breaks.
                "0": {
                    "connIdle": 180,     # drop idle connections after 3 min
                    "handshake": 4,      # abort a stalled handshake after 4 s
                    "uplinkOnly": 2,     # half-closed (peer done sending): 2 s
                    "downlinkOnly": 5,   # half-closed (we done sending): 5 s
                    "bufferSize": 512,   # KB per connection — bounds buffering
                }
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True,
                "statsOutboundUplink": True,
                "statsOutboundDownlink": True,
            }
        },
        "api": {"tag": "api", "services": ["StatsService"]},
        # Conditional dns block — present only for non-system DNS option.
        # Using dict-spread to avoid emitting an empty key xray would
        # otherwise complain about.
        **({"dns": dns_block} if dns_block else {}),
        "inbounds": [
            {
                "tag": "http-in",
                "listen": listen_host,
                "port": listen_port,
                "protocol": "http",
                "settings": {"allowTransparent": False},
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls"],
                    "routeOnly": False,
                },
            },
            {
                "tag": "socks-in",
                "listen": listen_host,
                "port": listen_port + 1,
                "protocol": "socks",
                "settings": {"udp": True, "auth": "noauth"},
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls"],
                    "routeOnly": False,
                },
            },
            {
                "tag": "api-in",
                "listen": _stats.API_LISTEN_HOST,
                "port": _stats.API_LISTEN_PORT,
                "protocol": "dokodemo-door",
                "settings": {"address": _stats.API_LISTEN_HOST},
            },
        ],
        "outbounds": [
            proxy_outbound,
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
            # v1.16.8: dns-out present whenever leak-protection is on
            # (regardless of DNS option). Upstream IP comes from the
            # selected option if it has one (adguard/cloudflare/quad9),
            # else falls back to Cloudflare 1.1.1.1 — that's the case
            # for the "System" option with protection on.
            # Transport rides outbounds[0] (proxy/VPN), so ISP sees
            # encrypted bytes, not DNS content.
            *([{
                "tag": "dns-out",
                "protocol": "dns",
                "settings": {
                    "address": hijack_upstream,
                    "port": 53,
                    "network": "tcp,udp",
                },
            }] if dns_leak_protection else []),
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                # Route the API inbound to the API outbound BEFORE any
                # other matching, otherwise stats queries would try to go
                # out through the proxy and fail.
                {"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"},
                *rules,
            ],
        },
    }


def write_config(
    proxy: ProxyConfig,
    direct_domains: list[str],
    listen_host: str = DEFAULT_LISTEN_HOST,
    listen_port: int = DEFAULT_LISTEN_PORT,
    dns_option: str = "system",
    dns_leak_protection: bool = True,
    block_ads: bool = False,
    route_ru_direct: bool = False,
    hysteria_socks_port: Optional[int] = None,
) -> str:
    config = build_config(
        proxy, direct_domains, listen_host, listen_port,
        dns_option=dns_option,
        dns_leak_protection=dns_leak_protection,
        block_ads=block_ads,
        route_ru_direct=route_ru_direct,
        hysteria_socks_port=hysteria_socks_port,
    )
    # Runtime config carries the server UUID/password — write it atomically
    # with user-only perms and never log its contents.
    target = paths.write_secure_text(
        paths.runtime_config_file(),
        json.dumps(config, indent=2, ensure_ascii=False),
    )
    return str(target)
