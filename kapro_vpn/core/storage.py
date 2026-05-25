"""Persistent storage for configs, the direct-routing sites list, and settings.

Privacy: on Windows, configs.json is DPAPI-encrypted at rest (only the
current user account can read it). load_configs() transparently handles
both encrypted and legacy plaintext files — opening an old pre-1.8.0
file still works, and the next save flips it to encrypted form.

On mac/Linux configs stay plaintext (file permissions only — same as
~/.ssh/config). DPAPI replacement on those platforms is future work.

What's NOT encrypted: sites.json (just domain names — not secret), and
settings.json (mostly preferences, but subscription_url IS a secret —
treated as such; we move it OUT of settings.json into the encrypted
configs.json blob for users who care).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from . import paths, secrets_store
from .parser import ProxyConfig


def _read_configs_bytes() -> bytes:
    """Read raw bytes of configs.json. Empty bytes if file missing.

    Centralized here so we can put DPAPI decode logic in one place.
    """
    f = paths.configs_file()
    if not f.is_file():
        return b""
    raw = f.read_bytes()
    if secrets_store.looks_encrypted(raw):
        try:
            return secrets_store.decrypt(raw)
        except Exception:
            # Encrypted blob unreadable (different Windows user, key
            # rotation, etc.). Surface as "no configs" rather than
            # crashing the app at startup — user can re-import.
            return b""
    return raw  # legacy plaintext


def _write_configs_bytes(data: bytes) -> None:
    """Write data to configs.json, DPAPI-encrypted where supported.

    On encrypt failure (rare — DPAPI Windows API rejection), fall
    back to plaintext write rather than losing the user's configs.
    """
    f = paths.configs_file()
    if secrets_store.is_supported():
        try:
            data = secrets_store.encrypt(data)
        except Exception:
            pass  # silently fall back to plaintext
    f.write_bytes(data)


# --- saved proxy configs --------------------------------------------------

def load_configs() -> list[ProxyConfig]:
    raw_bytes = _read_configs_bytes()
    if not raw_bytes:
        return []
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
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
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    _write_configs_bytes(payload)


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
    "kill_switch": False,    # leave TUN up if xray dies (no leak via real ISP)
    "language": "auto",      # "ru" / "en" / "auto" (detect from QLocale.system())
    "subscription_auto_refresh": True,  # background re-fetch every 12h
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
