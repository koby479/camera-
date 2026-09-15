from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QSpinBox, QDialogButtonBox,
    QComboBox, QVBoxLayout, QLabel
)

from app.core.camera import CameraConfig


class AddDeviceDialog(QDialog):
    """One dialog used both for 'הוסף NVR' and 'הוסף מצלמה בודדת' --
    the only difference is the title and whether we probe ONVIF profiles
    afterwards (handled by the caller)."""

    def __init__(self, is_nvr: bool, parent=None):
        super().__init__(parent)
        self.is_nvr = is_nvr
        self.setWindowTitle("הוספת NVR" if is_nvr else "הוספת מצלמה בודדת")
        self.setMinimumWidth(360)

        self.name_edit = QLineEdit("NVR חדש" if is_nvr else "מצלמה חדשה")
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("192.168.1.64")

        self.onvif_port_spin = QSpinBox()
        self.onvif_port_spin.setRange(1, 65535)
        self.onvif_port_spin.setValue(80)

        self.rtsp_port_spin = QSpinBox()
        self.rtsp_port_spin.setRange(1, 65535)
        self.rtsp_port_spin.setValue(554)

        self.user_edit = QLineEdit()
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)

        self.rtsp_path_edit = QLineEdit("/")
        self.rtsp_path_edit.setPlaceholderText("/Streaming/Channels/101 או /cam/realmonitor")

        self.transport_combo = QComboBox()
        self.transport_combo.addItems(["tcp", "udp"])

        form = QFormLayout()
        form.addRow("שם:", self.name_edit)
        form.addRow("כתובת IP:", self.host_edit)
        if is_nvr:
            form.addRow("פורט ONVIF (לרוב 80):", self.onvif_port_spin)
            note = QLabel("התוכנה תתחבר דרך ONVIF ותביא אוטומטית את כל הערוצים המחוברים ל-NVR.")
            note.setWordWrap(True)
        else:
            form.addRow("פורט RTSP (לרוב 554):", self.rtsp_port_spin)
            form.addRow("נתיב RTSP:", self.rtsp_path_edit)
            form.addRow("טרנספורט:", self.transport_combo)
        form.addRow("משתמש:", self.user_edit)
        form.addRow("סיסמה:", self.pass_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        if is_nvr:
            layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def to_config(self) -> CameraConfig:
        if self.is_nvr:
            return CameraConfig(
                name=self.name_edit.text().strip() or "NVR",
                host=self.host_edit.text().strip(),
                onvif_port=self.onvif_port_spin.value(),
                username=self.user_edit.text(),
                password=self.pass_edit.text(),
            )
        return CameraConfig(
            name=self.name_edit.text().strip() or "מצלמה",
            host=self.host_edit.text().strip(),
            port=self.rtsp_port_spin.value(),
            rtsp_path=self.rtsp_path_edit.text().strip() or "/",
            username=self.user_edit.text(),
            password=self.pass_edit.text(),
            transport=self.transport_combo.currentText(),
        )
