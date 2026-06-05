"""Single-instance enforcement via QLocalServer (Windows named-pipe).

Why: if the user double-taps the desktop shortcut, opens from Start Menu
while the tray icon is still alive, or fires up the auto-start launch
in parallel with a manual one, we'd end up with two KaproTUN.exe
processes — both racing for the same xray port (2080), both trying to
create a TUN interface called "KaproTun", both fighting the system-proxy
registry. None of that is recoverable; better to detect early.

Pattern: try to connect to a named pipe that the primary instance owns.
If the connection succeeds, we're the second instance — write "show" so
the primary brings its window forward, then exit. If the connection
fails, no primary exists; we become it by listening on the same name.

NOTE — two "KaproTUN.exe" in Task Manager is NORMAL, not a second instance.
The shipped build is a PyInstaller *onefile* exe: launching it starts a
bootloader process (parent) that unpacks the bundle to a temp dir and spawns
the real application as a child — same image name, so Task Manager shows two
`KaproTUN.exe` (and an admin/UAC elevation can add its own consent broker).
That is ONE logical instance: only the child runs the Qt event loop, so only
the child ever calls acquire() and owns the QLocalServer lock. The two engine
controllers can never both manage one sing-box/TUN, because the bootloader
parent runs no Python of ours. A genuine second launch (double-clicked
shortcut, autostart racing a manual start) is what acquire() catches and turns
away — and crucially it does so via this named-pipe handshake, NOT by killing
processes, so a second launch can never tear down the first instance's live
sing-box (orphan cleanup in main.py only runs AFTER we've proven we're the
sole instance).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


# Named pipe handle. QLocalServer scopes pipe names to the current user
# session on Windows, so two different users on the same machine can run
# their own KaproTUN side-by-side without colliding.
SERVER_NAME = "KaproTUN-singleton"

CMD_SHOW = b"show\n"
# Ask the primary instance to quit cleanly. Sent by the installer before a
# reinstall/uninstall so the app disconnects (restoring the system proxy +
# firewall rules) and releases its exe lock, instead of being force-killed.
CMD_QUIT = b"quit\n"

# Short timeouts — if the primary isn't responsive within 500 ms, we
# assume it's gone (orphaned pipe from a crashed previous run, etc.)
# and reclaim the name ourselves.
CONNECT_TIMEOUT_MS = 500
WRITE_TIMEOUT_MS = 500


class SingleInstanceGuard(QObject):
    """One per process. Call acquire() before showing the main window."""

    show_requested = Signal()  # fires when a second instance pinged us
    quit_requested = Signal()  # fires when the installer asks us to quit

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
        # Quit takes precedence: if an installer is asking us to step
        # aside, there's no point also raising the window.
        if CMD_QUIT.strip() in data:
            self.quit_requested.emit()
        elif CMD_SHOW.strip() in data:
            self.show_requested.emit()
