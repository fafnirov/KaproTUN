"""System-proxy lifecycle for proxy mode, with a crash-recovery journal.

Proxy mode (macOS without root) carries traffic by pointing the system proxy
at sing-box's local listener. That is a change to the user's machine that
outlives us if we die: the proxy keeps pointing at a port nobody is listening
on, and every browser on the machine fails with "connection refused" until
somebody finds the setting and turns it off. The same failure class as a TUN
session dying with the default route still hijacked — and handled the same
way, with a journal:

  begin()    snapshot the current proxy settings to disk, THEN change them
  end()      restore the snapshot and delete the journal
  recover()  on startup: a journal that still exists means the last session
             never reached end(), so restore it now

The snapshot is what makes this a restore rather than a blind "off": a user
who already ran a corporate or personal proxy gets it back, not wiped.
"""
from __future__ import annotations

import json
from typing import Optional

from . import paths, sing_box_config, system_proxy


def _journal():
    return paths.proxy_recovery_file()


def begin(host: str, port: int) -> bool:
    """Point the system proxy at host:port. Returns True only if it verifiably
    took effect; False means sing-box is listening but the system is NOT using
    it, and the caller must say so rather than report a working VPN.

    The journal is written BEFORE the first change. If writing it fails we
    change nothing: a proxy we could not undo after a crash is worse than no
    system proxy at all.
    """
    try:
        state = system_proxy.get_state()
        paths.write_secure_text(_journal(), json.dumps(state, ensure_ascii=False))
    except Exception:
        return False
    try:
        system_proxy.set_proxy(host, port)
    except Exception:
        pass
    applied = _points_at(host, port)
    if not applied:
        # Nothing (or only part) took effect. Put back whatever did change and
        # drop the journal, so a refused attempt leaves the machine untouched.
        end()
    return applied


def _points_at(host: str, port: int) -> bool:
    try:
        return system_proxy.mac_proxy_points_at(host, port)
    except Exception:
        return False


def end() -> None:
    """Restore the pre-session proxy settings and delete the journal.
    Idempotent and never raises: it runs on every disconnect and at exit."""
    state = _read_journal()
    if state is None:
        return
    try:
        if state:
            system_proxy.restore(state)
        elif _points_at(sing_box_config.PROXY_LISTEN_HOST,
                        sing_box_config.PROXY_LISTEN_PORT):
            # The journal exists but is unreadable, so there is no snapshot to
            # restore — yet the proxy still points at our listener. Switching
            # it off loses a pre-existing proxy setting we can no longer know;
            # leaving it on loses the user's internet. Off is the lesser harm.
            system_proxy.disable_proxy()
    except Exception:
        pass
    try:
        _journal().unlink()
    except OSError:
        pass


def recover() -> bool:
    """Startup hook. True if a stale session was found and undone.

    Safe to call unconditionally: the single-instance guard runs first, so a
    journal on disk cannot belong to a live session of ours."""
    if _read_journal() is None:
        return False
    end()
    return True


def is_active() -> bool:
    """True while a proxy session's journal is on disk."""
    return _read_journal() is not None


def _read_journal() -> Optional[dict]:
    try:
        f = _journal()
        if not f.is_file():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # Unreadable journal: we can't restore from it, but an empty dict still
        # lets end() delete it instead of tripping over it on every launch.
        return {}
