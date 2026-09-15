from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QSpinBox, QDialogButtonBox,
    QComboBox, QVBoxLayout, QLabel
)

from app.core.camera import CameraConfig


class AddDeviceDialog(QDialog):
    """One dialog used for 'הוסף NVR', 'הוסף מצלמה בודדת', and also for
    editing an existing one (pass existing_cfg) -- same fields, just
    pre-filled and the title/button say 'עריכה' instead of 'הוספה'."""

    def __init__(self, is_nvr: bool, parent=None, existing_cfg: CameraConfig | None = None):
        super().__init__(parent)
        self.is_nvr = is_nvr
        self.existing_cfg = existing_cfg
        is_edit = existing_cfg is not None
        if is_edit:
            self.setWindowTitle("עריכת NVR" if is_nvr else "עריכת מצלמה")
        else:
            self.setWindowTitle("הוספת NVR" if is_nvr else "הוספת מצלמה בודדת")
        self.setMinimumWidth(360)

        default_name = existing_cfg.name if existing_cfg else ("NVR חדש" if is_nvr else "מצלמה חדשה")
        self.name_edit = QLineEdit(default_name)
        self.host_edit = QLineEdit(existing_cfg.host if existing_cfg else "")
        self.host_edit.setPlaceholderText("192.168.1.64")

        self.onvif_port_spin = QSpinBox()
        self.onvif_port_spin.setRange(1, 65535)
        self.onvif_port_spin.setValue(existing_cfg.onvif_port if existing_cfg else 80)

        self.rtsp_port_spin = QSpinBox()
        self.rtsp_port_spin.setRange(1, 65535)
        self.rtsp_port_spin.setValue(existing_cfg.port if existing_cfg else 554)

        self.user_edit = QLineEdit(existing_cfg.username if existing_cfg else "")
        self.pass_edit = QLineEdit(existing_cfg.password if existing_cfg else "")
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)

        default_path = existing_cfg.rtsp_path if existing_cfg else "auto"
        self.rtsp_path_edit = QLineEdit(default_path)
        self.rtsp_path_edit.setPlaceholderText("auto = ננסה לבד את הנתיבים הנפוצים, או הקלד נתיב מדויק")

        self.transport_combo = QComboBox()
        self.transport_combo.addItems(["tcp", "udp"])
        if existing_cfg:
            idx = self.transport_combo.findText(existing_cfg.transport)
            if idx >= 0:
                self.transport_combo.setCurrentIndex(idx)

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
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("שמור שינויים" if is_edit else "הוסף")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        if is_nvr:
            layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def to_config(self) -> CameraConfig:
        # editing an existing device: keep its id (and parent_nvr_id) so the
        # sidebar/grid/worker all still recognize it as "the same camera"
        base_kwargs = {}
        if self.existing_cfg:
            base_kwargs["id"] = self.existing_cfg.id
            base_kwargs["parent_nvr_id"] = self.existing_cfg.parent_nvr_id

        if self.is_nvr:
            return CameraConfig(
                name=self.name_edit.text().strip() or "NVR",
                host=self.host_edit.text().strip(),
                onvif_port=self.onvif_port_spin.value(),
                username=self.user_edit.text(),
                password=self.pass_edit.text(),
                **base_kwargs,
            )
        return CameraConfig(
            name=self.name_edit.text().strip() or "מצלמה",
            host=self.host_edit.text().strip(),
            port=self.rtsp_port_spin.value(),
            rtsp_path=self.rtsp_path_edit.text().strip() or "auto",
            username=self.user_edit.text(),
            password=self.pass_edit.text(),
            transport=self.transport_combo.currentText(),
            **base_kwargs,
        )
