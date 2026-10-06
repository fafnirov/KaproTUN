"""Set/clear the system-wide HTTP proxy, cross-platform.

Per-OS strategy:

  Windows
    Tweak HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings
    + call WinINet's InternetSetOption to make IE/Edge/Office re-read it.

  macOS
    Shell out to `networksetup -setwebproxy / -setsecurewebproxy / -setproxybypassdomains`
    for every "active" network service (Wi-Fi, Ethernet, etc.). Most apps
    that honor system proxies pick this up immediately.

  Linux (GNOME)
    Use `gsettings` to flip /system/proxy/{mode,http,https} keys. KDE,
    XFCE etc. have their own settings stores; we cover GNOME because it's
    by far the majority desktop. For other DEs the user gets a clean
    no-op and a hint to use a per-app proxy, which xray on 127.0.0.1
    supports out of the box.

Each `get_state()` snapshot is opaque to callers — they pass it back to
`restore()` unchanged.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any

_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_INTERNET_OPTION_SETTINGS_CHANGED = 39
_INTERNET_OPTION_REFRESH = 37


def is_supported() -> bool:
    """True on all desktop OSes — we have at least a best-effort path
    for each. Callers should still expect set_proxy() to be a no-op
    on Linux outside GNOME.
    """
    return sys.platform in ("win32", "darwin", "linux")


def get_state() -> dict:
    """Snapshot current proxy settings so we can restore them later."""
    if sys.platform == "win32":
        return _win_get_state()
    if sys.platform == "darwin":
        return _mac_get_state()
    if sys.platform == "linux":
        return _linux_get_state()
    return {}


def set_proxy(host: str, port: int, override: str = "<local>") -> None:
    """Turn on system proxy pointing at host:port."""
    if sys.platform == "win32":
        _win_set_proxy(host, port, override)
    elif sys.platform == "darwin":
        _mac_set_proxy(host, port, override)
    elif sys.platform == "linux":
        _linux_set_proxy(host, port, override)


def disable_proxy() -> None:
    """Turn off system proxy (preserve last-used host:port where possible)."""
    if sys.platform == "win32":
        _win_disable_proxy()
    elif sys.platform == "darwin":
        _mac_disable_proxy()
    elif sys.platform == "linux":
        _linux_disable_proxy()


def restore(state: dict) -> None:
    """Reapply a snapshot taken with get_state()."""
    if not state:
        return
    if sys.platform == "win32":
        _win_restore(state)
    elif sys.platform == "darwin":
        _mac_restore(state)
    elif sys.platform == "linux":
        _linux_restore(state)


# ============================================================================
# Windows — registry + WinINet
# ============================================================================

def _winreg():
    import winreg
    return winreg


def _win_read(key, name, default):
    winreg = _winreg()
    try:
        return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return default


def _win_notify() -> None:
    """Tell apps (WinINet-based: IE/Edge/Office) to re-read the proxy."""
    import ctypes
    wininet = ctypes.windll.wininet
    wininet.InternetSetOptionW(0, _INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
    wininet.InternetSetOptionW(0, _INTERNET_OPTION_REFRESH, 0, 0)


def _win_get_state() -> dict:
    winreg = _winreg()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY_PATH) as key:
        enable = _win_read(key, "ProxyEnable", 0)
        server = _win_read(key, "ProxyServer", "")
        override = _win_read(key, "ProxyOverride", "")
    return {"_os": "win", "enable": int(enable), "server": str(server), "override": str(override)}


def _win_set_proxy(host: str, port: int, override: str) -> None:
    winreg = _winreg()
    server = f"{host}:{port}"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
        winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, override)
    _win_notify()


def _win_disable_proxy() -> None:
    winreg = _winreg()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
    _win_notify()


def _win_restore(state: dict) -> None:
    winreg = _winreg()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(state.get("enable", 0)))
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, str(state.get("server", "")))
        winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, str(state.get("override", "")))
    _win_notify()


# ============================================================================
# macOS — networksetup
# ============================================================================

_NETWORKSETUP = "/usr/sbin/networksetup"

# (state key, get verb, set verb, on/off verb, default port). The three proxy
# slots macOS keeps per network service. SOCKS is set alongside HTTP/HTTPS so
# apps that only speak SOCKS are carried too; sing-box's `mixed` inbound
# answers all three on one port.
_MAC_SLOTS = (
    ("http", "-getwebproxy", "-setwebproxy", "-setwebproxystate", "80"),
    ("https", "-getsecurewebproxy", "-setsecurewebproxy", "-setsecurewebproxystate", "443"),
    ("socks", "-getsocksfirewallproxy", "-setsocksfirewallproxy",
     "-setsocksfirewallproxystate", "1080"),
)


def _mac_run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=10, check=check,
    )


def _mac_active_services() -> list[str]:
    """Return the network-service names that aren't disabled.

    `networksetup -listallnetworkservices` prints a one-line header
    ("An asterisk (*) denotes that a network service is disabled.")
    then one service per line, with a leading "*" for disabled ones.
    We strip those and ignore VPN/Bluetooth-PAN entries that don't have
    web-proxy slots.
    """
    try:
        proc = _mac_run([_NETWORKSETUP, "-listallnetworkservices"], check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    services: list[str] = []
    for line in proc.stdout.splitlines()[1:]:  # skip header
        line = line.strip()
        if not line or line.startswith("*"):  # disabled
            continue
        services.append(line)
    return services


def _mac_get_state() -> dict:
    """Snapshot the HTTP / HTTPS / SOCKS proxy slots of every active service.

    Output is a nested dict {service_name: {http: {...}, https: {...},
    socks: {...}}}.
    """
    snapshot: dict[str, dict[str, dict[str, str]]] = {}
    for svc in _mac_active_services():
        snapshot[svc] = {key: _mac_query_one(svc, get_verb)
                         for key, get_verb, _set, _state, _port in _MAC_SLOTS}
    return {"_os": "mac", "services": snapshot}


def _mac_query_one(service: str, verb: str) -> dict[str, str]:
    """Parse `networksetup -getwebproxy <svc>` output into a dict.

    Output format is line-based, "Key: Value" pairs:
        Enabled: Yes
        Server: 127.0.0.1
        Port: 2080
        Authenticated Proxy Enabled: 0
    """
    out: dict[str, str] = {}
    try:
        proc = _mac_run([_NETWORKSETUP, verb, service], check=False)
    except (OSError, subprocess.SubprocessError):
        return out
    for line in proc.stdout.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip().lower()] = v.strip()
    return out


def _mac_set_proxy(host: str, port: int, override: str) -> None:
    """Point HTTP, HTTPS and SOCKS at host:port on every active service.

    `<local>` (Windows convention) translates to bypass entries for
    *.local, 169.254/16, and the standard localhost set.

    Failures are swallowed per service (a Bluetooth PAN has no proxy slots),
    which is exactly why callers must not treat a clean return as success —
    use mac_proxy_points_at() to find out what actually took effect.
    """
    bypass = _mac_bypass_list(override)
    for svc in _mac_active_services():
        for _key, _get, set_verb, _state, _port in _MAC_SLOTS:
            try:
                _mac_run([_NETWORKSETUP, set_verb, svc, host, str(port)])
            except (OSError, subprocess.SubprocessError):
                continue
        if bypass:
            try:
                _mac_run([_NETWORKSETUP, "-setproxybypassdomains", svc, *bypass])
            except (OSError, subprocess.SubprocessError):
                pass


def mac_proxy_points_at(host: str, port: int) -> bool:
    """True if at least one active service really has its web proxy enabled
    and pointing at host:port.

    This is the only trustworthy answer to "did the system proxy apply".
    networksetup changes network settings through an authorization check:
    an administrator account passes it silently, but a standard account is
    refused, and the refusal arrives as an ordinary non-zero exit that
    _mac_set_proxy has to tolerate anyway. Reading the setting back is the
    difference between "connected" and "connected, but nothing is using it".
    """
    if sys.platform != "darwin":
        return False
    for svc in _mac_active_services():
        cur = _mac_query_one(svc, "-getwebproxy")
        if (cur.get("enabled", "").lower() == "yes"
                and cur.get("server") == host
                and cur.get("port") == str(port)):
            return True
    return False


def _mac_disable_proxy() -> None:
    for svc in _mac_active_services():
        for _key, _get, _set, state_verb, _port in _MAC_SLOTS:
            try:
                _mac_run([_NETWORKSETUP, state_verb, svc, "off"])
            except (OSError, subprocess.SubprocessError):
                continue


def _mac_restore(state: dict) -> None:
    """Reapply the per-service proxy settings we snapshotted.

    A slot that was enabled goes back to its server:port; everything else is
    switched off. A slot missing from an older snapshot is left alone rather
    than guessed at."""
    services = state.get("services", {})
    for svc, conf in services.items():
        for key, _get, set_verb, state_verb, default_port in _MAC_SLOTS:
            if key not in conf:
                continue
            slot = conf.get(key) or {}
            try:
                if slot.get("enabled", "").lower() == "yes" and slot.get("server"):
                    _mac_run([_NETWORKSETUP, set_verb, svc,
                              slot["server"], slot.get("port", default_port)])
                else:
                    _mac_run([_NETWORKSETUP, state_verb, svc, "off"])
            except (OSError, subprocess.SubprocessError):
                continue


def _mac_bypass_list(override: str) -> list[str]:
    """Translate Windows-style override into a mac bypass-domains list."""
    if not override:
        return []
    # <local> → typical loopback/link-local set
    if override.strip() == "<local>":
        return [
            "*.local", "169.254/16", "localhost", "127.0.0.1",
            "::1", "*.lan", "*.home", "*.intranet",
        ]
    # Otherwise treat as semicolon-separated list (Windows format)
    return [p.strip() for p in override.split(";") if p.strip()]


# ============================================================================
# Linux — gsettings (GNOME). Other DEs get a graceful no-op.
# ============================================================================

def _gsettings_available() -> bool:
    return shutil.which("gsettings") is not None


def _gset(schema: str, key: str, value: str) -> None:
    try:
        subprocess.run(["gsettings", "set", schema, key, value],
                       check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _gget(schema: str, key: str) -> str:
    try:
        proc = subprocess.run(["gsettings", "get", schema, key],
                              check=False, capture_output=True, text=True, timeout=5)
        return (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _linux_get_state() -> dict:
    if not _gsettings_available():
        return {"_os": "linux", "available": False}
    return {
        "_os": "linux",
        "available": True,
        "mode":        _gget("org.gnome.system.proxy", "mode"),
        "http_host":   _gget("org.gnome.system.proxy.http", "host"),
        "http_port":   _gget("org.gnome.system.proxy.http", "port"),
        "https_host":  _gget("org.gnome.system.proxy.https", "host"),
        "https_port":  _gget("org.gnome.system.proxy.https", "port"),
        "ignore_hosts": _gget("org.gnome.system.proxy", "ignore-hosts"),
    }


def _linux_set_proxy(host: str, port: int, override: str) -> None:
    if not _gsettings_available():
        return
    _gset("org.gnome.system.proxy.http",  "host", host)
    _gset("org.gnome.system.proxy.http",  "port", str(port))
    _gset("org.gnome.system.proxy.https", "host", host)
    _gset("org.gnome.system.proxy.https", "port", str(port))
    if override == "<local>":
        # GNOME ignore-hosts is a GVariant array literal
        _gset("org.gnome.system.proxy", "ignore-hosts",
              "['localhost', '127.0.0.0/8', '::1']")
    _gset("org.gnome.system.proxy", "mode", "manual")


def _linux_disable_proxy() -> None:
    if not _gsettings_available():
        return
    _gset("org.gnome.system.proxy", "mode", "none")


def _linux_restore(state: dict) -> None:
    if not state.get("available") or not _gsettings_available():
        return
    if state.get("http_host"):
        _gset("org.gnome.system.proxy.http", "host", state["http_host"].strip("'\""))
    if state.get("http_port"):
        _gset("org.gnome.system.proxy.http", "port", state["http_port"])
    if state.get("https_host"):
        _gset("org.gnome.system.proxy.https", "host", state["https_host"].strip("'\""))
    if state.get("https_port"):
        _gset("org.gnome.system.proxy.https", "port", state["https_port"])
    if state.get("ignore_hosts"):
        _gset("org.gnome.system.proxy", "ignore-hosts", state["ignore_hosts"])
    if state.get("mode"):
        _gset("org.gnome.system.proxy", "mode", state["mode"].strip("'\""))
