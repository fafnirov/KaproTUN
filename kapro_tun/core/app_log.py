"""Rotating on-disk runtime log for the GUI / watchdog / connect lifecycle.

Until now those events lived only in the in-memory Logs page, so after a hang
or a crash there was nothing on disk to explain what happened (the existing
xray.log only covers xray). This writes lifecycle + watchdog + memory lines to
%LOCALAPPDATA%/KaproTUN/app.log with size-bounded rotation.

Hard rule: NO SECRETS. We only ever pass diagnostic lines here, and on top of
that every line is run through a redactor that strips share-URLs (which carry
UUIDs / passwords) and bare UUIDs before it touches the disk — defence in depth
in case a caller ever hands us a line that quotes a config.

Every function is best-effort and never raises: logging must not be able to
break the app.
"""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from typing import Optional

from . import paths


class _Utf8BomRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that guarantees a UTF-8 BOM at the very START of
    every log file (active + rotated).

    Why: the file is genuinely UTF-8, but Windows PowerShell 5.1 `Get-Content`
    and Notepad default to the ANSI code page (CP1251 on RU Windows) when there
    is no BOM, so Cyrillic diagnostics render as mojibake (`117.8 РњР‘`). A
    leading BOM makes those tools auto-detect UTF-8 and show the log correctly.

    We keep the plain `utf-8` codec (NOT `utf-8-sig`, whose incremental encoder
    would inject a BOM on the first write after re-opening a NON-empty file on
    restart — i.e. mid-stream) and emit the BOM ourselves only when the target
    file is empty. On rollover the base file is recreated empty, so each rotated
    file also starts with a BOM; on append to an existing file, none is added.
    """

    def _open(self):
        fresh = True
        try:
            fresh = (not os.path.exists(self.baseFilename)
                     or os.path.getsize(self.baseFilename) == 0)
        except OSError:
            fresh = False
        stream = super()._open()
        if fresh:
            try:
                stream.write("\ufeff")   # \ufeff -> EF BB BF in UTF-8 = the BOM
                stream.flush()
            except Exception:
                pass
        return stream

# Share-links (vless/vmess/trojan/ss/hysteria2/tuic/http(s)) embed the UUID,
# password and host — redact the whole token. Then redact any remaining bare
# UUID (8-4-4-4-12 hex).
_SHARE_URL = re.compile(
    r'\b(?:https?|vless|vmess|trojan|ss|ssr|hysteria2?|hy2|tuic|socks5?)://\S+',
    re.IGNORECASE,
)
_UUID = re.compile(
    r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'
)

_MAX_BYTES = 1_000_000   # ~1 MB per file …
_BACKUPS = 2             # … times (1 active + 2 rotated) ≈ 3 MB ceiling

_logger: Optional[logging.Logger] = None
_init_done = False


def redact(msg: str) -> str:
    """Strip share-URLs and bare UUIDs from a log line. Pure; never raises."""
    try:
        msg = _SHARE_URL.sub("[redacted-url]", msg)
        msg = _UUID.sub("[redacted-uuid]", msg)
        return msg
    except Exception:
        return "[redaction-error]"


def _get_logger() -> Optional[logging.Logger]:
    global _logger, _init_done
    if _init_done:
        return _logger
    _init_done = True
    try:
        lg = logging.getLogger("kaprotun.app")
        lg.setLevel(logging.INFO)
        lg.propagate = False  # don't leak into the root logger / console
        path = paths.app_log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _Utf8BomRotatingFileHandler(
            str(path), maxBytes=_MAX_BYTES, backupCount=_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        lg.addHandler(handler)
        _logger = lg
    except Exception:
        _logger = None
    return _logger


def log(msg: str) -> None:
    """Append one redacted, timestamped line to app.log. Best-effort."""
    try:
        lg = _get_logger()
        if lg is not None:
            lg.info(redact(str(msg)))
    except Exception:
        pass


def _reset_for_test() -> None:
    """Test hook: drop the cached logger + close handlers so a test can point
    app_log at a fresh file via a monkeypatched paths.app_log_file()."""
    global _logger, _init_done
    try:
        if _logger is not None:
            for h in list(_logger.handlers):
                try:
                    h.close()
                except Exception:
                    pass
                _logger.removeHandler(h)
    except Exception:
        pass
    _logger = None
    _init_done = False
