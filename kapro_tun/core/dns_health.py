"""DNS reachability probe for TUN mode.

In TUN mode with DNS-leak protection the physical NIC's DNS is cleared, so
*every* name lookup has to travel through the tunnel's DNS path. If that path
is dead (tunnel up but resolver unreachable), the machine ends up "connected
but nothing resolves" — the classic failure that forces a manual adapter
restart. This module gives a cheap yes/no answer to "can the system resolve
names right now?" used in two places:

  * connect-time gate (controller): after raising the tunnel and clearing the
    physical DNS, confirm resolution works before committing; roll back if not.
  * runtime watchdog (GUI): periodically re-check; trigger a bounded self-heal
    on a sustained outage.

Design rules:
  * NEVER raises — every public function returns a bool. Callers must be able
    to treat a probe as advisory and never have it crash a connect/heal path.
  * Bounded wall-clock — getaddrinfo can hang far past socket timeouts on a
    broken resolver, so each lookup runs in a worker thread we abandon on
    timeout instead of trusting the C library to honour a deadline.
  * Uses the OS resolver (socket.getaddrinfo), so it exercises the exact same
    path real traffic uses — including whichever interface currently owns DNS.
"""
from __future__ import annotations

import socket
# Import the executor eagerly (not via concurrent.futures.<attr> at first use).
# concurrent.futures lazily imports its .thread submodule on first attribute
# access, and that submodule calls threading._register_atexit() at import time —
# which raises "can't register atexit after shutdown" if the FIRST probe ever
# happens during interpreter teardown. Importing here, at app startup, does the
# registration early and once, so a probe fired from the shutdown path is safe.
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FuturesTimeout

# Well-known, highly-available hosts that are NOT in any RU-direct list, so in
# TUN mode they resolve *through the tunnel* — exactly the path we want to test.
# Multiple, so one provider's hiccup doesn't read as a tunnel outage.
_DEFAULT_HOSTS = ("cloudflare.com", "www.google.com", "github.com")


def _resolve_once(host: str, timeout: float) -> bool:
    """True if `host` resolves to at least one A record within `timeout`s.

    getaddrinfo has no timeout argument and can block well past any socket
    deadline when the resolver is black-holing queries, so we run it in a
    throwaway thread and give up on it after `timeout`. The abandoned thread
    dies on its own once the C call returns or errors; we never join it.
    """
    def _lookup() -> bool:
        try:
            socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            return True
        except OSError:
            return False
        except Exception:
            return False

    try:
        ex = ThreadPoolExecutor(max_workers=1)
    except Exception:
        # e.g. interpreter shutting down — treat as "can't resolve right now".
        return False
    try:
        fut = ex.submit(_lookup)
        try:
            return bool(fut.result(timeout=max(0.1, timeout)))
        except _FuturesTimeout:
            return False
        except Exception:
            return False
    finally:
        # Don't block on the possibly-stuck lookup thread.
        ex.shutdown(wait=False)


def probe(timeout: float = 2.0, attempts: int = 2,
          hosts: tuple[str, ...] | None = None) -> bool:
    """True if DNS resolution works right now.

    Returns as soon as ANY host in ANY attempt resolves (fast success path).
    Only returns False after every host has failed `attempts` times — so a
    single flaky lookup never reads as an outage. Worst-case wall-clock is
    bounded by `len(hosts) * attempts * timeout`.
    """
    host_list = hosts if hosts else _DEFAULT_HOSTS
    a = max(1, int(attempts))
    for _ in range(a):
        for h in host_list:
            try:
                if _resolve_once(h, timeout):
                    return True
            except Exception:
                # _resolve_once already swallows everything, but stay paranoid:
                # a probe must never escalate to an exception.
                continue
    return False
