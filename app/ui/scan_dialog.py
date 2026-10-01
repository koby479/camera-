from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QLabel, QProgressBar, QMessageBox
)

from app.core.scan import scan, ScanHit
from app.core.camera import CameraConfig


class ScanWorker(QThread):
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(list)   # list[ScanHit]
    failed = pyqtSignal(str)

    def __init__(self, spec: str, username: str, password: str, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.username = username
        self.password = password

    def run(self):
        try:
            hits = scan(
                self.spec, self.username, self.password,
                progress_cb=lambda d, t: self.progress.emit(d, t),
            )
            self.finished_ok.emit(hits)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class ScanDialog(QDialog):
    """סריקת רשת: מזינים IP בודד / טווח (192.168.1.1-254) / סאבנט (192.168.1.0/24)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("סריקת רשת")
        self.setMinimumSize(520, 420)
        self.hits: list[ScanHit] = []
        self.worker: ScanWorker | None = None

        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("192.168.1.100  או  192.168.1.1-254  או  192.168.1.0/24")
        self.user_edit = QLineEdit()
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)

        form = QFormLayout()
        form.addRow("יעד סריקה:", self.target_edit)
        form.addRow("משתמש (לבדיקת ONVIF, אופציונלי):", self.user_edit)
        form.addRow("סיסמה:", self.pass_edit)

        self.scan_btn = QPushButton("התחל סריקה")
        self.scan_btn.clicked.connect(self.start_scan)

        self.progress = QProgressBar()
        self.status_label = QLabel("")

        self.results_list = QListWidget()

        add_row = QHBoxLayout()
        self.add_as_single_btn = QPushButton("הוסף כמצלמה בודדת")
        self.add_as_single_btn.clicked.connect(self.add_selected_as_single)
        self.add_as_nvr_btn = QPushButton("הוסף כ-NVR")
        self.add_as_nvr_btn.clicked.connect(self.add_selected_as_nvr)
        add_row.addWidget(self.add_as_single_btn)
        add_row.addWidget(self.add_as_nvr_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.scan_btn)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        layout.addWidget(QLabel("תוצאות (מכשירים שהגיבו על פורט מצלמה/NVR נפוץ):"))
        layout.addWidget(self.results_list, stretch=1)
        layout.addLayout(add_row)

        self.selected_cfg: CameraConfig | None = None   # set when the caller should read it out
        self.selected_kind: str | None = None            # "single" | "nvr"

    def start_scan(self):
        spec = self.target_edit.text().strip()
        if not spec:
            QMessageBox.warning(self, "חסר יעד", "יש להזין כתובת IP, טווח או סאבנט לסריקה.")
            return
        self.results_list.clear()
        self.scan_btn.setEnabled(False)
        self.status_label.setText("סורק... זה יכול לקחת עד כמה דקות בטווח גדול.")
        self.worker = ScanWorker(spec, self.user_edit.text(), self.pass_edit.text())
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_progress(self, done: int, total: int):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)

    def _on_done(self, hits: list[ScanHit]):
        self.hits = hits
        self.scan_btn.setEnabled(True)
        if not hits:
            self.status_label.setText(
                "לא נמצא אף מכשיר עם פורט מצלמה/NVR פתוח. "
                "יתכן וזה חוסם ע\"י חומת אש/Windows Firewall, או שהמצלמה על תת-רשת אחרת."
            )
            return
        self.status_label.setText(f"נמצאו {len(hits)} מכשירים.")
        for h in hits:
            ports_str = ", ".join(f"{p}/{lbl}" for p, lbl in h.open_ports.items())
            onvif_str = ""
            if h.onvif_ok:
                onvif_str = f"  ✅ ONVIF תקין ({h.manufacturer or '?'})"
            elif h.onvif_error:
                onvif_str = f"  ⚠ ONVIF נכשל: {h.onvif_error[:60]}"
            item = QListWidgetItem(f"{h.host} — פורטים פתוחים: {ports_str}{onvif_str}")
            item.setData(1000, h)
            self.results_list.addItem(item)

    def _on_failed(self, error: str):
        self.scan_btn.setEnabled(True)
        self.status_label.setText(f"הסריקה נכשלה: {error}")

    def _selected_hit(self) -> ScanHit | None:
        item = self.results_list.currentItem()
        if not item:
            QMessageBox.information(self, "לא נבחר כלום", "יש לבחור מכשיר מהרשימה קודם.")
            return None
        return item.data(1000)

    def add_selected_as_single(self):
        hit = self._selected_hit()
        if not hit:
            return
        self.selected_cfg = CameraConfig(
            name=f"מצלמה {hit.host}",
            host=hit.host,
            port=554 if 554 in hit.open_ports else list(hit.open_ports.keys())[0],
            username=self.user_edit.text(),
            password=self.pass_edit.text(),
        )
        self.selected_kind = "single"
        self.accept()

    def add_selected_as_nvr(self):
        hit = self._selected_hit()
        if not hit:
            return
        onvif_port = 80 if 80 in hit.open_ports else (8000 if 8000 in hit.open_ports else 2020)
        self.selected_cfg = CameraConfig(
            name=f"NVR {hit.host}",
            host=hit.host,
            onvif_port=onvif_port,
            username=self.user_edit.text(),
            password=self.pass_edit.text(),
        )
        self.selected_kind = "nvr"
        self.accept()
