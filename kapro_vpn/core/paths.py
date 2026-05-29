"""Common filesystem paths for the application.

Per-platform conventions:
  - Windows: %LOCALAPPDATA%\\KaproVPN
  - macOS:   ~/Library/Application Support/KaproVPN
  - Linux:   $XDG_DATA_HOME/KaproVPN  (defaults to ~/.local/share/KaproVPN)

The xray binary is named differently on each OS (xray.exe vs plain xray),
so xray_exe() handles that. TUN-related binaries (tun2socks, wintun.dll)
only exist on Windows — the helper paths still return values on other
platforms so callers can use them in equality checks, but they live in
the same Windows-only TUN directory which will simply be empty/unused
on macOS/Linux until we ship a TUN implementation for those.
"""
import os
import sys
from pathlib import Path


def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def app_data_dir() -> Path:
    """Per-user data directory, platform-appropriate.

    Windows: %LOCALAPPDATA%\\KaproVPN  (e.g. C:/Users/<u>/AppData/Local/KaproVPN)
    macOS:   ~/Library/Application Support/KaproVPN
    Linux:   $XDG_DATA_HOME/KaproVPN  (defaults to ~/.local/share/KaproVPN)
    """
    if _is_windows():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "KaproVPN"
    elif _is_macos():
        path = Path.home() / "Library" / "Application Support" / "KaproVPN"
    else:
        # Linux/BSD — follow the XDG Base Directory spec
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / "KaproVPN"
    path.mkdir(parents=True, exist_ok=True)
    return path


def xray_dir() -> Path:
    path = app_data_dir() / "xray"
    path.mkdir(parents=True, exist_ok=True)
    return path


def xray_exe() -> Path:
    """Path to the xray binary — .exe suffix only on Windows."""
    name = "xray.exe" if _is_windows() else "xray"
    return xray_dir() / name


def tun_dir() -> Path:
    """Houses tun2socks + WinTUN driver. Currently used only on Windows;
    kept here so callers can still reference the path on other OSes
    (it'll just be empty until we add a non-Windows TUN implementation).
    """
    path = app_data_dir() / "tun"
    path.mkdir(parents=True, exist_ok=True)
    return path


def tun2socks_exe() -> Path:
    name = "tun2socks.exe" if _is_windows() else "tun2socks"
    return tun_dir() / name


def wintun_dll() -> Path:
    # Windows-only file; returning the path on other OSes is harmless,
    # nobody should ever check is_file() on it from non-Windows code.
    return tun_dir() / "wintun.dll"


def hysteria_dir() -> Path:
    """Houses the hysteria client binary (Hysteria2 transport).

    Xray-core can't speak Hysteria2, so for hy2 configs we run the
    standalone `hysteria` client as a local SOCKS5 proxy and chain xray
    through it — same helper-process pattern as tun2socks.
    """
    path = app_data_dir() / "hysteria"
    path.mkdir(parents=True, exist_ok=True)
    return path


def hysteria_exe() -> Path:
    name = "hysteria.exe" if _is_windows() else "hysteria"
    return hysteria_dir() / name


def hysteria_config_file() -> Path:
    """Generated hysteria client config (JSON content in a .yaml file —
    valid JSON is valid YAML, so hysteria's loader reads it without us
    needing a YAML serializer)."""
    return hysteria_dir() / "hysteria-client.yaml"


def configs_file() -> Path:
    return app_data_dir() / "configs.json"


def sites_file() -> Path:
    return app_data_dir() / "sites.json"


def settings_file() -> Path:
    return app_data_dir() / "settings.json"


def runtime_config_file() -> Path:
    """Generated xray JSON config written before each launch."""
    return app_data_dir() / "xray-runtime.json"


def logs_dir() -> Path:
    """Folder for diagnostic logs (startup crash dumps, etc.)."""
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_file() -> Path:
    return app_data_dir() / "xray.log"


def access_log_file() -> Path:
    """xray writes per-request lines here."""
    return app_data_dir() / "xray-access.log"


def bundled_default_sites() -> Path:
    """Default sites list shipped with the app (read-only fallback)."""
    return Path(__file__).resolve().parent.parent / "data" / "default_sites.json"
