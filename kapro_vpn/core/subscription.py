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

DPI fallback:
- Many provider sites (gmailvpn.ru, getoutline.org mirrors, etc.) are
  blocked by Russian ISPs at the TLS layer — the TCP handshake completes
  but ClientHello gets RST'd before ServerHello. requests sees this as
  SSLEOFError / ConnectionResetError.
- When xray is already running locally, we can fetch the subscription
  *through* the active tunnel: route the HTTP request to xray's mixed
  inbound at 127.0.0.1:listen_port. Same trick the browser uses when
  the system proxy is set — DPI sees only the encrypted outbound xray
  stream and can't pattern-match the inner request.
"""
from __future__ import annotations

import base64
import socket
from dataclasses import dataclass
from typing import Optional

import requests

from .parser import ParseError, ProxyConfig, parse

SUPPORTED_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://",
                     "hysteria2://", "hy2://")


@dataclass
class SubscriptionResult:
    configs: list[ProxyConfig]
    errors: list[str]
    raw_lines: int  # how many candidate lines we tried to parse
    via_proxy: bool = False  # did we fall back to the local xray tunnel?


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


def _fetch(url: str, timeout: tuple[float, float],
           proxy_url: Optional[str] = None) -> str:
    """One requests.get call; optionally routed through proxy_url."""
    proxies = None
    if proxy_url:
        # xray's mixed inbound speaks both HTTP and SOCKS on the same port,
        # so a single http://127.0.0.1:port URL handles http+https requests.
        proxies = {"http": proxy_url, "https": proxy_url}
    response = requests.get(url, timeout=timeout, proxies=proxies, headers={
        # Some providers gate access on a recognizable client UA — Clash is
        # the de-facto standard the subscription-token endpoints sniff for.
        "User-Agent": "ClashforWindows/0.20.39",
    })
    response.raise_for_status()
    return response.text


def _looks_like_dpi_block(err: Exception) -> bool:
    """Heuristic: did this failure look like a TLS-layer block?

    Russian DPI typically RSTs the connection mid-ClientHello, surfacing
    as SSLEOFError or a generic ConnectionResetError. ConnectionError with
    "EOF" / "reset" / "aborted" in the message is the same thing wrapped
    one layer up by urllib3.
    """
    msg = str(err).lower()
    needles = (
        "unexpected_eof_while_reading",
        "ssleoferror",
        "connection reset",
        "connectionreseterror",
        "connection aborted",
        "remoteend",
        "ssl: ",  # broad — covers misc handshake failures
    )
    return any(n in msg for n in needles)


def _probe_local_proxy(host: str, port: int, timeout: float = 0.5) -> bool:
    """True if something is listening at host:port (i.e. xray is up)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def result_from_body(body: str, via_proxy: bool = False) -> SubscriptionResult:
    """Take a raw subscription response and turn it into a SubscriptionResult.

    Shared between the URL-fetch flow and the manual-paste fallback in the
    UI, so a body the user copied from their browser parses identically
    to one we downloaded ourselves.
    """
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
        via_proxy=via_proxy,
    )


def import_subscription(
    url: str,
    timeout: tuple[float, float] = (10, 20),
    proxy_url: Optional[str] = None,
) -> SubscriptionResult:
    """Download a subscription and parse every contained share-URL.

    `url` should be the provider-supplied subscription URL.
    `proxy_url`, if set, routes the fetch through it (e.g.
    "http://127.0.0.1:2080" to go via the active xray tunnel).
    Raises requests.RequestException on network failure.
    """
    body = _fetch(url, timeout, proxy_url=proxy_url)
    return result_from_body(body, via_proxy=bool(proxy_url))


def import_with_dpi_fallback(
    url: str,
    local_proxy_host: str = "127.0.0.1",
    local_proxy_port: int = 2080,
    timeout: tuple[float, float] = (10, 20),
) -> SubscriptionResult:
    """Fetch a subscription, automatically retrying via the local tunnel
    if the direct attempt looks DPI-blocked.

    Flow:
      1. Try a normal direct fetch first — fast and avoids loading the
         tunnel for no reason.
      2. On TLS-handshake-EOF / connection-reset (the Russian DPI
         signature), probe 127.0.0.1:listen_port. If xray is up, retry
         through it. The result's `via_proxy` flag tells the caller a
         fallback happened so they can surface it in the UI.
      3. Otherwise re-raise the original error untouched.
    """
    try:
        return import_subscription(url, timeout=timeout)
    except requests.RequestException as direct_err:
        if not _looks_like_dpi_block(direct_err):
            raise
        if not _probe_local_proxy(local_proxy_host, local_proxy_port):
            # No active tunnel to fall back through — surface the
            # original DPI error so the caller's "connect first" hint
            # makes sense to the user.
            raise
        proxy_url = f"http://{local_proxy_host}:{local_proxy_port}"
        # Let the proxied attempt's own exception escape unwrapped — if
        # it ALSO fails, that's more interesting than the DPI error we
        # already explained away.
        return import_subscription(url, timeout=timeout, proxy_url=proxy_url)
