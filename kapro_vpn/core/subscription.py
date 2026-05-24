"""Download and parse VPN subscription URLs.

Most providers (dns.army, BMV, AmneziaFree, etc.) hand out a single URL
that returns a base64-encoded list of share-URLs — one per line. This
module fetches that, decodes it, and walks each line through the share-URL
parser to produce ProxyConfig objects ready for storage.

Format detection:
- Try base64-decode first; if the result starts with a known scheme
  (vless://, vmess://, trojan://, ss://, hysteria2://) treat as the
  intended payload.
- Otherwise assume the response is already plain text and parse directly.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import requests

from .parser import ParseError, ProxyConfig, parse

SUPPORTED_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://",
                     "hysteria2://", "hy2://")


@dataclass
class SubscriptionResult:
    configs: list[ProxyConfig]
    errors: list[str]
    raw_lines: int  # how many candidate lines we tried to parse


def parse_subscription_body(body: str) -> list[str]:
    """Pull share-URLs out of a subscription response body.

    Some providers ship plain text, some ship base64. We try base64 first
    if the body looks line-noise-y (no obvious share-URL anywhere), and
    fall back to the raw text otherwise.
    """
    body = body.strip()
    if not body:
        return []

    candidates = [body]
    # If the body doesn't have an obvious scheme already, try base64-decode
    if not any(sch in body for sch in SUPPORTED_SCHEMES):
        try:
            # base64 fix-padding: append '=' until length % 4 == 0
            padded = body + "=" * ((-len(body)) % 4)
            decoded = base64.b64decode(padded, validate=False).decode(
                "utf-8", errors="replace",
            )
            if any(sch in decoded for sch in SUPPORTED_SCHEMES):
                candidates.insert(0, decoded)
        except Exception:
            pass

    for text in candidates:
        urls = [
            line.strip() for line in text.splitlines()
            if line.strip()
            and not line.strip().startswith("#")
            and any(line.strip().startswith(sch) for sch in SUPPORTED_SCHEMES)
        ]
        if urls:
            return urls
    return []


def import_subscription(url: str, timeout: tuple[float, float] = (10, 20)) -> SubscriptionResult:
    """Download a subscription and parse every contained share-URL.

    `url` should be the provider-supplied subscription URL.
    Raises requests.RequestException on network failure.
    """
    response = requests.get(url, timeout=timeout, headers={
        # Some providers gate access on a recognizable client UA
        "User-Agent": "ClashforWindows/0.20.39",
    })
    response.raise_for_status()
    body = response.text

    share_urls = parse_subscription_body(body)
    configs: list[ProxyConfig] = []
    errors: list[str] = []
    for share_url in share_urls:
        try:
            configs.append(parse(share_url))
        except ParseError as e:
            short = share_url[:60] + ("…" if len(share_url) > 60 else "")
            errors.append(f"{short} — {e}")
    return SubscriptionResult(
        configs=configs, errors=errors, raw_lines=len(share_urls),
    )
