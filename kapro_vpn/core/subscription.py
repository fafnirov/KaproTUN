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
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

from .. import __version__
from .parser import ParseError, ProxyConfig, parse

# Self-identifying User-Agent so subscription providers have one obvious
# string to whitelist or block. Format follows the de-facto convention
# (`Name/Version (Platform; +URL)`) used by v2rayN, NekoBox, Streisand
# etc., so server-side allowlists that pattern-match "<Name>/" work out
# of the box.
USER_AGENT = f"KaproVPN/{__version__} (Windows; +https://github.com/fafnirov/KaproVPN)"

SUPPORTED_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://",
                     "hysteria2://", "hy2://")


def _human_bytes(n: int) -> str:
    """Compact byte size: 95.2 ГБ / 940 МБ / 12 КБ."""
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ")
    val = float(max(0, n))
    for u in units:
        if val < 1024.0 or u == units[-1]:
            return f"{int(val)} {u}" if u == "Б" else f"{val:.1f} {u}"
        val /= 1024.0
    return f"{n} Б"


@dataclass
class SubscriptionInfo:
    """Parsed `Subscription-Userinfo` header (de-facto Clash/v2ray standard):
    `upload=…; download=…; total=…; expire=…` — byte counters + a Unix expiry
    epoch. total <= 0 means unlimited; expire <= 0 means no expiry.
    """
    upload: int = 0
    download: int = 0
    total: int = 0
    expire: int = 0

    @property
    def used(self) -> int:
        return max(0, self.upload + self.download)

    @property
    def remaining(self) -> Optional[int]:
        if self.total <= 0:
            return None  # unlimited
        return max(0, self.total - self.used)

    def expire_date(self) -> Optional[datetime]:
        if self.expire <= 0:
            return None
        try:
            return datetime.fromtimestamp(self.expire)
        except (OverflowError, OSError, ValueError):
            return None

    def is_expired(self) -> bool:
        return 0 < self.expire < int(time.time())

    def summary(self) -> str:
        """One-line human status, or '' if the header carried nothing useful."""
        parts: list[str] = []
        if self.total > 0:
            parts.append(
                f"осталось {_human_bytes(self.remaining or 0)} "
                f"из {_human_bytes(self.total)}"
            )
        elif self.used > 0:
            parts.append(f"использовано {_human_bytes(self.used)}")
        d = self.expire_date()
        if d is not None:
            parts.append(
                f"истекла {d:%d.%m.%Y}" if self.is_expired() else f"до {d:%d.%m.%Y}"
            )
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return {"upload": self.upload, "download": self.download,
                "total": self.total, "expire": self.expire}

    @classmethod
    def from_dict(cls, d: dict) -> "SubscriptionInfo":
        return cls(
            upload=int(d.get("upload", 0)),
            download=int(d.get("download", 0)),
            total=int(d.get("total", 0)),
            expire=int(d.get("expire", 0)),
        )


def parse_userinfo(header: str) -> Optional[SubscriptionInfo]:
    """Parse a `Subscription-Userinfo` header value. Returns None if the
    header is absent or carries no recognisable numeric fields."""
    if not header:
        return None
    fields: dict[str, int] = {}
    for part in header.split(";"):
        key, sep, val = part.partition("=")
        if not sep:
            continue
        try:
            fields[key.strip().lower()] = int(float(val.strip()))
        except ValueError:
            continue
    if not fields:
        return None
    return SubscriptionInfo(
        upload=fields.get("upload", 0),
        download=fields.get("download", 0),
        total=fields.get("total", 0),
        expire=fields.get("expire", 0),
    )


@dataclass
class SubscriptionResult:
    configs: list[ProxyConfig]
    errors: list[str]
    raw_lines: int  # how many candidate lines we tried to parse
    via_proxy: bool = False  # did we fall back to the local xray tunnel?
    # Remaining-traffic / expiry from the provider's Subscription-Userinfo
    # header (None if not sent or for the manual-paste path).
    userinfo: Optional[SubscriptionInfo] = None
    # Names of configs that parsed fine but are provider "stubs" (e.g.
    # gmailvpn's `vless://…@0.0.0.0:1 #App not supported`) — filtered out
    # of `configs` so a dead placeholder is never imported as a server.
    placeholders: list[str] = field(default_factory=list)


@dataclass
class FetchError:
    """Structured classification of a subscription-fetch failure, so the
    UI can show an accurate cause instead of always blaming REALITY/DPI."""
    category: str        # not_found | auth | server | timeout | dpi | conn | unknown
    raw: str             # technical "TypeName: message" for the details line
    title: str           # short human-readable cause
    detail: str          # what the user should do
    suggest_manual: bool  # whether browser-copy-paste could plausibly help


# Hosts that mean "this isn't a real server" — providers hand these out as
# placeholders instead of a 404 when they won't serve a given client/plan.
PLACEHOLDER_HOSTS = {"", "0", "0.0.0.0", "127.0.0.1", "::", "::1", "localhost"}


def is_placeholder_config(cfg: ProxyConfig) -> bool:
    """True if a parsed config is a provider stub rather than a usable server.

    Catches the gmailvpn-style `vless://…@0.0.0.0:1 #App not supported`
    dummy: importing it would give the user a dead server with no hint why.
    """
    host = str(cfg.outbound.get("server", "")).strip().lower()
    if host in PLACEHOLDER_HOSTS:
        return True
    name = (cfg.name or "").lower()
    if "not supported" in name or "unsupported" in name:
        return True
    port = cfg.outbound.get("server_port", cfg.outbound.get("port"))
    try:
        if port is not None and int(port) <= 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def classify_fetch_error(exc: Exception) -> FetchError:
    """Map a fetch exception to an accurate, actionable message.

    The big win over the old "always REALITY/whitelist" hint: an HTTP
    status error means the server *answered* — it's not a TLS/DPI block,
    and manual browser-paste won't conjure a subscription that 404s.
    """
    raw = f"{type(exc).__name__}: {exc}"
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else None
    if status is not None:
        if status in (404, 410):
            return FetchError(
                "not_found", raw,
                f"Подписка не найдена (HTTP {status}).",
                "Ссылка неверная или устарела — ручная вставка не поможет. "
                "Запроси у провайдера актуальную ссылку.",
                suggest_manual=False,
            )
        if status in (401, 403):
            return FetchError(
                "auth", raw,
                f"Доступ запрещён (HTTP {status}).",
                "Подписка не активна / требует авторизации, либо доступ "
                "ограничен по IP. Если URL открывается в браузере — "
                "скопируй ответ и вставь вручную.",
                suggest_manual=True,
            )
        return FetchError(
            "server", raw,
            f"Сервер провайдера ответил ошибкой (HTTP {status}).",
            "Это на стороне провайдера. Попробуй позже или запроси новую ссылку.",
            suggest_manual=False,
        )
    if _looks_like_dpi_block(exc):
        return FetchError(
            "dpi", raw,
            "Похоже на DPI-блокировку или REALITY / IP-белый список.",
            "Открой URL в браузере, скопируй ответ целиком и вставь вручную. "
            "Или подключись к любому серверу — KaproVPN попробует ещё раз "
            "через туннель.",
            suggest_manual=True,
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return FetchError(
            "timeout", raw,
            "Таймаут — сервер не ответил.",
            "Проверь интернет и ссылку. Если сайт открывается в браузере — "
            "вставь ответ вручную.",
            suggest_manual=True,
        )
    if isinstance(exc, requests.exceptions.ConnectionError):
        return FetchError(
            "conn", raw,
            "Не удалось соединиться с сервером провайдера.",
            "Проверь ссылку и интернет. Если URL открывается в браузере — "
            "вставь ответ вручную.",
            suggest_manual=True,
        )
    return FetchError(
        "unknown", raw,
        "Не удалось загрузить подписку.",
        "Если URL открывается в браузере — скопируй ответ и вставь вручную.",
        suggest_manual=True,
    )


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
           proxy_url: Optional[str] = None) -> tuple[str, Optional[SubscriptionInfo]]:
    """One requests.get call; optionally routed through proxy_url.

    Returns (body, userinfo) — userinfo parsed from the provider's
    `Subscription-Userinfo` header (remaining traffic + expiry), or None.
    """
    proxies = None
    if proxy_url:
        # xray's mixed inbound speaks both HTTP and SOCKS on the same port,
        # so a single http://127.0.0.1:port URL handles http+https requests.
        proxies = {"http": proxy_url, "https": proxy_url}
    response = requests.get(url, timeout=timeout, proxies=proxies, headers={
        "User-Agent": USER_AGENT,
    })
    response.raise_for_status()
    userinfo = parse_userinfo(response.headers.get("Subscription-Userinfo", ""))
    return response.text, userinfo


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
    placeholders: list[str] = []
    for share_url in share_urls:
        try:
            cfg = parse(share_url)
        except ParseError as e:
            short = share_url[:60] + ("…" if len(share_url) > 60 else "")
            errors.append(f"{short} — {e}")
            continue
        if is_placeholder_config(cfg):
            placeholders.append(cfg.name or share_url[:40])
        else:
            configs.append(cfg)
    return SubscriptionResult(
        configs=configs, errors=errors, raw_lines=len(share_urls),
        via_proxy=via_proxy, placeholders=placeholders,
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
    body, userinfo = _fetch(url, timeout, proxy_url=proxy_url)
    result = result_from_body(body, via_proxy=bool(proxy_url))
    result.userinfo = userinfo
    return result


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
