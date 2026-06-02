"""Run xray.exe as a managed subprocess."""
from __future__ import annotations

import subprocess
import threading
from collections import deque
from typing import Callable, Optional

from . import paths

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LogSink = Callable[[str], None]


class XrayProcess:
    """Wraps an xray.exe subprocess.

    `on_log` (optional) is invoked from a background reader thread for every
    line of xray's combined stdout+stderr — caller marshals to UI thread.
    """

    def __init__(self, on_log: Optional[LogSink] = None, log_buffer: int = 500):
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._on_log = on_log
        self._recent: deque[str] = deque(maxlen=log_buffer)
        self._lock = threading.Lock()

    # --- lifecycle --------------------------------------------------------

    # Rotate xray.log when it grows past this size. ~1 MB is generous
    # for a single session but stops the file from accumulating
    # multi-GB over months of uninterrupted use. Old log is renamed
    # xray.log.1 so users still have one backup for post-mortem.
    MAX_LOG_BYTES = 1_048_576  # 1 MiB

    def start(self, config_path: str) -> None:
        if self.is_running():
            raise RuntimeError("xray is already running")
        exe = paths.xray_exe()
        if not exe.is_file():
            raise FileNotFoundError(f"xray.exe not found at {exe}")

        # Rotate xray.log if it's grown unreasonably between sessions.
        # xray appends to the same file across runs, so without this
        # the file can creep up over months. Cheap (one stat + maybe
        # one rename), runs once per connect.
        self._rotate_log_if_needed()

        self._proc = subprocess.Popen(
            [str(exe), "run", "-c", config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            cwd=str(paths.xray_dir()),  # cwd matters: xray loads geoip.dat/geosite.dat from here
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _rotate_log_if_needed(self) -> None:
        """If xray.log is over MAX_LOG_BYTES, rename to xray.log.1
        (replacing any existing .1). Single-backup strategy — we don't
        keep a chain, just the last full file.
        """
        log_path = paths.log_file()
        try:
            if not log_path.is_file():
                return
            if log_path.stat().st_size <= self.MAX_LOG_BYTES:
                return
            backup = log_path.with_suffix(log_path.suffix + ".1")
            if backup.exists():
                try:
                    backup.unlink()
                except OSError:
                    return
            log_path.replace(backup)
        except OSError:
            # Best-effort — if rotation fails (file locked by another
            # process etc.) just leave the file alone, xray will
            # continue appending.
            pass

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

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def pid(self) -> Optional[int]:
        """OS pid of the running xray process, or None — for the memory
        watchdog to sample its private memory / handle count."""
        proc = self._proc
        return proc.pid if (proc is not None and proc.poll() is None) else None

    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc else None

    # --- validation -------------------------------------------------------

    @staticmethod
    def check_config(config_path: str) -> tuple[bool, str]:
        """Run `xray run -test -c <path>`. Returns (ok, message)."""
        exe = paths.xray_exe()
        if not exe.is_file():
            return False, f"xray.exe not found at {exe}"
        try:
            result = subprocess.run(
                [str(exe), "run", "-test", "-c", config_path],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW,
                cwd=str(paths.xray_dir()),
            )
            if result.returncode == 0:
                return True, "OK"
            return False, (result.stderr or result.stdout or "Unknown error").strip()
        except Exception as e:
            return False, str(e)

    # --- logs -------------------------------------------------------------

    def recent_logs(self) -> list[str]:
        with self._lock:
            return list(self._recent)

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            with self._lock:
                self._recent.append(line)
            if self._on_log:
                try:
                    self._on_log(line)
                except Exception:
                    pass
