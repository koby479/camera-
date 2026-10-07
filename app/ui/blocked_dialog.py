"""Full-screen blocking dialog shown when the server returns status='blocked'.
Cannot be closed with X. The only way out is 'exit' or 'retry' (which re-checks the server)."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from app.core import telemetry


class BlockedDialog(QDialog):
    def __init__(self, message: str = "", parent=None):
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.CustomizeWindowHint |
                         Qt.WindowType.WindowTitleHint)
        self.setWindowTitle("גישה חסומה")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setMinimumSize(460, 220)

        icon = QLabel("🔒")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size:64px;")

        text = QLabel(message or telemetry.BLOCK_MSG_DEFAULT)
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setStyleSheet("font-size:15px; padding:8px;")

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setStyleSheet("color:#9fb4ff;")

        btns = QDialogButtonBox()
        retry = btns.addButton("בדוק שוב (נסה לאחר פתיחת הגישה)", QDialogButtonBox.ButtonRole.AcceptRole)
        quit_ = btns.addButton("יציאה", QDialogButtonBox.ButtonRole.RejectRole)
        retry.clicked.connect(self._retry)
        quit_.clicked.connect(self._quit)

        lay = QVBoxLayout(self)
        lay.addWidget(icon)
        lay.addWidget(text)
        lay.addWidget(self.status)
        lay.addWidget(btns)

        # auto-retry every 5 minutes so the dialog responds without the user pressing anything
        self._auto = QTimer(self)
        self._auto.setInterval(5 * 60 * 1000)
        self._auto.timeout.connect(self._retry)
        self._auto.start()

    def _retry(self):
        self.status.setText("בודק...")
        self._get_devices = getattr(self.parent(), "_get_devices_for_telemetry", lambda: ([], []))
        nvrs, singles = self._get_devices()
        from app.core.telemetry import send_now, STATUS_BLOCKED, STATUS_ERROR, last_status
        result = send_now(nvrs, singles)
        if result.ok and last_status() != STATUS_BLOCKED:
            self.accept()      # unblocked – let the main window reload
        else:
            self.status.setText("עדיין חסום. " + ("ה-NVR לא נגיש." if not result.ok else ""))

    def _quit(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()
