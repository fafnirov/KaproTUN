"""Single-instance enforcement via QLocalServer (Windows named-pipe).

Why: if the user double-taps the desktop shortcut, opens from Start Menu
while the tray icon is still alive, or fires up the auto-start launch
in parallel with a manual one, we'd end up with two KaproVPN.exe
processes — both racing for the same xray port (2080), both trying to
create a TUN interface called "KaproTun", both fighting the system-proxy
registry. None of that is recoverable; better to detect early.

Pattern: try to connect to a named pipe that the primary instance owns.
If the connection succeeds, we're the second instance — write "show" so
the primary brings its window forward, then exit. If the connection
fails, no primary exists; we become it by listening on the same name.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


# Named pipe handle. QLocalServer scopes pipe names to the current user
# session on Windows, so two different users on the same machine can run
# their own KaproVPN side-by-side without colliding.
SERVER_NAME = "KaproVPN-singleton"

CMD_SHOW = b"show\n"

# Short timeouts — if the primary isn't responsive within 500 ms, we
# assume it's gone (orphaned pipe from a crashed previous run, etc.)
# and reclaim the name ourselves.
CONNECT_TIMEOUT_MS = 500
WRITE_TIMEOUT_MS = 500


class SingleInstanceGuard(QObject):
    """One per process. Call acquire() before showing the main window."""

    show_requested = Signal()  # fires when a second instance pinged us

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._server: Optional[QLocalServer] = None
        self._is_primary = False

    @property
    def is_primary(self) -> bool:
        return self._is_primary

    def acquire(self) -> bool:
        """Try to claim the primary-instance lock.

        Returns:
          True  — we are the only instance, caller should continue launch.
          False — another instance was already running; we've signalled it
                  to show its window, caller should exit cleanly.
        """
        if self._probe_existing():
            return False

        # No primary exists. Reclaim any stale lock from a crashed
        # previous run, then listen.
        QLocalServer.removeServer(SERVER_NAME)
        self._server = QLocalServer(self)
        if not self._server.listen(SERVER_NAME):
            # Couldn't even bind — extremely rare (firewall, race with
            # another launch). Treat as primary so the app still works,
            # we just won't be able to forward future "show" requests.
            self._is_primary = True
            return True

        self._server.newConnection.connect(self._on_new_connection)
        self._is_primary = True
        return True

    # --- internals --------------------------------------------------------

    def _probe_existing(self) -> bool:
        """True if a primary instance is alive and accepted our 'show'."""
        sock = QLocalSocket()
        sock.connectToServer(SERVER_NAME)
        if not sock.waitForConnected(CONNECT_TIMEOUT_MS):
            return False
        sock.write(CMD_SHOW)
        sock.flush()
        sock.waitForBytesWritten(WRITE_TIMEOUT_MS)
        sock.disconnectFromServer()
        return True

    def _on_new_connection(self) -> None:
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        sock.readyRead.connect(lambda: self._on_data(sock))
        sock.disconnected.connect(sock.deleteLater)

    def _on_data(self, sock: QLocalSocket) -> None:
        data = bytes(sock.readAll())
        if CMD_SHOW.strip() in data:
            self.show_requested.emit()
