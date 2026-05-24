"""Russian IPv4 CIDR list fetcher and parser.

Used in TUN mode to install bypass routes for the entire Russian IP space,
so any RU-hosted resource (Yandex CDN, VK statics, Sberbank, госуслуги,
even minor sites we never explicitly listed) is routed through the user's
real interface instead of looping into our VPN.

Source: ipdeny.com aggregated zone files, derived hourly from RIRs (RIPE
for RU). About 3500 IPv4 CIDRs in the aggregated list. Plain text, one
CIDR per line.

(The previously-used herrbischoff/country-ip-blocks repo got archived in
March 2026; ipdeny is the stable long-running alternative.)
"""
from __future__ import annotations

import socket
import struct
from typing import Callable, Optional

import requests

from . import paths

GEOIP_RU_URL = "https://www.ipdeny.com/ipblocks/data/aggregated/ru-aggregated.zone"
MIN_VALID_FILE_BYTES = 5_000  # sanity check — actual file is ~80 KB

ProgressCb = Optional[Callable[[int, int], None]]


def cache_file() -> object:
    """Path to the locally cached CIDR file."""
    return paths.app_data_dir() / "geoip-ru.txt"


def is_cached() -> bool:
    f = cache_file()
    return f.is_file() and f.stat().st_size >= MIN_VALID_FILE_BYTES


def download(progress: ProgressCb = None, attempts: int = 3) -> None:
    """Download the CIDR list with chunk timeout + retry, write to cache."""
    last_err: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            data = b""
            with requests.get(GEOIP_RU_URL, stream=True, timeout=(10, 20)) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                chunks: list[bytes] = []
                got = 0
                for chunk in r.iter_content(chunk_size=8 * 1024):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
                data = b"".join(chunks)
            cache_file().write_bytes(data)
            return
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
    """
    if not is_cached():
        return []
    try:
        text = cache_file().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "/" not in line:
            continue
        try:
            net, prefix_str = line.split("/", 1)
            prefix = int(prefix_str)
            if not 0 <= prefix <= 32:
                continue
            mask_int = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
            mask = socket.inet_ntoa(struct.pack(">I", mask_int))
            out.append((net.strip(), mask))
        except (ValueError, OSError, struct.error):
            continue
    return out
