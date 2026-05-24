"""Persistent storage for configs, the direct-routing sites list, and settings."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from . import paths
from .parser import ProxyConfig


# --- saved proxy configs --------------------------------------------------

def load_configs() -> list[ProxyConfig]:
    f = paths.configs_file()
    if not f.is_file():
        return []
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out: list[ProxyConfig] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            out.append(ProxyConfig(
                name=str(item["name"]),
                protocol=str(item["protocol"]),
                raw_url=str(item["raw_url"]),
                outbound=dict(item.get("outbound", {})),
            ))
        except (KeyError, TypeError):
            continue
    return out


def save_configs(configs: list[ProxyConfig]) -> None:
    data = [asdict(c) for c in configs]
    paths.configs_file().write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# --- direct-routing site list ---------------------------------------------

def load_sites() -> list[str]:
    user_file = paths.sites_file()
    if user_file.is_file():
        source = user_file
    else:
        source = paths.bundled_default_sites()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return []
    sites = data.get("sites", []) if isinstance(data, dict) else data
    return [str(s).strip().lower() for s in sites if str(s).strip()]


def save_sites(sites: list[str]) -> None:
    cleaned = sorted({s.strip().lower() for s in sites if s.strip()})
    paths.sites_file().write_text(
        json.dumps({"sites": cleaned}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def reset_sites_to_default() -> list[str]:
    """Copy the bundled default list into the user file. Returns the list."""
    default_data = json.loads(paths.bundled_default_sites().read_text(encoding="utf-8"))
    sites = default_data.get("sites", [])
    save_sites(sites)
    return sites


# --- app settings ---------------------------------------------------------

DEFAULT_SETTINGS: dict[str, Any] = {
    "listen_host": "127.0.0.1",
    "listen_port": 2080,
    "last_config_name": "",
    "auto_set_system_proxy": True,
    "mode": "http",  # "http" (browser-only) or "tun" (system-wide, needs admin)
    "autoconnect_on_launch": False,
    "subscription_url": "",  # last imported subscription, for one-click re-sync
}


def load_settings() -> dict[str, Any]:
    f = paths.settings_file()
    if not f.is_file():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data if isinstance(data, dict) else {})
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    paths.settings_file().write_text(
        json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
