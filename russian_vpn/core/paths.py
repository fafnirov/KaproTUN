"""Common filesystem paths for the application."""
import os
from pathlib import Path


def app_data_dir() -> Path:
    """Per-user data directory, e.g. C:/Users/<user>/AppData/Local/russian-vpn."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    path = Path(base) / "russian-vpn"
    path.mkdir(parents=True, exist_ok=True)
    return path


def singbox_dir() -> Path:
    path = app_data_dir() / "singbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def singbox_exe() -> Path:
    return singbox_dir() / "sing-box.exe"


def configs_file() -> Path:
    return app_data_dir() / "configs.json"


def sites_file() -> Path:
    return app_data_dir() / "sites.json"


def settings_file() -> Path:
    return app_data_dir() / "settings.json"


def runtime_config_file() -> Path:
    """Generated sing-box JSON config written before each launch."""
    return app_data_dir() / "singbox-runtime.json"


def log_file() -> Path:
    return app_data_dir() / "singbox.log"


def bundled_default_sites() -> Path:
    """Default sites list shipped with the app (read-only fallback)."""
    return Path(__file__).resolve().parent.parent / "data" / "default_sites.json"
