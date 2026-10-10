"""Russian IPv4 CIDR list fetcher and parser.

Used in TUN mode to install bypass routes for the entire Russian IP space,
so any RU-hosted resource (Yandex CDN, VK statics, Sberbank, госуслуги,
even minor sites we never explicitly listed) is routed through the user's
real interface instead of looping into our VPN.

Source: ipdeny.com aggregated zone files, derived hourly from RIRs (RIPE
for RU). About 8600 IPv4 CIDRs in the aggregated list (45 million addresses,
1% of IPv4). Plain text, one CIDR per line.

This list decides which destinations LEAVE THE TUNNEL, and it arrives over
plain HTTPS with no signature we could pin (it changes hourly). So it is not
trusted as given: every line must be a real IPv4 network, and the list as a
whole must look like one country's address space — see parse_cidrs(). A list
that fails is discarded, and the session simply runs without the RU rule
(everything through the VPN), which is the safe direction.

(The previously-used herrbischoff/country-ip-blocks repo got archived in
March 2026; ipdeny is the stable long-running alternative.)
"""
from __future__ import annotations

import ipaddress
import os
from typing import Callable, Optional

import requests

from . import paths

GEOIP_RU_URL = "https://www.ipdeny.com/ipblocks/data/aggregated/ru-aggregated.zone"
MIN_VALID_FILE_BYTES = 5_000  # sanity check — actual file is ~135 KB
MAX_FILE_BYTES = 2_000_000    # ~15x the real file; nothing legitimate is bigger

# What one country's list may look like. The real one (Oct 2026): 8611 networks,
# the widest a /13, 45 M addresses in total. The limits leave room for growth
# and still make "route the whole internet direct" impossible to express.
MIN_PREFIX = 12                   # nothing wider than a /12 (1 M addresses)
MAX_NETWORKS = 20_000
MAX_TOTAL_ADDRESSES = 90_000_000   # 2.1% of IPv4, twice the real list
# ...and a floor. A near-empty list is not "safe": it would be cached, never
# replaced (the list is fetched only when missing), and RU-direct would stay
# switched off without a word.
MIN_NETWORKS = 1_000
MIN_TOTAL_ADDRESSES = 10_000_000


class InvalidList(ValueError):
    """The list as a whole cannot be a country's address space."""

# Bypass system proxy — see xray_installer for the full story.
_NO_PROXY = {"http": "", "https": ""}

ProgressCb = Optional[Callable[[int, int], None]]


def cache_file() -> object:
    """Path to the locally cached CIDR file."""
    return paths.app_data_dir() / "geoip-ru.txt"


def is_cached() -> bool:
    f = cache_file()
    return f.is_file() and f.stat().st_size >= MIN_VALID_FILE_BYTES


def parse_cidrs(text: str) -> list[ipaddress.IPv4Network]:
    """The IPv4 networks in `text`, one per line.

    A line that is not an IPv4 network (a hostname, IPv6, a bad prefix) is
    skipped: one such line used to reach the engine config and make sing-box
    reject it on every connect. Raises InvalidList when the list itself is
    implausible — a network wider than MIN_PREFIX, or too much address space
    in total — because then it is not a list to repair, it is one to refuse.
    """
    nets: list[ipaddress.IPv4Network] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "/" not in line:
            continue
        try:
            net = ipaddress.IPv4Network(line, strict=False)
        except ValueError:
            continue
        if net.prefixlen < MIN_PREFIX:
            raise InvalidList(f"{net} is wider than /{MIN_PREFIX}")
        nets.append(net)
        if len(nets) > MAX_NETWORKS:
            raise InvalidList(f"more than {MAX_NETWORKS} networks")
    if len(nets) < MIN_NETWORKS:
        raise InvalidList(f"only {len(nets)} networks, expected at least {MIN_NETWORKS}")
    total = sum(n.num_addresses for n in ipaddress.collapse_addresses(nets))
    if not MIN_TOTAL_ADDRESSES <= total <= MAX_TOTAL_ADDRESSES:
        raise InvalidList(f"{total} addresses in total, expected "
                          f"{MIN_TOTAL_ADDRESSES}..{MAX_TOTAL_ADDRESSES}")
    return nets


def download(progress: ProgressCb = None, attempts: int = 3) -> None:
    """Download the CIDR list with chunk timeout + retry, write to cache.

    The body is size-capped and must pass parse_cidrs() BEFORE it is written;
    the write is atomic, so the cache never holds a partial or refused list."""
    last_err: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            data = b""
            with requests.get(GEOIP_RU_URL, stream=True, timeout=(10, 20),
                              proxies=_NO_PROXY) as r:
                r.raise_for_status()
                if not str(getattr(r, "url", GEOIP_RU_URL)).lower().startswith("https://"):
                    raise InvalidList("redirected away from HTTPS")
                total = int(r.headers.get("Content-Length", 0))
                chunks: list[bytes] = []
                got = 0
                for chunk in r.iter_content(chunk_size=8 * 1024):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    got += len(chunk)
                    if got > MAX_FILE_BYTES:
                        raise InvalidList(f"list is larger than {MAX_FILE_BYTES} bytes")
                    if progress:
                        progress(got, total)
                data = b"".join(chunks)
            if len(data) < MIN_VALID_FILE_BYTES:
                raise InvalidList("list is too short to be real")
            parse_cidrs(data.decode("utf-8", errors="replace"))
            target = cache_file()
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            return
        except InvalidList as e:
            # Not a network hiccup: the server sent something we refuse.
            # Retrying the same URL would only fetch it again.
            raise RuntimeError(f"Список geoip:ru отклонён: {e}") from e
        except (requests.exceptions.RequestException, OSError) as e:
            last_err = e
    raise RuntimeError(f"Не удалось скачать geoip:ru после {attempts} попыток: {last_err}")


def ensure_cached(progress: ProgressCb = None) -> bool:
    """Returns True if the cache is available (downloads if missing)."""
    if is_cached():
        return True
    try:
        download(progress=progress)
    except Exception:
        return False
    return is_cached()


def load_cidrs() -> list[tuple[str, str]]:
    """Parse cached file into a list of (network, dotted-mask) tuples.

    Returns empty list if the cache is missing or malformed. Each CIDR like
    `5.255.192.0/19` becomes ('5.255.192.0', '255.255.224.0').

    A cache that fails validation is DELETED: it would otherwise sit there
    forever (the list is only downloaded when missing), and the next connect
    would trust it again. Without it the session has no RU rule, and the next
    connect fetches a fresh list.
    """
    if not is_cached():
        return []
    try:
        if cache_file().stat().st_size > MAX_FILE_BYTES:
            raise InvalidList(f"cache is larger than {MAX_FILE_BYTES} bytes")
        nets = parse_cidrs(cache_file().read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    except InvalidList as e:
        from . import app_log
        app_log.log(f"[geoip] cached RU list rejected and removed: {e}")
        try:
            cache_file().unlink()
        except OSError:
            pass
        return []
    return [(str(n.network_address), str(n.netmask)) for n in nets]
