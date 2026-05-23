"""Connection controller: ties together config generation, sing-box, and system proxy."""
from __future__ import annotations

from typing import Callable, Optional

from . import singbox_config, storage, system_proxy
from .parser import ProxyConfig
from .singbox_process import SingboxProcess


class ConnectionError(Exception):
    pass


class ConnectionManager:
    """Single source of truth for the connect/disconnect lifecycle."""

    def __init__(self, on_log: Optional[Callable[[str], None]] = None):
        self.process = SingboxProcess(on_log=on_log)
        self.settings = storage.load_settings()
        self._saved_proxy_state: Optional[dict] = None
        self._active: Optional[ProxyConfig] = None

    # --- public API -------------------------------------------------------

    def connect(self, config: ProxyConfig, direct_domains: list[str]) -> None:
        if self.is_connected():
            raise ConnectionError("Already connected. Disconnect first.")

        host = str(self.settings.get("listen_host", "127.0.0.1"))
        port = int(self.settings.get("listen_port", 2080))

        path = singbox_config.write_config(config, direct_domains, host, port)

        ok, msg = SingboxProcess.check_config(path)
        if not ok:
            raise ConnectionError(f"sing-box rejected the config:\n{msg}")

        try:
            self.process.start(path)
        except Exception as e:
            raise ConnectionError(f"Failed to start sing-box: {e}") from e

        if self.settings.get("auto_set_system_proxy", True):
            self._saved_proxy_state = system_proxy.get_state()
            try:
                system_proxy.set_proxy(host, port)
            except Exception as e:
                # Roll back the subprocess so we don't leave a dangling sing-box.
                self.process.stop()
                self._saved_proxy_state = None
                raise ConnectionError(
                    f"Started sing-box but failed to set system proxy: {e}"
                ) from e

        self._active = config

    def disconnect(self) -> None:
        if self._saved_proxy_state is not None:
            try:
                system_proxy.restore(self._saved_proxy_state)
            finally:
                self._saved_proxy_state = None
        self.process.stop()
        self._active = None

    def is_connected(self) -> bool:
        return self.process.is_running()

    def active_config(self) -> Optional[ProxyConfig]:
        return self._active if self.is_connected() else None

    def update_settings(self, **changes) -> None:
        self.settings.update(changes)
        storage.save_settings(self.settings)
