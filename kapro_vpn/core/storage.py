"""Persistent storage for configs, the direct-routing sites list, and settings.

Privacy: on Windows, configs.json is DPAPI-encrypted at rest (only the
current user account can read it). load_configs() transparently handles
both encrypted and legacy plaintext files — opening an old pre-1.8.0
file still works, and the next save flips it to encrypted form.

On macOS/Linux (v1.16.12+) configs are AES-256-GCM encrypted with a key
held in the OS keystore (Keychain / Secret Service) — see secrets_store.
Where no keystore is available (headless Linux) they fall back to
plaintext, protected by file permissions only, same as ~/.ssh/config.

What's NOT encrypted: sites.json (just domain names — not secret), and
settings.json (mostly preferences, but subscription_url IS a secret —
treated as such; we move it OUT of settings.json into the encrypted
configs.json blob for users who care).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import paths, secrets_store
from .parser import ProxyConfig


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically: write a sibling .tmp, then
    os.replace it over the target (atomic on the same filesystem on both
    Windows and POSIX).

    A crash or power loss mid-write leaves the original file intact rather
    than a truncated/half-written one — which is exactly the corruption
    that produced the v1.16.11 startup crash (a stray byte read back as
    invalid UTF-8). On any failure the partial .tmp is removed and the
    error re-raised, so the original is never replaced by garbage.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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
    _atomic_write_bytes(f, data)


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
    except (json.JSONDecodeError, FileNotFoundError, UnicodeDecodeError):
        # UnicodeDecodeError: a stray non-utf8 byte (partial write, AV
        # quarantine restore, disk corruption) must not crash startup —
        # fall back to "no custom sites" like load_configs does.
        return []
    sites = data.get("sites", []) if isinstance(data, dict) else data
    return [str(s).strip().lower() for s in sites if str(s).strip()]


def save_sites(sites: list[str]) -> None:
    cleaned = sorted({s.strip().lower() for s in sites if s.strip()})
    _atomic_write_bytes(
        paths.sites_file(),
        json.dumps({"sites": cleaned}, indent=2, ensure_ascii=False).encode("utf-8"),
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
    "dns_option": "system",  # see core/dns_options.py — system|adguard|cloudflare|quad9
    "public_ip_probe": True,  # fetch & show "Ваш IP: X (страна)" after connect
    "ipv6_leak_protection": True,  # block global-unicast IPv6 outbound in TUN mode
    "webrtc_leak_protection": True,  # block STUN UDP (3478/5349/19302/19305-19308) so browsers can't leak real IP via WebRTC
    "dns_leak_protection": True,  # hijack :53 to VPN-tunneled DoH/upstream + silence physical-NIC DNS so ISP can't see queries
    "theme": "auto",  # "auto" (follow OS) / "dark" / "light" — see gui/styles.py
    "window_size": [480, 870],  # [w, h] — restored on launch, saved on close
}


def load_settings() -> dict[str, Any]:
    f = paths.settings_file()
    if not f.is_file():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # UnicodeDecodeError: settings.json is the very first file read at
        # launch (main.py -> i18n.init_from_settings). A corrupted byte
        # here would crash before the window ever opens — and a startup
        # crash means the in-app auto-updater never runs, leaving the user
        # permanently stuck. Fall back to defaults instead.
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data if isinstance(data, dict) else {})
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    _atomic_write_bytes(
        paths.settings_file(),
        json.dumps(settings, indent=2, ensure_ascii=False).encode("utf-8"),
    )
