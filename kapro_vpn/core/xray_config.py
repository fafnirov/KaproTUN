"""Generate Xray-core JSON configuration with split routing.

The converter re-parses the share URL stored on each ProxyConfig (rather than
re-using the sing-box-formatted outbound dict) so that Xray-specific fields
like xhttp transport, REALITY spiderX, etc. are preserved without forcing
the sing-box-format outbound to know about them.
"""
from __future__ import annotations

import base64
import json
from typing import Any
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

def proxy_to_xray_outbound(cfg: ProxyConfig) -> dict[str, Any]:
    """Convert a parsed ProxyConfig into an Xray-core outbound dict."""
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
        raise NotImplementedError(
            "Xray-core не поддерживает Hysteria2. Используй v2/hy2-совместимый клиент "
            "или жди добавления второго движка (sing-box) в KaproVPN."
        )
    raise ValueError(f"Unknown protocol scheme: {scheme}")


# ---- full config ---------------------------------------------------------

def build_config(
    proxy: ProxyConfig,
    direct_domains: list[str],
    listen_host: str = DEFAULT_LISTEN_HOST,
    listen_port: int = DEFAULT_LISTEN_PORT,
    log_level: str = "warning",
    dns_option: str = "system",
) -> dict[str, Any]:
    """Build a complete Xray-core client config with split routing.

    The proxy outbound is first in the outbounds list — Xray uses the first
    outbound as the default for non-matching traffic, so domains not in the
    direct list go through `proxy` automatically.

    dns_option (from core/dns_options.py) controls how DNS resolution
    happens. "system" — let the OS resolve everything, no xray-side dns
    block. For named options (adguard/cloudflare/quad9) we add a `dns`
    block with DoH servers AND a routing rule that forces those servers'
    plain IPs to "direct" (so DoH-over-443 from apps that bypass xray's
    own resolver still doesn't tunnel through the VPN).
    """
    proxy_outbound = proxy_to_xray_outbound(proxy)
    cleaned = sorted({d.strip().lower() for d in direct_domains if d.strip()})
    dns_opt = dns_options.get(dns_option)

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
        # DNS-leak hardening: queries to common public DNS resolvers
        # ALSO get force-routed direct, even if some other rule below
        # might match them. The controller already adds OS-level
        # host-routes for these via the real gateway, but if anything
        # slips through (e.g. an app doing DNS over TCP/853 which the
        # kernel routes differently), this rule makes sure xray drops
        # it to direct rather than tunnelling — keeping the DNS query
        # off our VPN provider's view of the user's browsing.
        {"type": "field", "outboundTag": "direct", "ip": [
            "1.1.1.1/32", "1.0.0.1/32",      # Cloudflare
            "8.8.8.8/32", "8.8.4.4/32",      # Google
            "9.9.9.9/32",                     # Quad9
            "77.88.8.8/32", "77.88.8.1/32",  # Yandex
            "77.88.8.88/32", "77.88.8.7/32", # Yandex safe/family
        ]},
        # And by port: anything to UDP 53 / TCP 53 should NEVER tunnel
        # unless it's already direct via the IP rule above. Catches DNS
        # to less-common resolvers without us hard-coding their IPs.
        {"type": "field", "outboundTag": "direct",
         "network": "udp", "port": "53"},
        {"type": "field", "outboundTag": "direct",
         "network": "tcp", "port": "53"},
    ]
    # User-chosen DNS service — its plain IPs should never tunnel through
    # the VPN (otherwise DoH-over-443 from apps that don't use xray's own
    # resolver takes an unnecessary VPN-server round-trip and the DNS
    # provider sees the VPN server's IP, not the user's). Skipped when
    # dns_option == "system" because bypass_ips is empty in that case.
    if dns_opt.bypass_ips:
        rules.append({
            "type": "field",
            "outboundTag": "direct",
            "ip": [f"{ip}/32" for ip in dns_opt.bypass_ips],
        })

    if domain_rules:
        rules.append({"type": "field", "domain": domain_rules, "outboundTag": "direct"})

    # API inbound for runtime stats (read by core/xray_stats.py via the
    # `xray api stats` CLI helper). Routed to the dedicated `api` outbound
    # rather than the proxy/direct ones.
    from . import xray_stats as _stats

    # DNS block — only when the user picked a named service. For "system"
    # we leave xray's default behavior (resolve via the OS), so this block
    # is omitted entirely. For DoH options, point xray at the encrypted
    # endpoint and force IPv4-only queries (we don't tunnel v6 anyway).
    dns_block: dict[str, Any] | None = None
    if dns_opt.doh_servers:
        dns_block = {
            "servers": list(dns_opt.doh_servers),
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
) -> str:
    config = build_config(
        proxy, direct_domains, listen_host, listen_port, dns_option=dns_option,
    )
    target = paths.runtime_config_file()
    target.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(target)
