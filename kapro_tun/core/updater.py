"""Check GitHub Releases for a newer KaproTUN version.

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

GITHUB_RELEASES_LATEST = "https://api.github.com/repos/fafnirov/KaproTUN/releases/latest"

# The release asset the in-app updater downloads. Must match the name the
# release workflow publishes — it is how we find the digest in the API
# response, and a rename would silently turn verification off.
SETUP_ASSET_NAME = "KaproTUN-Setup.exe"

# Bypass system proxy on update checks — same reason as the installers.
# Without this, a stale 127.0.0.1:2080 system-proxy entry from a crashed
# HTTP-mode session makes the updater self-perpetuate the bug: user
# can't update to a fix because the update mechanism itself fails.
_NO_PROXY = {"http": "", "https": ""}


@dataclass
class UpdateInfo:
    version: str   # remote release version, with leading "v" stripped
    tag: str       # original tag including "v" prefix (used as URL slug)
    url: str       # html_url to the release page
    notes: str     # release body markdown
    # SHA-256 of the Windows installer asset, as reported by GitHub itself.
    # GitHub computes this from the bytes it stores, so it is an independent
    # statement of what we published — which is what lets the installer be
    # fetched from the mirror and still be trustworthy. Empty when the API
    # omitted it; the updater treats that as "cannot verify" and refuses.
    setup_sha256: str = ""


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
        response = requests.get(GITHUB_RELEASES_LATEST, timeout=timeout,
                                proxies=_NO_PROXY)
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError):
        return None
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None
    # The digest travels with the version discovery, deliberately: the same
    # request that tells us an update exists also tells us what it must hash
    # to. There is no path where we learn about a release but cannot verify
    # its installer.
    setup_digest = ""
    for asset in data.get("assets") or []:
        if str(asset.get("name") or "") == SETUP_ASSET_NAME:
            setup_digest = str(asset.get("digest") or "").removeprefix("sha256:")
            break
    return UpdateInfo(
        version=tag.lstrip("v"),
        tag=tag,
        url=str(data.get("html_url") or ""),
        notes=str(data.get("body") or ""),
        setup_sha256=setup_digest,
    )
