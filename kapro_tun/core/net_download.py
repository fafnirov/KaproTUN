"""Size-bounded HTTP downloads for our own binaries / installers.

A binary or installer fetch must never let a hostile or malfunctioning
server stream unbounded data into memory (or onto disk) — that's a trivial
DoS / disk-fill. Every download here is CAPPED two ways:

  • reject up front if the server's declared Content-Length exceeds the cap;
  • abort mid-stream the instant the running total crosses the cap (covers
    servers that lie about, or omit, Content-Length).

Caps are per asset type (below) — generous versus the real sizes but a hard
ceiling against a runaway response.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Callable, Optional

import requests

# Per-asset ceilings. Real sizes today: xray+geo ~25 MB, tun2socks ~5 MB,
# wintun ~0.5 MB, hysteria ~15 MB, our setup/portable exe ~40-60 MB.
MAX_XRAY_ZIP = 80 * 1024 * 1024
MAX_TUN2SOCKS_ZIP = 40 * 1024 * 1024
MAX_WINTUN_ZIP = 16 * 1024 * 1024
MAX_HYSTERIA_BIN = 80 * 1024 * 1024
MAX_SETUP_EXE = 150 * 1024 * 1024
# sing-box archive ~15-25 MB (binary ~30 MB unpacked); cap generously.
MAX_SINGBOX_ARCHIVE = 80 * 1024 * 1024

# Bypass system proxy — we're fetching our own deps, not user traffic, and a
# stale 127.0.0.1:2080 proxy from a crashed session would otherwise break it.
_NO_PROXY = {"http": "", "https": ""}

ProgressCb = Optional[Callable[[int, int], None]]


class DownloadTooLarge(RuntimeError):
    """A download exceeded its size cap (declared via Content-Length, or
    measured while streaming). Carries a user-readable Russian message."""


def _human(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb:.0f} МБ" if mb >= 1 else f"{n} Б"


def _reject_if_declared_too_big(resp, max_bytes: int, url: str) -> None:
    cl = resp.headers.get("Content-Length")
    if not cl:
        return
    try:
        declared = int(cl)
    except (TypeError, ValueError):
        return
    if declared > max_bytes:
        raise DownloadTooLarge(
            f"Файл слишком большой: сервер сообщил {_human(declared)} "
            f"(лимит {_human(max_bytes)}). Скачивание отклонено. [{url}]")


def _content_length(resp) -> int:
    """Declared body size for the progress callback, or 0 when the server
    omits OR lies about it (a non-numeric Content-Length like 'abc' must NOT
    crash the download — it's just treated as 'unknown total', and the hard
    cap in _guard_running_total still protects us during streaming)."""
    try:
        return int(resp.headers.get("Content-Length"))
    except (TypeError, ValueError):
        return 0


def _guard_running_total(downloaded: int, max_bytes: int, url: str) -> None:
    if downloaded > max_bytes:
        raise DownloadTooLarge(
            f"Скачивание превысило лимит {_human(max_bytes)} и было "
            f"прервано (сервер прислал больше заявленного). [{url}]")


def download_to_memory(url: str, max_bytes: int, progress: ProgressCb = None,
                       timeout=(10, 20)) -> bytes:
    """Stream `url` into memory, capped at `max_bytes`. Raises
    DownloadTooLarge if the declared or streamed size exceeds the cap, or
    requests exceptions on network failure."""
    with requests.get(url, stream=True, timeout=timeout, proxies=_NO_PROXY) as r:
        r.raise_for_status()
        _reject_if_declared_too_big(r, max_bytes, url)
        total = _content_length(r)
        sink = io.BytesIO()
        downloaded = 0
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            downloaded += len(chunk)
            _guard_running_total(downloaded, max_bytes, url)
            sink.write(chunk)
            if progress:
                progress(downloaded, total)
        return sink.getvalue()


def download_to_file(url: str, dest: Path, max_bytes: int,
                     progress: ProgressCb = None, timeout=(10, 30)) -> Path:
    """Stream `url` to `dest` atomically (.part then os.replace), capped at
    `max_bytes`. The partial file is removed on any failure. Returns `dest`."""
    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with requests.get(url, stream=True, timeout=timeout, proxies=_NO_PROXY) as r:
            r.raise_for_status()
            _reject_if_declared_too_big(r, max_bytes, url)
            total = _content_length(r)
            downloaded = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    _guard_running_total(downloaded, max_bytes, url)
                    f.write(chunk)
                    if progress:
                        progress(downloaded, total)
        os.replace(tmp, dest)
        return dest
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
