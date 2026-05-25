r"""WireGuard via the official WireGuard for Windows service.

Why this exists separately from xray:

  Xray-core ships its own user-space WireGuard outbound (gVisor netstack).
  It works on paper, but in practice we hit a wall trying to make it pass
  real traffic from RU networks — the kind of intermittent silent failure
  that's untestable on a developer machine outside the affected region.

  The official WireGuard for Windows client uses tunnel.dll (a Go
  implementation) running as a Windows service, with WinTUN as the
  network driver. It's the upstream reference implementation, used by
  every popular Windows VPN GUI (Hiddify, Nekoray, NekoBox, etc).
  Reliability is on a different level.

So: instead of replicating WG inside our process, we drive the official
client via its CLI:

  wireguard.exe /installtunnelservice  <path-to-conf>
    → registers a Windows service named "WireGuardTunnel$<basename>"
    → service starts, creates the TUN interface, runs handshake,
      sets up routes per AllowedIPs

  wireguard.exe /uninstalltunnelservice <basename>
    → stops + removes the service, tears down interface and routes

Our config file lives at:
  %LOCALAPPDATA%\KaproVPN\wg\<name>.conf

Service basename = stem of that file = our chosen tunnel name.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from . import paths

# Pinned MSI version we extract binaries from. Bump after manual smoke-test
# of a newer release (WireGuard for Windows is API-stable across versions
# but we pin to avoid surprises).
WIREGUARD_MSI_VERSION = "0.5.3"
WIREGUARD_MSI_FILENAME = f"wireguard-amd64-{WIREGUARD_MSI_VERSION}.msi"

# Upstream MSI URL — used as fallback if our mirror is unreachable.
WIREGUARD_MSI_UPSTREAM = (
    f"https://download.wireguard.com/windows-client/{WIREGUARD_MSI_FILENAME}"
)
# Our mirror (server-setup/sync-binaries.sh re-hosts the MSI). Primary
# because it's geographically closer + we control its uptime.
WIREGUARD_MSI_MIRROR = f"https://files.kaprovpn.pro/{WIREGUARD_MSI_FILENAME}"

# Legacy system install path — checked LAST as a fallback for users who
# already had WireGuard for Windows installed (e.g. from KaproVPN v1.3.1
# which used to install the MSI). New installs go to our portable path
# under app_data_dir() / "wg" / "bin" instead.
LEGACY_SYSTEM_EXE = r"C:\Program Files\WireGuard\wireguard.exe"

DOWNLOAD_URL = WIREGUARD_MSI_UPSTREAM  # legacy alias, kept for callers

# Bypass system proxy on internal downloads — see xray_installer for
# full rationale (stale registry proxy from crashed HTTP-mode sessions).
_NO_PROXY = {"http": "", "https": ""}

# Hidden-subprocess flag — without it Windows pops a fleeting console
# window for every wireguard.exe call.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ProgressCb = Optional[Callable[[int, int], None]]


def wg_dir() -> Path:
    """Where our generated WG .conf files live."""
    p = paths.app_data_dir() / "wg"
    p.mkdir(parents=True, exist_ok=True)
    return p


def wg_bin_dir() -> Path:
    """Where the portable WireGuard binaries live (our private copy).

    Windows SCM uses the path of the exe that called /installtunnelservice
    as the service binary path, so this can be anywhere on disk — no need
    to live in C:\\Program Files. LOCALAPPDATA is readable by SYSTEM
    (which the registered tunnel service runs as), so this works for
    service registration too.
    """
    p = wg_dir() / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_wireguard_exe() -> Optional[Path]:
    """Locate wireguard.exe. Checks our portable install first, then
    legacy system install paths.

    Order of precedence:
      1. $WIREGUARD_EXE env var (manual override for advanced users)
      2. Our portable copy under %LOCALAPPDATA%\\KaproVPN\\wg\\bin
      3. C:\\Program Files\\WireGuard (legacy — left over from v1.3.1
         or from the user installing wireguard.com MSI manually)
      4. PATH (if user put wireguard.exe somewhere on PATH)

    Returns None if not found anywhere.
    """
    if sys.platform != "win32":
        return None
    env_override = os.environ.get("WIREGUARD_EXE")
    if env_override and Path(env_override).is_file():
        return Path(env_override)
    portable = wg_bin_dir() / "wireguard.exe"
    if portable.is_file():
        return portable
    if Path(LEGACY_SYSTEM_EXE).is_file():
        return Path(LEGACY_SYSTEM_EXE)
    found = shutil.which("wireguard.exe")
    if found:
        return Path(found)
    return None


def is_installed() -> bool:
    return find_wireguard_exe() is not None


# ---------------------------------------------------------------- silent install

def _download_msi(progress: ProgressCb = None) -> bytes:
    """Fetch the WireGuard MSI: mirror first, upstream fallback.

    Mirror is faster + more reliable from RU. Upstream is the safety
    net if our VPS is down. 2 attempts per source, 4 chances total.
    """
    last_err: Optional[Exception] = None
    for url in (WIREGUARD_MSI_MIRROR, WIREGUARD_MSI_UPSTREAM):
        for attempt in range(2):
            try:
                sink = io.BytesIO()
                downloaded = 0
                with requests.get(url, stream=True, timeout=(10, 30),
                                  proxies=_NO_PROXY) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length", 0))
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        sink.write(chunk)
                        downloaded += len(chunk)
                        if progress:
                            progress(downloaded, total)
                data = sink.getvalue()
                # Sanity: MSI is ~5-8 MB; anything tiny is a captive-
                # portal HTML or a broken truncated download.
                if len(data) < 500_000:
                    raise RuntimeError(
                        f"Suspiciously small download ({len(data)} B) — "
                        f"probably a 404 page or captive portal"
                    )
                return data
            except (requests.exceptions.RequestException, OSError, RuntimeError) as e:
                last_err = e
    raise RuntimeError(
        f"Не удалось скачать WireGuard MSI ни с зеркала, ни с upstream: {last_err}"
    )


_PORTABLE_BINARIES = ("wireguard.exe", "tunnel.dll", "wg.exe", "wintun.dll")


def portable_install(progress: ProgressCb = None) -> None:
    """Download the WireGuard MSI and extract its binaries to OUR path.

    Does NOT run msiexec /i (system install). Instead runs
    msiexec /a (administrative install) which unpacks files to a temp
    directory without touching the system: no service registration,
    no Programs & Features entry, no Start Menu shortcut.

    We then copy just the four files we need into
    %LOCALAPPDATA%\\KaproVPN\\wg\\bin\\ and discard the rest.

    Requires admin because msiexec /a still needs it for some MSIs;
    even when it doesn't, the controller has already gated on
    admin.is_admin() before reaching here (WG needs admin to register
    the service later anyway).
    """
    if sys.platform != "win32":
        raise RuntimeError("WireGuard portable install is Windows-only")

    raw = _download_msi(progress=progress)

    fd, msi_path = tempfile.mkstemp(suffix=".msi", prefix="kaprovpn-wg-")
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)

    # msiexec /a wants a target dir; it'll create a "PFiles\WireGuard\"
    # subtree inside (mirroring where it would normally install).
    extract_dir = Path(tempfile.mkdtemp(prefix="kaprovpn-wg-extract-"))
    try:
        proc = subprocess.run(
            ["msiexec", "/a", msi_path, "/qn",
             f"TARGETDIR={extract_dir}"],
            capture_output=True, timeout=180,
            creationflags=_NO_WINDOW,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"msiexec /a вернул {proc.returncode}. "
                f"stderr: {(proc.stderr or b'').decode('utf-8', errors='replace')[:200]}"
            )

        # Walk the extracted tree, find each of our needed binaries.
        # MSI layout is TARGETDIR\PFiles\WireGuard\<files> typically.
        found: dict[str, Path] = {}
        for path in extract_dir.rglob("*"):
            name = path.name.lower()
            if path.is_file() and name in _PORTABLE_BINARIES:
                # Prefer matches deeper in the tree (more specific
                # WireGuard subfolder) over shallow ones if there's
                # a duplicate — irrelevant for current MSI layout but
                # robust against future repacks.
                found[name] = path

        missing = [n for n in _PORTABLE_BINARIES if n not in found]
        if missing:
            raise RuntimeError(
                f"WireGuard MSI распакован, но не нашлись: {', '.join(missing)}. "
                f"Содержимое: {[p.name for p in extract_dir.rglob('*') if p.is_file()][:10]}"
            )

        # Copy into our portable bin dir. Atomic-ish per file — if we
        # crash mid-copy the user gets a re-install on next launch.
        target_dir = wg_bin_dir()
        for name in _PORTABLE_BINARIES:
            shutil.copy2(found[name], target_dir / name)

    finally:
        # Clean up both the temp MSI and the extract dir — they're
        # ~10 MB each, no need to leave them around.
        try:
            os.remove(msi_path)
        except OSError:
            pass
        try:
            shutil.rmtree(extract_dir, ignore_errors=True)
        except OSError:
            pass

    # Sanity check.
    if not is_installed():
        raise RuntimeError(
            "Файлы скопировались, но wireguard.exe не находится. "
            "Возможно, антивирус удалил его — временно отключи защиту "
            "реального времени и попробуй снова."
        )


def ensure_installed(progress: ProgressCb = None) -> None:
    """Idempotent: install only if missing.

    Detects both our portable install and a legacy system install
    (from v1.3.1 or wireguard.com manual install) as "already installed"
    — no need to download MSI again in either case.
    """
    if is_installed():
        return
    portable_install(progress=progress)


# Backwards-compat shim for any caller still using the old name.
def silent_install(progress: ProgressCb = None) -> None:
    portable_install(progress=progress)


def get_installed_version() -> Optional[str]:
    exe = find_wireguard_exe()
    if exe is None:
        return None
    # WireGuard for Windows doesn't expose --version cleanly, but the
    # binary's file-info has it. PowerShell one-liner gives a clean
    # string; cheap enough.
    #
    # errors="replace" is critical on RU Windows — without it, any
    # cp1251 byte we can't decode (Russian-locale PowerShell output
    # leaks them sometimes) crashes the subprocess reader thread.
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
        return (proc.stdout or "").strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- naming

_TUNNEL_NAME_INVALID = re.compile(r"[^A-Za-z0-9_-]+")


def sanitize_tunnel_name(name: str) -> str:
    """Make a name safe for use as a Windows-service component.

    Service names like "WireGuardTunnel$<name>" reject spaces and most
    punctuation. We strip down to [A-Za-z0-9_-] and prepend "KaproVPN-"
    so we can recognise our own tunnels in `services.msc` and clean
    them up safely without touching tunnels the user created via the
    WireGuard GUI directly.
    """
    cleaned = _TUNNEL_NAME_INVALID.sub("-", name).strip("-_")
    if not cleaned:
        cleaned = "tunnel"
    # Service-name + tunnel-name length cap is ~63 chars.
    return f"KaproVPN-{cleaned}"[:63]


def conf_path_for(tunnel_name: str) -> Path:
    return wg_dir() / f"{tunnel_name}.conf"


# ---------------------------------------------------------------- service ops

class WireGuardError(Exception):
    """Raised when wireguard.exe refuses an install/uninstall call."""


def install_tunnel(conf_text: str, tunnel_name: str) -> Path:
    """Write the .conf to disk and register it as a Windows service.

    Returns the path of the on-disk .conf (kept for the service to
    re-read on reboot). Caller is responsible for routes — WireGuard
    only handles the AllowedIPs-based defaults; KaproVPN's
    network_routes adds bypass entries on top.

    Raises WireGuardError on failure (missing WireGuard.exe, refused
    install, malformed config).
    """
    exe = find_wireguard_exe()
    if exe is None:
        raise WireGuardError(
            "WireGuard для Windows не установлен.\n"
            f"Скачай: {DOWNLOAD_URL}\n"
            "Поставь (10 МБ, ~30 секунд), потом снова жми «Подключить»."
        )

    conf_file = conf_path_for(tunnel_name)
    # Hardening: make sure no stale conf from a previous run with the
    # same tunnel name is hanging around — would silently get used.
    conf_file.write_text(conf_text, encoding="utf-8")

    # /installtunnelservice is the official, documented CLI verb.
    # It requires admin privileges; controller has already gated on
    # admin.is_admin() before calling us.
    #
    # Use bytes (no text=True) — wireguard.exe stderr on RU Windows
    # contains cp1251 bytes that crash Python's utf-8 reader thread.
    # Decode at the error site with errors="replace" so we never lose
    # the returncode just because the message was un-decodable.
    proc = subprocess.run(
        [str(exe), "/installtunnelservice", str(conf_file)],
        capture_output=True, timeout=15,
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode(
            "utf-8", errors="replace",
        ).strip() or "(no output)"
        raise WireGuardError(
            f"wireguard.exe вернул ошибку (rc={proc.returncode}):\n{err}"
        )
    return conf_file


def uninstall_tunnel(tunnel_name: str) -> None:
    """Stop and remove the service. Best-effort — if it's already gone,
    we don't care.
    """
    exe = find_wireguard_exe()
    if exe is None:
        # Nothing to do — the service can't exist without the exe.
        return
    # bytes mode, see install_tunnel for rationale.
    try:
        subprocess.run(
            [str(exe), "/uninstalltunnelservice", tunnel_name],
            capture_output=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    # Don't delete the .conf — keeps "what was the last config" intact
    # for the user to inspect if something went wrong.


def is_tunnel_active(tunnel_name: str) -> bool:
    """True if the Windows service for this tunnel is in Running state."""
    return get_tunnel_status(tunnel_name) == "Running"


def get_tunnel_status(tunnel_name: str) -> str:
    """Last-known status of the service: 'Running' / 'Stopped' /
    'StartPending' / 'StopPending' / '' (no such service).

    Locale-independent: uses Get-Service which returns the
    ServiceControllerStatus enum value (always English) regardless
    of system locale, unlike `sc query` which prints localized text.

    NB on PowerShell escaping: in single-quoted strings the dollar
    sign is a LITERAL character (variable expansion only happens
    inside double-quoted strings). So we pass the service name
    straight, no backtick-escape — earlier versions added a backtick
    before $ which made PowerShell search for a name with a literal
    backtick in it and find nothing.
    """
    service_name = f"WireGuardTunnel${tunnel_name}"
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"(Get-Service -Name '{service_name}' -ErrorAction SilentlyContinue).Status"],
            capture_output=True, timeout=5,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or b"").decode("utf-8", errors="replace").strip()


def wait_for_tunnel_up(tunnel_name: str, timeout: float = 10.0) -> bool:
    """Block until the service reports Running (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_tunnel_active(tunnel_name):
            return True
        time.sleep(0.3)
    return False


def list_kaprovpn_tunnels() -> list[str]:
    """Names of currently-installed KaproVPN tunnel services.

    Useful for startup cleanup — if a previous run crashed without
    uninstalling its tunnel, this lets us find and remove it before
    starting a new one.

    PowerShell again to dodge sc's localization: Get-Service returns
    the Name property as the actual service name (locale-independent),
    while `sc query` writes "ИМЯ_СЛУЖБЫ:" instead of "SERVICE_NAME:"
    on Russian Windows — breaking the v1.3.0–1.3.2 string match.
    """
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-Service -Name 'WireGuardTunnel$KaproVPN-*' "
             "-ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Name"],
            capture_output=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    found = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("WireGuardTunnel$KaproVPN-"):
            name = line.split("WireGuardTunnel$", 1)[1].strip()
            found.append(name)
    return found


def cleanup_orphan_tunnels() -> int:
    """Remove any leftover KaproVPN-* tunnels from prior crashed runs.

    Like the orphan-killer in main.py but for WireGuard services.
    Returns the count cleaned up.
    """
    orphans = list_kaprovpn_tunnels()
    for name in orphans:
        uninstall_tunnel(name)
    return len(orphans)
