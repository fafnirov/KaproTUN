"""Crash-recovery for TUN mode's physical-NIC DNS clobber.

The dangerous, non-self-healing state in TUN mode is this: leak protection
clears DNS on the physical interface so lookups can't leak to the ISP resolver.
That clear is undone on a clean disconnect. But if the app is force-killed,
crashes, or the machine loses power *while connected*, the physical NIC stays
"DNS = none" with no tunnel to carry queries — the user reboots into a machine
that resolves nothing and has to restart the adapter by hand.

This module makes that recoverable without any manual step:

  * mark(name, index)  — write a tiny journal naming the interface whose DNS we
    are about to clear. Called right before the clear, every connect.
  * clear()            — delete the journal. Called on clean disconnect. Its
    absence means "no TUN session is holding anyone's DNS hostage".
  * recover()          — run once at startup. If a journal survived from a
    previous run, the previous run did NOT disconnect cleanly, so restore that
    interface's DNS to DHCP and delete the journal. Idempotent and silent when
    there's nothing to recover (no journal → returns []).

Everything here is best-effort and never raises into its caller: a recovery
pass must not be able to stop the app from starting, and journalling must not
be able to stop a connect.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from . import paths

_JOURNAL_VERSION = 1


def _journal_path():
    return paths.tun_recovery_file()


def mark(iface_name: str, iface_index: Optional[int]) -> bool:
    """Record that `iface_name`/`iface_index`'s DNS is being cleared.

    Written atomically (temp + replace) so a crash mid-write can't leave a
    half-parsed journal. Returns True on success; never raises — a failed
    journal write costs us recovery for this one session but must not abort
    the connect.
    """
    try:
        path = _journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _JOURNAL_VERSION,
            "iface_name": iface_name or "",
            "iface_index": int(iface_index) if iface_index is not None else None,
            "dns_cleared": True,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def clear() -> None:
    """Delete the journal — called on a clean disconnect. Silent if absent."""
    try:
        p = _journal_path()
        if p.exists():
            p.unlink()
    except Exception:
        pass


def has_pending() -> bool:
    """True if a journal exists (a TUN session is, or was, holding DNS)."""
    try:
        return _journal_path().exists()
    except Exception:
        return False


def _read_journal() -> Optional[dict]:
    try:
        with open(_journal_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        # Corrupt/garbage journal — signal "present but unreadable" so recover()
        # still cleans it up rather than leaving it to trip every startup.
        return {}


def recover() -> list[str]:
    """Undo a leaked DNS-clear left by a previous, unclean run.

    Returns a list of human-readable action strings (for the log). Empty list
    means there was nothing to recover — the normal, clean-shutdown case — so
    callers can stay silent. Idempotent: deletes the journal at the end, so a
    second call right after does nothing. Never raises.
    """
    actions: list[str] = []
    try:
        if not has_pending():
            return actions

        data = _read_journal()
        if not data:
            # Missing or unreadable — nothing actionable, just clear the marker.
            clear()
            if data == {}:
                actions.append("Найден повреждённый журнал TUN — удалён.")
            return actions

        if not data.get("dns_cleared"):
            clear()
            return actions

        name = (data.get("iface_name") or "").strip()
        index = data.get("iface_index")
        label = name or (f"ifIndex {index}" if index is not None else "?")
        actions.append(
            f"Обнаружен незавершённый TUN-сеанс — восстанавливаю DNS на «{label}»."
        )

        # Import here (not at module top) so this module stays importable on
        # non-Windows / in tests without dragging in the Windows route stack.
        try:
            from . import network_routes as nr
        except Exception:
            nr = None

        restored = False
        if nr is not None:
            # Prefer index — robust to a name captured with a broken encoding.
            if isinstance(index, int):
                try:
                    restored = bool(nr.reset_dns_by_index(index))
                except Exception:
                    restored = False
            if not restored and name:
                try:
                    nr.reset_dns(name)
                    restored = True
                except Exception:
                    restored = False

        if restored:
            actions.append(f"DNS на «{label}» возвращён в режим DHCP.")
        else:
            actions.append(
                f"Не удалось автоматически восстановить DNS на «{label}» "
                f"(возможно, интерфейс уже отключён) — DHCP вернёт его при "
                f"переподключении сети."
            )

        clear()
        return actions
    except Exception:
        # Absolute backstop — a recovery pass must never break startup.
        try:
            clear()
        except Exception:
            pass
        return actions
