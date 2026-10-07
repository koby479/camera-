"""Only one copy of the program at a time (a tray program that is double-clicked again must bring the existing
window back, not start a second set of connections). Local socket named after the Windows user."""
from __future__ import annotations

import getpass
import time

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


def _name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    return f"UniversalCamViewer-{user}"


def is_running(ask_to_show: bool = False, timeout_ms: int = 700, name: str | None = None) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(name or _name())
    if not sock.waitForConnected(timeout_ms):
        return False
    if ask_to_show:
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def wait_until_gone(seconds: float = 15.0, name: str | None = None) -> None:
    """After an update restart: the old copy needs a moment to finish closing."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and is_running(False, 300, name):
        time.sleep(0.4)


class Listener(QObject):
    show_requested = pyqtSignal()

    def __init__(self, parent=None, name: str | None = None):
        super().__init__(parent)
        self._name = name or _name()
        QLocalServer.removeServer(self._name)          # a leftover from a crashed copy
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        self.ok = self._server.listen(self._name)

    def _on_connection(self):
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            conn.waitForReadyRead(300)
            if bytes(conn.readAll()) == b"show":
                self.show_requested.emit()
            conn.disconnectFromServer()
