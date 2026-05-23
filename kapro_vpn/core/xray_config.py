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

from . import paths
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
) -> dict[str, Any]:
    """Build a complete Xray-core client config with split routing.

    The proxy outbound is first in the outbounds list — Xray uses the first
    outbound as the default for non-matching traffic, so domains not in the
    direct list go through `proxy` automatically.
    """
    proxy_outbound = proxy_to_xray_outbound(proxy)
    cleaned = sorted({d.strip().lower() for d in direct_domains if d.strip()})

    # `domain:foo.bar` in Xray matches foo.bar AND any *.foo.bar
    domain_rules = [f"domain:{d}" for d in cleaned]

    rules: list[dict[str, Any]] = [
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
    ]
    if domain_rules:
        rules.append({"type": "field", "domain": domain_rules, "outboundTag": "direct"})

    return {
        "log": {"loglevel": log_level},
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
        ],
        "outbounds": [
            proxy_outbound,
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": rules,
        },
    }


def write_config(
    proxy: ProxyConfig,
    direct_domains: list[str],
    listen_host: str = DEFAULT_LISTEN_HOST,
    listen_port: int = DEFAULT_LISTEN_PORT,
) -> str:
    config = build_config(proxy, direct_domains, listen_host, listen_port)
    target = paths.runtime_config_file()
    target.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(target)
