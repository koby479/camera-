from __future__ import annotations

import cv2
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

from app.core.camera import CameraConfig, CameraStatus

STATUS_LABELS = {
    CameraStatus.UNKNOWN: ("מתחבר...", "#888888"),
    CameraStatus.ACTIVE: ("פעילה", "#2ecc71"),
    CameraStatus.DEAD: ("מתה", "#e67e22"),
    CameraStatus.OFFLINE: ("לא מחוברת", "#e74c3c"),
}


class VideoTile(QWidget):
    def __init__(self, cfg: CameraConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.status = CameraStatus.UNKNOWN

        self.video_label = QLabel("ממתין לתמונה...")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background:#0d0f13; color:#5a5f6a; border-radius:0 0 8px 8px;")
        self.video_label.setMinimumSize(160, 90)

        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-weight:600; padding:2px; color:#e6e6e6;")

        self.status_label = QLabel()
        self.status_label.setStyleSheet("padding:2px;")

        header = QWidget()
        from PyQt6.QtWidgets import QHBoxLayout
        h = QHBoxLayout(header)
        h.setContentsMargins(8, 5, 8, 5)
        h.addWidget(self.title_label)
        h.addStretch()
        h.addWidget(self.status_label)
        header.setStyleSheet("background:#1f232c; border-radius:8px 8px 0 0;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self.video_label, stretch=1)

        self.setStyleSheet("border:1px solid #2a2e37; border-radius:8px;")
        self.set_status(CameraStatus.UNKNOWN)

    def set_status(self, status: CameraStatus):
        self.status = status
        text, color = STATUS_LABELS[status]
        self.title_label.setText(self.cfg.name)
        self.status_label.setText(f"● {text}")
        self.status_label.setStyleSheet(f"color:{color}; font-weight:bold; padding:2px;")
        if status in (CameraStatus.OFFLINE, CameraStatus.DEAD):
            self.video_label.setText(text)

    def update_frame(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(qimg).scaled(
            self.video_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self.video_label.setPixmap(pix)
