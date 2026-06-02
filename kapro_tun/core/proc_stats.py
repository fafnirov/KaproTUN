"""Per-process memory / handle sampling for the runtime memory watchdog.

tun2socks (gVisor's userspace netstack) and xray buffer per connection; under a
flood of flows or a leak their private memory and handle count can climb until
the machine swaps to a halt (observed: tun2socks ~5 GB / ~30k handles, xray
~2.3 GB / ~59k handles). This module gives a cheap, best-effort sample of those
two numbers so the watchdog can log them and, on a genuine runaway, trigger a
clean reconnect.

Built on psutil (already a project dependency). Every public function is
best-effort and NEVER raises — a sampling failure must not be able to break the
connect/heal path or the UI poll that drives it.
"""
from __future__ import annotations

from collections import namedtuple
from typing import Optional

# private_bytes: Windows "private bytes" (PrivateUsage) — the number Task
#   Manager shows and the one the bug report cited; RSS elsewhere.
# handles: Windows handle count (0 on platforms without the concept).
ProcSample = namedtuple("ProcSample", ["private_bytes", "handles"])


def sample(pid: Optional[int]) -> Optional[ProcSample]:
    """Sample one process by pid. Returns None if pid is None, the process is
    gone, or anything goes wrong — callers treat None as 'no data this tick'."""
    if pid is None:
        return None
    try:
        import psutil
        p = psutil.Process(int(pid))
        mi = p.memory_info()
        private = getattr(mi, "private", None)
        if private is None:                      # non-Windows: no PrivateUsage
            private = getattr(mi, "rss", 0)
        try:
            handles = int(p.num_handles())       # Windows-only API
        except Exception:
            handles = 0
        return ProcSample(int(private or 0), handles)
    except Exception:
        return None


def human_bytes(n) -> str:
    """Compact human size for logs (Russian units)."""
    try:
        n = float(n)
    except Exception:
        return "?"
    if n < 1024:
        return f"{int(n)} Б"
    for unit in ("КБ", "МБ", "ГБ", "ТБ"):
        n /= 1024
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} ТБ"
