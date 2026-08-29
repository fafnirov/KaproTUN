"""Parser for the `v2ray-json` subscription format.

Some panels (Remnawave, Marzban and friends) do not serve a list of share-URLs
at all — they return a JSON ARRAY of complete Xray client configurations, each
with its own `dns` / `routing` / `inbounds` / `outbounds` and a `remarks` field
holding the display name. Observed live when a provider switched formats and
KaproTUN silently imported zero servers: the payload was perfectly valid, we
just did not speak that dialect.

This module converts each entry into the same sing-box-shaped ProxyConfig the
share-URL parsers produce, so everything downstream (config generation, the
picker, ping, subscriptions) is unchanged.

Only the proxy outbound is taken from each entry. The rest of the Xray config —
its own routing rules, DNS and inbounds — is deliberately DISCARDED: KaproTUN
applies its own split-routing policy, and honouring a provider's embedded rules
would silently override the user's choices about what bypasses the tunnel.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .parser import ParseError, ProxyConfig

# Outbound protocols we can faithfully reproduce on sing-box. `freedom` and
# `blackhole` are Xray's direct/block pseudo-outbounds and are never servers.
_PROXY_PROTOCOLS = ("vless", "vmess", "trojan", "shadowsocks")


def looks_like_v2ray_json(body: str) -> bool:
    """Cheap sniff: a JSON array whose first element carries `outbounds`."""
    text = (body or "").lstrip()
    if not text.startswith("["):
        return False
    try:
        data = json.loads(text)
    except Exception:
        return False
    return (isinstance(data, list) and bool(data)
            and isinstance(data[0], dict) and "outbounds" in data[0])


def _tls_block(stream: dict, default_sni: str = "") -> Optional[dict]:
    """Xray streamSettings -> sing-box `tls` block (incl. REALITY / uTLS)."""
    security = str(stream.get("security") or "").lower()
    if security not in ("tls", "reality", "xtls"):
        return None
    reality = stream.get("realitySettings") or {}
    tls_set = stream.get("tlsSettings") or {}
    src = reality if security == "reality" else tls_set
    sni = str(src.get("serverName") or default_sni or "")
    tls: dict[str, Any] = {"enabled": True}
    if sni:
        tls["server_name"] = sni
    if src.get("allowInsecure"):
        tls["insecure"] = True
    alpn = src.get("alpn")
    if alpn:
        tls["alpn"] = list(alpn) if isinstance(alpn, list) else [str(alpn)]
    fingerprint = str(src.get("fingerprint") or "")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    if security == "reality":
        public_key = str(reality.get("publicKey") or "")
        if not public_key:
            raise ParseError("REALITY без publicKey")
        tls["reality"] = {"enabled": True, "public_key": public_key}
        short_id = str(reality.get("shortId") or "")
        if short_id:
            tls["reality"]["short_id"] = short_id
    return tls


def _transport_block(stream: dict) -> Optional[dict]:
    """Xray streamSettings -> sing-box `transport`. None for plain TCP."""
    network = str(stream.get("network") or "tcp").lower()
    if network in ("tcp", "raw", ""):
        return None
    if network == "ws":
        ws = stream.get("wsSettings") or {}
        out: dict[str, Any] = {"type": "ws"}
        if ws.get("path"):
            out["path"] = str(ws["path"])
        headers = ws.get("headers") or {}
        host = headers.get("Host") or headers.get("host")
        if host:
            out["headers"] = {"Host": str(host)}
        return out
    if network == "grpc":
        grpc = stream.get("grpcSettings") or {}
        out = {"type": "grpc"}
        if grpc.get("serviceName"):
            out["service_name"] = str(grpc["serviceName"])
        return out
    if network in ("h2", "http"):
        h2 = stream.get("httpSettings") or {}
        out = {"type": "http"}
        if h2.get("path"):
            out["path"] = str(h2["path"])
        if h2.get("host"):
            hosts = h2["host"]
            out["host"] = list(hosts) if isinstance(hosts, list) else [str(hosts)]
        return out
    if network == "httpupgrade":
        hu = stream.get("httpupgradeSettings") or {}
        out = {"type": "httpupgrade"}
        if hu.get("path"):
            out["path"] = str(hu["path"])
        if hu.get("host"):
            out["host"] = str(hu["host"])
        return out
    raise ParseError(f"транспорт «{network}» не поддерживается")


def _outbound_to_singbox(ob: dict) -> dict:
    """One Xray outbound -> a sing-box outbound dict (no tag)."""
    protocol = str(ob.get("protocol") or "").lower()
    settings = ob.get("settings") or {}
    stream = ob.get("streamSettings") or {}

    if protocol in ("vless", "vmess"):
        vnext = settings.get("vnext") or []
        if not vnext:
            raise ParseError(f"{protocol}: пустой vnext")
        node = vnext[0]
        users = node.get("users") or []
        if not users:
            raise ParseError(f"{protocol}: нет users")
        user = users[0]
        out: dict[str, Any] = {
            "type": protocol,
            "server": str(node.get("address") or ""),
            "server_port": int(node.get("port") or 0),
            "uuid": str(user.get("id") or ""),
        }
        if protocol == "vless":
            flow = str(user.get("flow") or "")
            if flow:
                out["flow"] = flow
        else:
            out["security"] = str(user.get("security") or "auto")
            if user.get("alterId") is not None:
                out["alter_id"] = int(user.get("alterId") or 0)
    elif protocol == "trojan":
        servers = settings.get("servers") or []
        if not servers:
            raise ParseError("trojan: пустой servers")
        node = servers[0]
        out = {
            "type": "trojan",
            "server": str(node.get("address") or ""),
            "server_port": int(node.get("port") or 0),
            "password": str(node.get("password") or ""),
        }
    elif protocol == "shadowsocks":
        servers = settings.get("servers") or []
        if not servers:
            raise ParseError("shadowsocks: пустой servers")
        node = servers[0]
        out = {
            "type": "shadowsocks",
            "server": str(node.get("address") or ""),
            "server_port": int(node.get("port") or 0),
            "method": str(node.get("method") or ""),
            "password": str(node.get("password") or ""),
        }
    else:
        raise ParseError(f"протокол «{protocol}» не поддерживается")

    if not out["server"] or not out["server_port"]:
        raise ParseError("нет адреса или порта сервера")

    tls = _tls_block(stream, default_sni=out["server"])
    if tls:
        out["tls"] = tls
    transport = _transport_block(stream)
    if transport:
        out["transport"] = transport
    return out


def parse_v2ray_json(body: str) -> tuple[list, list]:
    """Parse a v2ray-json subscription body.

    Returns (configs, errors). Never raises on a single bad entry — one broken
    server must not cost the user the whole subscription (the same rule the
    share-URL path follows).

    Entries are de-duplicated by (type, server, port): panels commonly ship an
    "auto / fastest" entry that repeats every server already listed separately,
    which would otherwise show up as a pile of duplicates in the picker.
    """
    configs: list = []
    errors: list = []
    seen: set = set()
    try:
        data = json.loads(body)
    except Exception as e:
        return [], [f"v2ray-json: невалидный JSON ({type(e).__name__})"]
    if not isinstance(data, list):
        return [], ["v2ray-json: ожидался массив конфигураций"]

    # Single-proxy entries first, balancer entries ("auto / fastest", which
    # repeat every server) last. Without this the balancer wins the dedupe and
    # a real server loses its own name — e.g. "Нидерланды" vanished behind
    # "Авто | Самый быстрый" because both start at the same address.
    def _proxy_count(entry) -> int:
        if not isinstance(entry, dict):
            return 0
        return len([o for o in (entry.get("outbounds") or [])
                    if isinstance(o, dict)
                    and str(o.get("protocol") or "").lower() in _PROXY_PROTOCOLS])

    ordered = sorted(enumerate(data),
                     key=lambda pair: (_proxy_count(pair[1]) > 1, pair[0]))
    for index, item in ordered:
        if not isinstance(item, dict):
            continue
        name = str(item.get("remarks") or item.get("tag") or f"server {index + 1}")
        proxies = [o for o in (item.get("outbounds") or [])
                   if isinstance(o, dict)
                   and str(o.get("protocol") or "").lower() in _PROXY_PROTOCOLS]
        if not proxies:
            continue
        # EVERY proxy outbound in the entry, not just the first: a balancer
        # usually repeats servers that are listed separately (those dedupe away
        # harmlessly), but it can also be the ONLY place a server appears, and
        # dropping it would silently cost the user that node.
        for proxy in proxies:
            try:
                outbound = _outbound_to_singbox(proxy)
            except ParseError as e:
                errors.append(f"{name} — {e}")
                continue
            except Exception as e:
                errors.append(f"{name} — {type(e).__name__}: {e}")
                continue
            key = (outbound["type"], outbound["server"], outbound["server_port"])
            if key in seen:
                continue
            seen.add(key)
            # A single-proxy entry is one server and `remarks` names it. Inside a
            # balancer the entry name describes the group, so the outbound's own
            # tag is the better label.
            label = name if len(proxies) == 1 else (
                str(proxy.get("tag") or "").strip() or name)
            configs.append(ProxyConfig(
                name=label,
                protocol=outbound["type"],
                raw_url="",
                outbound=outbound,
                network=str((proxy.get("streamSettings") or {}).get("network") or ""),
            ))
    return configs, errors
