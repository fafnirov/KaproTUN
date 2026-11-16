"""Small, UI-independent helpers for pinned servers and subscription summaries."""
from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import urlsplit

from . import storage
from .subscription import endpoint_key, source_id


def favorite_key(config) -> str:
    # A rename must not unpin a server; never persist credentials in preferences.
    identity = (config.source, endpoint_key(config))
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()


def reconcile_favorites(keys, before, after) -> list[str]:
    result = list(dict.fromkeys(keys))
    for previous in before:
        old = favorite_key(previous)
        if old not in result:
            continue
        current = next((c for c in after if favorite_key(c) == old), None)
        if current is None:
            current = next((c for c in after if (c.source, c.name) ==
                            (previous.source, previous.name)), None)
        if current is not None:
            result[result.index(old)] = favorite_key(current)
    valid = {favorite_key(c) for c in after}
    return list(dict.fromkeys(k for k in result if k in valid))[:3]


def subscription_delta(before, after, fetched, source: str) -> dict:
    old = [c for c in before if c.source == source]
    new = [c for c in after if c.source == source]
    by_endpoint = {endpoint_key(c): c for c in old}
    by_name = {c.name: c for c in old}
    added = changed = 0
    for cfg in new:
        prior = by_endpoint.get(endpoint_key(cfg)) or by_name.get(cfg.name)
        if prior is None:
            added += 1
        elif (cfg.name, cfg.protocol, cfg.outbound) != (prior.name, prior.protocol, prior.outbound):
            changed += 1
    received = [c for c in fetched if c.source == source]
    names = {c.name for c in received}
    endpoints = {endpoint_key(c) for c in received}
    missing = sum(p.name not in names and endpoint_key(p) not in endpoints for p in old)
    return {"added": added, "changed": changed, "missing": missing}


def remember_subscription(url, result, before, after) -> None:
    """Store metadata per provider, without displaying its bearer URL."""
    sid = source_id(url)
    statuses = dict(storage.load_settings().get("subscription_status") or {})
    statuses[sid] = {
        "host": urlsplit(url).hostname or "",
        "updated": int(time.time()),
        "userinfo": result.userinfo.to_dict() if result.userinfo is not None else None,
        **subscription_delta(before, after, result.configs, sid),
    }
    storage.update_settings({"subscription_status": statuses})
