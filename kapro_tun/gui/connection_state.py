"""Single source of truth for connection-state presentation (v2.1.0).

The app used ad-hoc state strings ('idle' / 'connecting' / 'connected')
scattered across the connect button, the status label and the tray, and it
surfaced reconnect / error / kill-switch only as toasts + log lines — so the
main UI could read "Подключено" while the tunnel was actually flapping or the
kill-switch was holding everything blocked. That ambiguity is the bug.

This module normalises every situation into ONE model of six canonical states,
each with a fixed presentation: a status label, a colour accent (palette field
name), a small indicator glyph, and the connect-button behaviour. UI widgets
map a state -> StateSpec and never invent their own text/colour again.

Pure data + helpers, no Qt imports — trivially unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..core.i18n import tr

# --- the six canonical states ---------------------------------------------
DISCONNECTED = "disconnected"
CONNECTING = "connecting"
CONNECTED = "connected"
RECONNECTING = "reconnecting"
ERROR = "error"
KILLSWITCH_ACTIVE = "killswitch_active"

ALL_STATES = (
    DISCONNECTED, CONNECTING, CONNECTED, RECONNECTING, ERROR, KILLSWITCH_ACTIVE,
)

# Legacy / loose aliases older call-sites still pass.
_ALIASES = {
    "idle": DISCONNECTED,
    "off": DISCONNECTED,
    "disconnect": DISCONNECTED,
    "": DISCONNECTED,
}


def normalize(state: str) -> str:
    """Map any input (legacy alias, garbage, None) to a canonical state.
    Unknown -> DISCONNECTED (the safe 'nothing is happening' default)."""
    s = (state or "").strip().lower()
    s = _ALIASES.get(s, s)
    return s if s in ALL_STATES else DISCONNECTED


@dataclass(frozen=True)
class StateSpec:
    state: str            # canonical state
    label: str            # status text; '{detail}' optionally appended by caller.
                          # In _SPECS this holds an i18n KEY ("cstate.*"); spec()
                          # resolves it to the active-locale string via tr().
    accent: str           # palette FIELD name: ACCENT | TEXT_MUTED | DANGER | SUCCESS
    glyph: str            # tiny indicator char (○ ◌ ● ✕ ■)
    button_text_key: str  # i18n key for the connect-button caption
    button_enabled: bool  # whether the connect button accepts clicks
    circle_state: str     # CircleConnectButton VISUAL state: idle | connecting | connected
    is_error: bool        # error-class state -> surface explicitly, never swallow


# The `label` slot holds an i18n KEY here, not the literal text — spec()
# resolves it through tr() at call time so the active locale (fixed at
# startup) wins. Storing the key (not tr(...)) keeps _SPECS locale-agnostic
# even though this module is imported before i18n.init_from_settings runs.
_SPECS = {
    DISCONNECTED: StateSpec(
        DISCONNECTED, "cstate.disconnected", "TEXT_MUTED", "○",
        "home.connect", True, "idle", False),
    CONNECTING: StateSpec(
        CONNECTING, "cstate.connecting", "TEXT_MUTED", "◌",
        "home.connecting", True, "connecting", False),
    CONNECTED: StateSpec(
        CONNECTED, "cstate.connected", "ACCENT", "●",
        "home.disconnect", True, "connected", False),
    RECONNECTING: StateSpec(
        RECONNECTING, "cstate.reconnecting", "ACCENT", "◌",
        "home.connecting", True, "connecting", False),
    ERROR: StateSpec(
        ERROR, "cstate.error", "DANGER", "✕",
        "home.connect", True, "idle", True),
    KILLSWITCH_ACTIVE: StateSpec(
        KILLSWITCH_ACTIVE, "cstate.killswitch", "DANGER", "■",
        "home.connect", True, "idle", True),
}


def spec(state: str) -> StateSpec:
    """Presentation spec for a state (input normalised first).

    The stored `label` is an i18n key; resolve it to the active-locale
    string via tr() so callers get ready-to-display text in `.label`."""
    base = _SPECS[normalize(state)]
    return replace(base, label=tr(base.label))
