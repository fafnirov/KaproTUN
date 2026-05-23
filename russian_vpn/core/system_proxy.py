"""Set/clear Windows system HTTP proxy via the registry.

Stores the previous state so disconnect can restore exactly what the user had
before the app started — including the case where they had a manual proxy.
"""
from __future__ import annotations

import sys

_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_INTERNET_OPTION_SETTINGS_CHANGED = 39
_INTERNET_OPTION_REFRESH = 37


def _winreg():
    import winreg
    return winreg


def is_supported() -> bool:
    return sys.platform == "win32"


def get_state() -> dict:
    """Snapshot current proxy settings so we can restore them later."""
    if not is_supported():
        return {"enable": 0, "server": "", "override": ""}
    winreg = _winreg()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _KEY_PATH) as key:
        enable = _read(key, "ProxyEnable", 0)
        server = _read(key, "ProxyServer", "")
        override = _read(key, "ProxyOverride", "")
    return {"enable": int(enable), "server": str(server), "override": str(override)}


def set_proxy(host: str, port: int, override: str = "<local>") -> None:
    """Turn on system proxy pointing at host:port."""
    if not is_supported():
        return
    winreg = _winreg()
    server = f"{host}:{port}"
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _KEY_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
        winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, override)
    _notify()


def disable_proxy() -> None:
    """Turn off system proxy (leaves server/override values in place)."""
    if not is_supported():
        return
    winreg = _winreg()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _KEY_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
    _notify()


def restore(state: dict) -> None:
    """Reapply a snapshot taken with get_state()."""
    if not is_supported() or not state:
        return
    winreg = _winreg()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _KEY_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(
            key, "ProxyEnable", 0, winreg.REG_DWORD, int(state.get("enable", 0))
        )
        winreg.SetValueEx(
            key, "ProxyServer", 0, winreg.REG_SZ, str(state.get("server", ""))
        )
        winreg.SetValueEx(
            key, "ProxyOverride", 0, winreg.REG_SZ, str(state.get("override", ""))
        )
    _notify()


def _read(key, name, default):
    winreg = _winreg()
    try:
        return winreg.QueryValueEx(key, name)[0]
    except FileNotFoundError:
        return default


def _notify() -> None:
    """Tell apps (WinINet-based: IE/Edge/Office) to re-read the proxy."""
    if not is_supported():
        return
    import ctypes
    wininet = ctypes.windll.wininet
    wininet.InternetSetOptionW(0, _INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
    wininet.InternetSetOptionW(0, _INTERNET_OPTION_REFRESH, 0, 0)
