"""Run sing-box.exe as a managed subprocess."""
from __future__ import annotations

import subprocess
import threading
from collections import deque
from typing import Callable, Optional

from . import paths

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

LogSink = Callable[[str], None]


class SingboxProcess:
    """Wraps a sing-box.exe subprocess.

    `on_log` (optional) is invoked from a background thread for every line of
    sing-box's stderr — caller is responsible for marshalling to the UI thread.
    """

    def __init__(self, on_log: Optional[LogSink] = None, log_buffer: int = 500):
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._on_log = on_log
        self._recent: deque[str] = deque(maxlen=log_buffer)
        self._lock = threading.Lock()

    # --- lifecycle --------------------------------------------------------

    def start(self, config_path: str) -> None:
        if self.is_running():
            raise RuntimeError("sing-box is already running")
        exe = paths.singbox_exe()
        if not exe.is_file():
            raise FileNotFoundError(f"sing-box.exe not found at {exe}")

        self._proc = subprocess.Popen(
            [str(exe), "run", "-c", config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            cwd=str(paths.singbox_dir()),
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

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc else None

    # --- validation -------------------------------------------------------

    @staticmethod
    def check_config(config_path: str) -> tuple[bool, str]:
        """Run `sing-box check -c <path>`. Returns (ok, message)."""
        exe = paths.singbox_exe()
        if not exe.is_file():
            return False, f"sing-box.exe not found at {exe}"
        try:
            result = subprocess.run(
                [str(exe), "check", "-c", config_path],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW,
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
