"""Run tun2socks as a managed subprocess (cross-platform).

tun2socks creates the OS-level TUN device and forwards all IP traffic
from it into xray's SOCKS5 inbound. Combined with default-route changes,
this gives system-wide tunneling for every app (Telegram, Steam, games).

Per-OS device specification:
  Windows  -device wintun://KaproTun    (uses WinTUN driver, dll alongside)
  macOS    -device utun                 (let kernel auto-assign utunN)
  Linux    -device tun://kaprotun       (we choose the name, kernel creates it)

The interface naming is what we look up later via the platform's route
manager to set IP / metric / DNS on it.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from typing import Callable, Optional

from . import paths

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LogSink = Callable[[str], None]

# Friendly name for our TUN interface. On Windows + Linux we choose it,
# on macOS the kernel picks (we capture the chosen utunN name from tun2socks'
# startup log lines).
TUN_DEVICE_NAME = "KaproTun" if sys.platform == "win32" else "kaprotun"


def _device_arg() -> str:
    """The right -device URI for our OS."""
    if sys.platform == "win32":
        return f"wintun://{TUN_DEVICE_NAME}"
    if sys.platform == "darwin":
        # macOS kernel insists on naming utunN itself; passing a fixed
        # name would error. Letting it pick is fine — we discover the
        # actual name afterward via getifaddrs.
        return "utun"
    # Linux — we choose the name and the kernel creates it.
    return f"tun://{TUN_DEVICE_NAME}"


class Tun2socksProcess:
    """Wraps tun2socks as a subprocess."""

    def __init__(self, on_log: Optional[LogSink] = None, log_buffer: int = 500):
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._on_log = on_log
        self._recent: deque[str] = deque(maxlen=log_buffer)
        self._lock = threading.Lock()
        # macOS-only: the kernel-assigned utunN name, captured from logs
        # once the process starts. None until tun2socks announces it.
        self._mac_device_name: Optional[str] = None

    def start(self, socks_addr: str = "127.0.0.1:2081",
              mtu: int = 1500, loglevel: str = "info") -> None:
        if self.is_running():
            raise RuntimeError("tun2socks is already running")
        exe = paths.tun2socks_exe()
        if not exe.is_file():
            raise FileNotFoundError(f"tun2socks binary not found at {exe}")
        if sys.platform == "win32" and not paths.wintun_dll().is_file():
            raise FileNotFoundError(f"wintun.dll not found at {paths.wintun_dll()}")

        # tun2socks needs to find WinTUN.dll alongside on Windows; we set
        # cwd to its directory. On Unix cwd doesn't matter for binary
        # resolution but we keep it for log/temp file consistency.
        self._proc = subprocess.Popen(
            [
                str(exe),
                "-device", _device_arg(),
                "-proxy", f"socks5://{socks_addr}",
                "-loglevel", loglevel,
                "-mtu", str(mtu),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            cwd=str(paths.tun_dir()),
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def stop(self, timeout: float = 3.0) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        self._proc = None
        self._mac_device_name = None

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc else None

    def recent_logs(self) -> list[str]:
        with self._lock:
            return list(self._recent)

    def mac_device_name(self) -> Optional[str]:
        """macOS-only: actual utunN name the kernel assigned, or None
        until tun2socks has announced it.
        """
        return self._mac_device_name

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            with self._lock:
                self._recent.append(line)
            # Capture the macOS-assigned device name from log lines like
            #   "INFO[0000] [STACK] tun://utun5 <-> socks5://127.0.0.1:2081"
            if sys.platform == "darwin" and self._mac_device_name is None:
                marker = "utun"
                idx = line.find(marker)
                while idx != -1:
                    # extract a contiguous "utun" + digits
                    j = idx + len(marker)
                    while j < len(line) and line[j].isdigit():
                        j += 1
                    name = line[idx:j]
                    if name != "utun":  # must have a number
                        self._mac_device_name = name
                        break
                    idx = line.find(marker, idx + 1)
            if self._on_log:
                try:
                    self._on_log(line)
                except Exception:
                    pass
