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
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from . import paths, secrets_store
from .parser import ProxyConfig

_log = logging.getLogger("kaprotun.storage")


class SecretsError(RuntimeError):
    """Raised when a secret could not be encrypted on a platform that
    SUPPORTS encryption. We refuse to silently fall back to plaintext for
    secrets there — the value stays in memory only and the caller surfaces
    the failure (via last_error()) rather than leaking UUIDs/passwords or a
    paid subscription link to disk in the clear.
    """


_last_error: Optional[str] = None


def last_error() -> Optional[str]:
    """Most recent secret-write failure, for diagnostics (Settings/Logs).
    None when the last secret write succeeded (or none has happened)."""
    return _last_error


def _record_error(msg: str) -> None:
    global _last_error
    _last_error = msg
    _log.warning("storage/secrets: %s", msg)


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


def _read_encrypted(path: Path) -> bytes:
    """Read an encrypted-at-rest file (configs / secrets). Transparently
    decrypts; returns empty bytes if the file is missing or undecryptable.

    Centralizes the decrypt-or-legacy-plaintext logic for every secret file.
    """
    if not path.is_file():
        return b""
    raw = path.read_bytes()
    if secrets_store.looks_encrypted(raw):
        try:
            return secrets_store.decrypt(raw)
        except Exception as e:
            # Blob unreadable (different Windows user, lost DEK, tamper).
            # Surface as "empty" rather than crashing at startup — the user
            # re-imports. Not a privacy regression: we simply can't read
            # ciphertext that isn't ours. Record it so a "my servers
            # vanished" report is diagnosable instead of silent.
            _record_error(f"could not decrypt {path.name}: {type(e).__name__}: {e}")
            return b""
    return raw  # legacy plaintext


def _write_encrypted(path: Path, data: bytes) -> None:
    """Write `data` to `path`, encrypted-at-rest where the platform supports
    it, atomically and with user-only file permissions.

    SECURITY — no silent plaintext: if the platform CAN encrypt
    (secrets_store.is_supported() is True) but encryption fails, we do NOT
    quietly write the secret in the clear (that would be an invisible
    privacy regression). We record the reason and raise SecretsError so the
    caller can surface it; the secret stays in memory only.

    Plaintext is written ONLY where the platform genuinely has no keystore
    (is_supported() is False — e.g. headless Linux without Secret Service),
    and that path is explicit and logged. File permissions are the
    protection there, same as ``~/.ssh/config``.
    """
    if secrets_store.is_supported():
        try:
            data = secrets_store.encrypt(data)
        except Exception as e:
            msg = (f"encryption failed on a keystore-capable platform "
                   f"({sys.platform}): {type(e).__name__}: {e} — refusing to "
                   f"write {path.name} in plaintext")
            _record_error(msg)
            raise SecretsError(msg) from e
    else:
        _log.info("secrets_store unsupported on %s — %s written in plaintext "
                  "(file-permission protected)", sys.platform, path.name)
    _atomic_write_bytes(path, data)
    if not paths.harden_file_perms(path):
        _record_error(f"could not chmod 0600 {path.name} (defence-in-depth only)")


def _read_configs_bytes() -> bytes:
    """Raw decrypted bytes of configs.json (empty if missing)."""
    return _read_encrypted(paths.configs_file())


def _write_configs_bytes(data: bytes) -> None:
    """Write configs.json encrypted-at-rest. Raises SecretsError if the
    platform supports encryption but it failed (never plaintext-leaks)."""
    _write_encrypted(paths.configs_file(), data)


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


def save_configs(configs: list[ProxyConfig]) -> bool:
    """Persist the server list, encrypted-at-rest. Returns True on success.

    On a keystore-capable platform where encryption fails, returns False and
    records last_error() — the configs (UUIDs/passwords) are NOT written in
    plaintext, and the UI is not crashed. In-memory configs survive; the user
    can retry. The genuinely-unsupported-platform path still writes (plaintext
    by documented design) and returns True.
    """
    data = [asdict(c) for c in configs]
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        _write_configs_bytes(payload)
        return True
    except SecretsError as e:
        _record_error(f"configs not saved (encryption failure, not leaked): {e}")
        return False


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


# --- subscription secrets (encrypted, kept out of settings.json) ----------

# These three were historically stored in settings.json in plaintext. A
# subscription URL is a bearer credential (anyone with it pulls your paid
# servers), so they now live in the encrypted secrets.json blob. Runtime
# code still reads them via settings["subscription_url"] etc.: load_settings
# overlays them back into the settings dict, and save_settings strips them
# out of settings.json and writes them to the blob.
_SUBSCRIPTION_SECRET_KEYS = (
    "subscription_url", "subscription_urls", "subscription_userinfo",
)


def load_subscription_secrets() -> dict[str, Any]:
    """Decrypt and return the subscription-secrets blob ({} if absent)."""
    raw = _read_encrypted(paths.secrets_file())
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_subscription_secrets(secrets: dict[str, Any]) -> None:
    """Persist subscription secrets to the encrypted blob (only the known
    secret keys). Raises SecretsError if encryption is supported but failed —
    the secret is then NOT written anywhere in plaintext."""
    payload = {k: secrets.get(k) for k in _SUBSCRIPTION_SECRET_KEYS}
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    _write_encrypted(paths.secrets_file(), data)


# --- app settings ---------------------------------------------------------

DEFAULT_SETTINGS: dict[str, Any] = {
    "listen_host": "127.0.0.1",
    "listen_port": 2080,
    "last_config_name": "",
    "auto_set_system_proxy": True,
    "mode": "http",  # "http" (browser-only) or "tun" (system-wide, needs admin)
    "autoconnect_on_launch": False,
    "subscription_url": "",  # last imported subscription, for one-click re-sync
    "subscription_urls": [],  # every distinct subscription URL imported — "Обновить" re-fetches them all
    "subscription_userinfo": None,  # last seen Subscription-Userinfo (traffic/expiry) as dict, or None
    "kill_switch": False,    # leave TUN up if xray dies (no leak via real ISP)
    "language": "auto",      # "ru" / "en" / "auto" (detect from QLocale.system())
    "subscription_auto_refresh": True,  # background re-fetch every 12h
    "dns_option": "system",  # see core/dns_options.py — system|adguard|cloudflare|quad9
    "public_ip_probe": True,  # fetch & show "Ваш IP: X (страна)" after connect
    "ipv6_leak_protection": True,  # block global-unicast IPv6 outbound in TUN mode
    "webrtc_leak_protection": True,  # block STUN UDP (3478/5349/19302/19305-19308) so browsers can't leak real IP via WebRTC
    "dns_leak_protection": True,  # hijack :53 to VPN-tunneled DoH/upstream + silence physical-NIC DNS so ISP can't see queries
    "hysteria_auto_bandwidth": True,  # auto-measure link speed for hy2 brutal CC (no manual entry). v1.20.0
    "hysteria_up_mbps": 0,    # uplink Mbps for hy2 brutal CC — auto-measured (auto mode) or manual; 0 = BBR
    "hysteria_down_mbps": 0,  # downlink Mbps for hy2 brutal CC — auto-measured (auto mode) or manual; 0 = BBR
    "block_ads": False,  # drop geosite:category-ads-all at the xray routing layer (any DNS) — v1.19.0
    "route_ru_direct": False,  # route all geoip:ru traffic direct (bypass VPN), not just the curated domain list — v1.19.0
    "performance_preset": "balanced",  # v2.1.6: tun2socks TCP buffer ceiling — economy(512k)/balanced(1m, default)/speed(4m). Caps per-flow memory; default is NOT the old 4m blow-up
    "theme": "auto",  # "auto" (follow OS) / "dark" / "light" — see gui/styles.py
    "window_size": [480, 870],  # [w, h] — restored on launch (advanced resizable mode only)
    "allow_window_resize": False,  # v2.0.3: opt-in. False = fixed-size window (no mouse resize, no drift); True = resizable + edge handles
    "window_size_preset": "auto",  # v2.1.0: "auto" (compact on low screens) | "standard" | "compact"
}


def _read_settings_file() -> dict[str, Any]:
    """Read + parse the on-disk settings.json, or {} if absent/corrupt.

    settings.json is the very first file read at launch (main.py ->
    i18n.init_from_settings). A corrupted byte here would crash before the
    window ever opens — and a startup crash means the in-app auto-updater
    never runs. So a parse error falls back to defaults, never raises.
    """
    f = paths.settings_file()
    if not f.is_file():
        return {}
    try:
        parsed = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_settings() -> dict[str, Any]:
    raw_data = _read_settings_file()
    merged = dict(DEFAULT_SETTINGS)
    merged.update(raw_data)

    # Subscription secrets live in the encrypted blob, not settings.json.
    # Overlay them so runtime code reading settings["subscription_url"] etc.
    # is unchanged. The blob is authoritative whenever it holds a value.
    secrets = load_subscription_secrets()
    for k in _SUBSCRIPTION_SECRET_KEYS:
        v = secrets.get(k)
        if v not in (None, "", []):
            merged[k] = v

    # One-time migration: if the legacy plaintext fields are still physically
    # present in settings.json, move them into the encrypted blob and strip
    # the file. Best-effort — if it can't persist now it retries on the next
    # save; `merged` already carries the values for this session.
    if any(k in raw_data for k in _SUBSCRIPTION_SECRET_KEYS):
        try:
            save_settings(merged)
        except Exception as e:  # never let migration crash startup
            _record_error(f"subscription-secret migration deferred: {e}")
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    # Subscription secrets go to the encrypted blob, NEVER to settings.json
    # (where they'd sit as a plaintext bearer credential).
    secrets_persisted = True
    try:
        save_subscription_secrets(settings)
    except SecretsError as e:
        secrets_persisted = False
        _record_error(f"subscription secrets not persisted this save: {e}")

    clean = {k: v for k, v in settings.items()
             if k not in _SUBSCRIPTION_SECRET_KEYS}

    if not secrets_persisted:
        # Encryption is supported but failed RIGHT NOW. Two rules, both to
        # avoid data loss WITHOUT introducing new plaintext:
        #   1. If legacy plaintext secret fields are ALREADY present in the
        #      on-disk settings.json (un-migrated), keep them there verbatim —
        #      a transient DPAPI/keystore hiccup must NOT delete the user's
        #      subscription URL. The migration is genuinely deferred to a later
        #      save (load_settings retries it).
        #   2. Secret keys that were NOT already on disk are left out — we never
        #      write a fresh plaintext secret to settings.json. A brand-new
        #      secret that only lived in memory simply isn't persisted this
        #      save; last_error() (recorded above) explains why.
        existing = _read_settings_file()
        for k in _SUBSCRIPTION_SECRET_KEYS:
            if k in existing:
                clean[k] = existing[k]

    _atomic_write_bytes(
        paths.settings_file(),
        json.dumps(clean, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    paths.harden_file_perms(paths.settings_file())
