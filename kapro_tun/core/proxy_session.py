"""System-proxy lifecycle for proxy mode, with a crash-recovery journal.

Proxy mode (macOS without root) carries traffic by pointing the system proxy
at sing-box's local listener. That is a change to the user's machine that
outlives us if we die: the proxy keeps pointing at a port nobody is listening
on, and every browser on the machine fails with "connection refused" until
somebody finds the setting and turns it off. The same failure class as a TUN
session dying with the default route still hijacked — and handled the same
way, with a journal:

  begin()    snapshot the current proxy settings to disk, THEN change them
  end()      restore the snapshot and delete the journal — once the system
             proxy really no longer points at us; until then the journal stays
  recover()  on startup: a journal that still exists means the last session
             never reached end() (or its restore failed), so restore it now

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
        services = list((state or {}).get("services") or {})
        if not services:
            # Not one network service whose settings could be read: there
            # would be nothing to put back afterwards, so nothing is changed.
            return False
        paths.write_secure_text(_journal(), json.dumps(state, ensure_ascii=False))
    except Exception:
        return False
    try:
        # Only the services in the snapshot: what could not be read is not
        # ours to change.
        system_proxy.set_proxy(host, port, services=services)
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


def end() -> bool:
    """Restore the pre-session proxy settings and delete the journal.
    Idempotent and never raises: it runs on every disconnect and at exit.

    Returns False if the system proxy STILL points at our listener afterwards
    — macOS refused the change, or networksetup failed. The journal is then
    kept (v4.1.6; it used to be deleted regardless, and with it the only
    record of what to restore): the next start runs recover() and tries
    again, and the caller tells the user that the proxy is still on."""
    state = _read_journal()
    if state is None:
        return True
    ours = (sing_box_config.PROXY_LISTEN_HOST, sing_box_config.PROXY_LISTEN_PORT)
    try:
        if state:
            system_proxy.restore(state)
    except Exception:
        pass
    if _points_at(*ours):
        # The restore did not take, or there was no readable snapshot to
        # restore from. Switching the proxy off loses a pre-existing setting
        # we could not put back; leaving it on a listener that is gone loses
        # the user's internet. Off is the lesser harm.
        try:
            system_proxy.disable_proxy()
        except Exception:
            pass
        if _points_at(*ours):
            return False
    try:
        _journal().unlink()
    except OSError:
        pass
    return True


def points_at_listener() -> bool:
    """True if the system proxy points at our listener right now — whoever
    set it."""
    return _points_at(sing_box_config.PROXY_LISTEN_HOST, sing_box_config.PROXY_LISTEN_PORT)


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
