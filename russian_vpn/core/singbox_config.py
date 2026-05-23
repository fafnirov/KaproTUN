"""Generate sing-box JSON configuration with split routing."""
from __future__ import annotations

import json
from typing import Any

from . import paths
from .parser import ProxyConfig

DEFAULT_LISTEN_PORT = 2080
DEFAULT_LISTEN_HOST = "127.0.0.1"


def build_config(
    proxy: ProxyConfig,
    direct_domains: list[str],
    listen_host: str = DEFAULT_LISTEN_HOST,
    listen_port: int = DEFAULT_LISTEN_PORT,
    log_level: str = "info",
) -> dict[str, Any]:
    """Build a complete sing-box client config.

    Domains in `direct_domains` route via the system's real network (Russian IP);
    everything else routes through the proxy outbound built from `proxy`.
    """
    proxy_outbound = dict(proxy.outbound)
    proxy_outbound["tag"] = "proxy"

    cleaned_domains = sorted({d.strip().lower() for d in direct_domains if d.strip()})

    config: dict[str, Any] = {
        "log": {
            "level": log_level,
            "timestamp": True,
            "output": str(paths.log_file()).replace("\\", "/"),
        },
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": listen_host,
                "listen_port": listen_port,
            }
        ],
        "outbounds": [
            proxy_outbound,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "auto_detect_interface": True,
            "final": "proxy",
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "action": "route", "outbound": "direct"},
            ],
        },
    }

    if cleaned_domains:
        config["route"]["rules"].append(
            {"domain_suffix": cleaned_domains, "action": "route", "outbound": "direct"}
        )

    return config


def write_config(
    proxy: ProxyConfig,
    direct_domains: list[str],
    listen_host: str = DEFAULT_LISTEN_HOST,
    listen_port: int = DEFAULT_LISTEN_PORT,
) -> str:
    """Build the config and write it to the runtime path. Returns the path."""
    config = build_config(proxy, direct_domains, listen_host, listen_port)
    target = paths.runtime_config_file()
    target.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(target)
