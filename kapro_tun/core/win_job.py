"""Windows Job Object backstop so helper processes die with the GUI.

Problem: sing-box / tun2socks / xray / hysteria are spawned as ordinary child
processes. On Windows a child does NOT die when its parent dies — so if the GUI
exits NON-gracefully (crash, taskkill, UAC-elevated fault, logoff), our Python
atexit/disconnect cleanup never runs and the helper is orphaned with the TUN up
and the default route hijacked (the user is left with no UI and a captured
network).

Fix: at GUI startup create ONE Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
and assign every spawned helper to it. The GUI process holds the only handle for
its whole life; when it exits for ANY reason the last handle closes and the
kernel terminates every assigned helper. sing-box removes its own auto_route
routes on exit, and the next launch's startup sweeps restore DNS/firewall.

Windows-only; every function is a silent no-op elsewhere and never raises.
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wt.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wt.LARGE_INTEGER),
            ("LimitFlags", wt.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wt.DWORD),
            ("Affinity", ctypes.POINTER(wt.ULONG)),
            ("PriorityClass", wt.DWORD),
            ("SchedulingClass", wt.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _job_handle = None  # held for the GUI process lifetime; never closed by us


def _ensure_job():
    """Lazily create the kill-on-close job. Returns its handle or None."""
    global _job_handle
    if _job_handle is not None:
        return _job_handle
    try:
        h = _k32.CreateJobObjectW(None, None)
        if not h:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _k32.SetInformationJobObject(
            h, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            _k32.CloseHandle(h)
            return None
        _job_handle = h
        return _job_handle
    except Exception:
        return None


def assign(pid: int) -> bool:
    """Assign process `pid` to the kill-on-close job. No-op off Windows.

    Returns True if assigned. Best-effort: never raises. When it can't assign
    (e.g. the child already created its own job, or insufficient rights), the
    existing Python-level teardown still applies — this is only the backstop.
    """
    if sys.platform != "win32" or not pid:
        return False
    job = _ensure_job()
    if not job:
        return False
    h = None
    try:
        h = _k32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, int(pid))
        if not h:
            return False
        return bool(_k32.AssignProcessToJobObject(job, h))
    except Exception:
        return False
    finally:
        if h:
            try:
                _k32.CloseHandle(h)
            except Exception:
                pass
