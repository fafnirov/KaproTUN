"""Check GitHub Releases for a newer KaproVPN version.

Read-only: we tell the user a new release exists and open the GitHub
release page in their browser when they ask. Actual binary replacement
will land later once we ship a PyInstaller-built .exe — replacing
loose .py files at runtime is fragile.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from .. import __version__

GITHUB_RELEASES_LATEST = "https://api.github.com/repos/fafnirov/KaproVPN/releases/latest"


@dataclass
class UpdateInfo:
    version: str   # remote release version, with leading "v" stripped
    tag: str       # original tag including "v" prefix (used as URL slug)
    url: str       # html_url to the release page
    notes: str     # release body markdown


def _parse_version(s: str) -> tuple[int, ...]:
    """Best-effort SemVer-ish tuple, "0.1.0" → (0, 1, 0). Bad chars ignored."""
    parts = []
    for chunk in s.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(remote: str, local: str = __version__) -> bool:
    """True if remote is a higher semver than local."""
    return _parse_version(remote) > _parse_version(local)


def latest_release(timeout: tuple[float, float] = (5, 10)) -> Optional[UpdateInfo]:
    """Query GitHub for the latest release. Returns None on any failure.

    Caller checks `is_newer(info.version)` separately so the same
    UpdateInfo can also drive a "you're on the latest version" display.
    """
    try:
        response = requests.get(GITHUB_RELEASES_LATEST, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError):
        return None
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None
    return UpdateInfo(
        version=tag.lstrip("v"),
        tag=tag,
        url=str(data.get("html_url") or ""),
        notes=str(data.get("body") or ""),
    )
