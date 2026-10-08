"""The one-time consent screen, and the settings dialog reachable any time from the menu. Both show the exact
fields that are sent - no field is ever sent that is not listed on this screen."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QLabel, QPushButton, QTextEdit, QVBoxLayout)

from app.core import settings, telemetry

EXPLAIN = (
    "התוכנה יכולה לשלוח אליי (המפתח) פרטי ניהול, כדי שאדע מי משתמש בה ואוכל לתמוך בך.\n\n"
    "מה נשלח, בכל דיווח:\n"
    "  • גרסת התוכנה, שעת ההפעלה, ומספר התקנה אקראי (לא שם המשתמש שלך ב-Windows)\n"
    "  • עבור כל NVR/מצלמה שהגדרת: השם שנתת לה, כתובת ה-IP, הפורט ומספר הערוצים\n"
    "  • אפשר גם, אם תבחר בכך למטה, לשתף את שם המשתמש (לא הסיסמה) של ה-NVR/מצלמה\n\n"
    "מה לעולם לא נשלח:\n"
    "  • סיסמה של אף מצלמה או NVR, בשום מצב\n"
    "  • תמונה, וידאו או הקלטה כלשהי\n\n"
    "אפשר לשנות את ההחלטה בכל רגע: תפריט עזרה ← \"שיתוף פרטים לניהול\"."
)


class ConsentDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("שיתוף פרטים לניהול")
        self.setMinimumWidth(480)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        text = QTextEdit(EXPLAIN)
        text.setReadOnly(True)
        self.computer_name = QCheckBox("לשתף גם את שם המחשב")
        self.credentials_username = QCheckBox("לשתף גם את שם המשתמש (לא הסיסמה) של ה-NVR/מצלמה")
        buttons = QDialogButtonBox()
        yes = buttons.addButton("מאשר, שתף", QDialogButtonBox.ButtonRole.AcceptRole)
        no = buttons.addButton("לא תודה", QDialogButtonBox.ButtonRole.RejectRole)
        yes.clicked.connect(self.accept)
        no.clicked.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(text)
        lay.addWidget(self.computer_name)
        lay.addWidget(self.credentials_username)
        lay.addWidget(buttons)

    @staticmethod
    def ask(parent=None) -> bool:
        """Shown once, the first time it is relevant. Returns whether the person agreed."""
        dlg = ConsentDialog(parent)
        agreed = dlg.exec() == QDialog.DialogCode.Accepted
        settings.put("telemetry_share_computer_name", agreed and dlg.computer_name.isChecked())
        settings.put("telemetry_share_credentials_username", agreed and dlg.credentials_username.isChecked())
        telemetry.set_enabled(agreed)
        return agreed


class TelemetrySettingsDialog(QDialog):
    """Reopened any time from the menu: current status, what is shared, and a way to turn it off or back on."""

    def __init__(self, reporter, parent=None):
        super().__init__(parent)
        self.reporter = reporter
        self.setWindowTitle("שיתוף פרטים לניהול")
        self.setMinimumWidth(480)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        text = QTextEdit(EXPLAIN)
        text.setReadOnly(True)
        self.enabled_box = QCheckBox("לשתף פרטי ניהול")
        self.enabled_box.setChecked(telemetry.enabled())
        self.computer_name = QCheckBox("לשתף גם את שם המחשב")
        self.computer_name.setChecked(bool(settings.get("telemetry_share_computer_name", False)))
        self.computer_name.setEnabled(self.enabled_box.isChecked())
        self.enabled_box.toggled.connect(self.computer_name.setEnabled)
        self.credentials_username = QCheckBox("לשתף גם את שם המשתמש (לא הסיסמה) של ה-NVR/מצלמה")
        self.credentials_username.setChecked(bool(settings.get("telemetry_share_credentials_username", False)))
        self.credentials_username.setEnabled(self.enabled_box.isChecked())
        self.enabled_box.toggled.connect(self.credentials_username.setEnabled)
        self.status = QLabel(self._status_text())
        self.status.setWordWrap(True)
        send_now = QPushButton("שלח דיווח עכשיו")
        send_now.setEnabled(telemetry.enabled())
        send_now.clicked.connect(self._send_now)
        self.enabled_box.toggled.connect(send_now.setEnabled)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(text)
        lay.addWidget(self.enabled_box)
        lay.addWidget(self.computer_name)
        lay.addWidget(self.credentials_username)
        lay.addWidget(send_now)
        lay.addWidget(self.status)
        lay.addWidget(buttons)

    def _status_text(self) -> str:
        if not telemetry.enabled():
            return "השיתוף כבוי. שום דבר לא נשלח."
        r = self.reporter.last_result
        if r is None:
            return "השיתוף פעיל. עדיין לא נשלח דיווח בהפעלה הזו."
        return "הדיווח האחרון נשלח בהצלחה." if r.ok else f"הדיווח האחרון נכשל: {r.detail}"

    def _send_now(self):
        self.reporter.notify_changed()
        self.status.setText("נשלח ברקע...")

    def accept(self):
        telemetry.set_enabled(self.enabled_box.isChecked())
        settings.put("telemetry_share_computer_name", self.computer_name.isChecked())
        settings.put("telemetry_share_credentials_username", self.credentials_username.isChecked())
        self.reporter.notify_changed()
        super().accept()
