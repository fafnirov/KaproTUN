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

    # A rollover can lose a race on Windows: another instance of the app (or a
    # tail/editor) holds the file open and the rename fails. The stock handler
    # then leaves the stream closed and every later record is dropped — which is
    # exactly how app.log went silent for ten days while the app kept running.
    # An oversized log is vastly better than no log, so a failed rotation is
    # swallowed, the stream reopened, and the next attempt is delayed a little
    # instead of being retried on every single record.
    _ROLLOVER_RETRY_S = 30.0

    def doRollover(self):
        import time
        now = time.monotonic()
        if now < getattr(self, "_rollover_blocked_until", 0.0):
            return
        try:
            super().doRollover()
            self._rollover_blocked_until = 0.0
        except Exception:
            self._rollover_blocked_until = now + self._ROLLOVER_RETRY_S
            try:
                if self.stream is None:
                    self.stream = self._open()
            except Exception:
                pass

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


# How long to wait before retrying a failed logger setup. Without a retry a
# single transient failure (the log file momentarily held by another instance)
# silently disabled logging for the WHOLE process lifetime — the app ran for
# days afterwards writing nothing, leaving later problems undiagnosable.
_INIT_RETRY_S = 20.0
_next_init_attempt = 0.0


def _build_handler(path) -> logging.Handler:
    handler = _Utf8BomRotatingFileHandler(
        str(path), maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    return handler


def _get_logger() -> Optional[logging.Logger]:
    global _logger, _init_done, _next_init_attempt
    if _logger is not None:
        return _logger
    import time
    now = time.monotonic()
    if _init_done and now < _next_init_attempt:
        return None            # backing off, don't hammer a locked file
    _init_done = True
    _next_init_attempt = now + _INIT_RETRY_S
    try:
        lg = logging.getLogger("kaprotun.app")
        lg.setLevel(logging.INFO)
        lg.propagate = False  # don't leak into the root logger / console
        for stale in list(lg.handlers):    # a retry must not stack handlers
            lg.removeHandler(stale)
        path = paths.app_log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handler = _build_handler(path)
        except OSError:
            # The shared file is unusable (locked by another instance). Fall
            # back to a private per-process log rather than losing the trail.
            handler = _build_handler(path.with_name(f"{path.stem}-{os.getpid()}{path.suffix}"))
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


# --- Network Debug Mode (v3.5.1) ------------------------------------------
# Opt-in, millisecond-resolution trace of the connection lifecycle: health
# probes and their verdicts, reconnect decisions and WHY, interface/route
# changes, sing-box lifecycle. Off by default (it is chatty); the user turns it
# on to catch an intermittent problem in the act — e.g. a mid-game freeze.
# Never logs user traffic contents: only event names + numeric/technical fields,
# and every line still goes through redact().
_net_debug = False


def set_net_debug(enabled: bool) -> None:
    """Enable/disable Network Debug Mode. Cheap: net() is a no-op when off."""
    global _net_debug
    _net_debug = bool(enabled)


def net_debug_enabled() -> bool:
    return _net_debug


def net(event: str, **fields) -> None:
    """One millisecond-stamped diagnostic event. No-op unless debug is on.

    The normal app.log formatter only has second resolution, which is useless
    for correlating a 5-8 s freeze with what the client was doing — so the
    millisecond stamp is rendered into the message itself."""
    if not _net_debug:
        return
    try:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        tail = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        log(f"[net] {ts} {event}{(' ' + tail) if tail else ''}")
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
    global _next_init_attempt
    _logger = None
    _init_done = False
    _next_init_attempt = 0.0
