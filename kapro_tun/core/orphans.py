"""End helper processes a previous run of KaproTUN left behind — and only those.

A run that crashed or was force-closed can leave its engine alive: the process
keeps the "KaproTun" adapter (the next start then dies with "Cannot create a
file when that file already exists") and keeps the engine's file open (a fresh
download cannot replace it). So leftovers are ended at startup, and again when
a connect finds the adapter busy.

Until v4.1.2 that was done by image name — `taskkill /IM sing-box.exe`,
`pkill -x sing-box`. But sing-box, xray and tun2socks are what half of all VPN
clients are built on: with another client running next to ours, starting
KaproTUN ended its engine and dropped the user's other tunnel. A name says
what a program is, not whose it is.

A process is ours if it was started from our own data folder — that is where
the engine is installed, and nobody else's is. Anything whose path we cannot
read is left alone, and if processes cannot be listed at all, nothing is
killed: never "by name, then".
"""
from __future__ import annotations

import os
from typing import Iterable, Iterator

from . import app_log, paths

# Engines this client runs or ever ran (legacy ones can still be orphans of an
# old version on the first start after an update).
HELPER_NAMES = ("sing-box", "tun2socks", "xray", "hysteria")


def _processes() -> Iterator[tuple]:
    """(pid, image name, executable path) of the processes we can see."""
    import psutil
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        info = proc.info
        yield info.get("pid"), info.get("name") or "", info.get("exe") or ""


def _kill(pid: int) -> bool:
    """End the process and wait for it to be gone (so its files and its
    adapter are released before the caller goes on)."""
    import psutil
    try:
        proc = psutil.Process(pid)
        proc.kill()
    except psutil.NoSuchProcess:
        return True
    except Exception:
        return False
    try:
        proc.wait(timeout=3)
    except Exception:
        # Told to go, slow to leave. Still "ended" for the caller, who then
        # gives the system a moment before it tries the adapter again.
        pass
    return True


def _norm(path: str) -> str:
    try:
        path = os.path.realpath(path)
    except OSError:
        path = os.path.abspath(path)
    return os.path.normcase(path)


def _own_roots() -> list[str]:
    roots = []
    for folder in (paths.app_data_dir, paths.sing_box_dir, paths.tun_dir):
        try:
            roots.append(_norm(str(folder())).rstrip("\\/") + os.sep)
        except Exception:
            pass
    return roots


def _stem(name: str) -> str:
    name = (name or "").lower()
    return name[:-4] if name.endswith(".exe") else name


def kill_own_helpers(names: Iterable[str] = HELPER_NAMES) -> int:
    """End leftover helper processes that were started from our data folder.
    Returns how many were ended. Never raises."""
    want = {_stem(n) for n in names}
    me = os.getpid()
    try:
        roots = _own_roots()
        found = [(pid, name) for pid, name, exe in _processes()
                 if pid != me and _stem(name) in want and exe
                 and any(_norm(exe).startswith(root) for root in roots)]
    except Exception as e:
        app_log.log(f"[orphans] processes could not be listed — nothing ended: {type(e).__name__}")
        return 0
    ended = 0
    for pid, name in found:
        if _kill(pid):
            ended += 1
            app_log.log(f"[orphans] ended leftover {name} (pid {pid})")
        else:
            app_log.log(f"[orphans] could not end leftover {name} (pid {pid})")
    return ended
