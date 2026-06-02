"""Common filesystem paths for the application.

Per-platform conventions:
  - Windows: %LOCALAPPDATA%\\KaproTUN
  - macOS:   ~/Library/Application Support/KaproTUN
  - Linux:   $XDG_DATA_HOME/KaproTUN  (defaults to ~/.local/share/KaproTUN)

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


# App folder name. Renamed from "KaproVPN" → "KaproTUN" in v1.22.0; the
# legacy name is migrated once on first launch so existing users keep their
# saved servers, settings, secrets and downloaded binaries.
_APP_DIR_NAME = "KaproTUN"
_LEGACY_DIR_NAME = "KaproVPN"
_migrated = False


def _app_data_base() -> Path:
    """The platform's per-user data root (the parent of our app folder)."""
    if _is_windows():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base)
    if _is_macos():
        return Path.home() / "Library" / "Application Support"
    # Linux/BSD — follow the XDG Base Directory spec
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base)


def _migrate_legacy_dir(base: Path, new_dir: Path) -> None:
    """One-time, best-effort move of the old KaproVPN folder to KaproTUN.

    Runs only when the legacy folder exists and the new one is absent or
    empty — so it never clobbers a fresh KaproTUN install, and never runs
    twice (a no-op once the rename has happened). Failure is swallowed:
    losing the migration just means a user re-adds servers, which must
    never be allowed to block startup.
    """
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        legacy = base / _LEGACY_DIR_NAME
        if not legacy.is_dir() or legacy == new_dir:
            return
        new_has_data = new_dir.is_dir() and any(new_dir.iterdir())
        if new_has_data:
            return  # fresh KaproTUN already in use — don't touch it
        if new_dir.exists():
            try:
                new_dir.rmdir()  # empty placeholder — clear it so rename can land
            except OSError:
                pass
        if not new_dir.exists():
            legacy.rename(new_dir)
        else:
            # Couldn't clear the placeholder — fall back to per-item move.
            import shutil
            for item in legacy.iterdir():
                dest = new_dir / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
    except Exception:
        pass  # best-effort; never block startup on migration


def app_data_dir() -> Path:
    """Per-user data directory, platform-appropriate.

    Windows: %LOCALAPPDATA%\\KaproTUN  (e.g. C:/Users/<u>/AppData/Local/KaproTUN)
    macOS:   ~/Library/Application Support/KaproTUN
    Linux:   $XDG_DATA_HOME/KaproTUN  (defaults to ~/.local/share/KaproTUN)

    On first call it migrates the pre-rename "KaproVPN" folder if present.
    """
    base = _app_data_base()
    path = base / _APP_DIR_NAME
    _migrate_legacy_dir(base, path)
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


def app_log_file() -> Path:
    """Rotating runtime log for the GUI/watchdog/connect lifecycle (NOT the raw
    xray/tun2socks stream — that's log_file()). Diagnostic events only, with
    secrets redacted. See core/app_log.py."""
    return app_data_dir() / "app.log"


def tun_recovery_file() -> Path:
    """Crash-recovery journal for TUN mode. While a TUN session holds the
    physical NIC's DNS cleared (leak protection), we record which interface
    was touched here. On the next startup, recover() reads this to restore
    DNS-to-DHCP if the previous run died before disconnect could. Deleted on
    a clean disconnect, so its mere presence means 'a TUN session is/was live'.
    """
    return app_data_dir() / "tun-session.json"


def secrets_file() -> Path:
    """Encrypted-at-rest blob for non-config secrets — subscription URLs and
    the last-seen Subscription-Userinfo. Uses the same crypto as
    configs.json (see secrets_store). Kept OUT of settings.json so a
    casually-shared settings file never leaks a paid subscription link.
    """
    return app_data_dir() / "secrets.json"


def harden_file_perms(path: Path) -> bool:
    """Restrict a sensitive file to the current user. Best-effort defence in
    depth (the encryption / DPAPI layer is the real protection).

    POSIX: chmod 0600 (owner read/write only) — same as ``~/.ssh/id_*``.
    Windows: %LOCALAPPDATA% already carries a user-only ACL that children
    inherit, so there's nothing to tighten; we return True (already private).

    Returns True if perms are (now) user-only, False if the chmod failed —
    so a caller writing a credential file can log the miss instead of
    assuming it's locked down.
    """
    if os.name != "posix":
        return True  # %LOCALAPPDATA% is per-user ACL'd on Windows
    try:
        os.chmod(path, 0o600)
        return True
    except OSError:
        return False


def write_secure_text(path: Path, text: str) -> Path:
    """Atomically write `text` to `path` with user-only permissions.

    For runtime config files (xray-runtime.json, hysteria-client.yaml) that
    embed UUIDs / passwords / auth: perms are tightened on the temp file
    BEFORE the rename, so the credential file is never briefly world-readable.
    Returns the path. NEVER log `text` — it contains secrets.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(text.encode("utf-8"))
        harden_file_perms(tmp)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    harden_file_perms(path)
    return path


def remove_runtime_configs() -> list[str]:
    """Delete the on-disk runtime configs (xray + hysteria) after their
    processes have stopped. Call on every disconnect/exit so credentials
    don't linger at rest.

    Returns the names that could NOT be removed (a credential left on disk)
    so the caller can surface it instead of hiding the failure. Already-gone
    files count as success.
    """
    failed: list[str] = []
    for f in (runtime_config_file(), hysteria_config_file()):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            if f.exists():
                failed.append(f.name)
    return failed


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
