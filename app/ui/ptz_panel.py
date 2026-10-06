"""Small floating window that moves a camera (PTZ): direction pad, zoom, focus, iris, speed and presets.
Hold a button to move, release to stop. Works for cameras on a DVRIP recorder; whether the camera itself can move
is decided by the camera - a refusal from the recorder is shown in the status line."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget)

from app.core.ptz import PtzController

PAD = [("↖", "up_left"), ("↑", "up"), ("↗", "up_right"),
       ("←", "left"), ("■", None), ("→", "right"),
       ("↙", "down_left"), ("↓", "down"), ("↘", "down_right")]


class PtzPanel(QWidget):
    _result = pyqtSignal(bool, str)          # from the network thread to the window

    def __init__(self, cfg, parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.cfg = cfg
        self._active: str | None = None
        self.ctl = PtzController.for_cfg(cfg)
        self.setWindowTitle(f"שליטה במצלמה - {cfg.name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._result.connect(self._show_result)
        self.ctl.on_result = lambda ok, text: self._result.emit(ok, text)

        grid = QGridLayout()
        for i, (label, name) in enumerate(PAD):
            b = QPushButton(label)
            b.setFixedSize(52, 52)
            b.setStyleSheet("font-size:20px; font-weight:bold;")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if name:
                b.pressed.connect(lambda n=name: self._start(n))
                b.released.connect(self._stop)
            else:
                b.setToolTip("עצור")
                b.clicked.connect(self._stop)
            grid.addWidget(b, i // 3, i % 3)

        def pair(title, minus, plus):
            row = QHBoxLayout()
            row.addWidget(QLabel(title))
            row.addStretch(1)
            for text, name in ((("−"), minus), (("+"), plus)):
                b = QPushButton(text)
                b.setFixedSize(40, 30)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.pressed.connect(lambda n=name: self._start(n))
                b.released.connect(self._stop)
                row.addWidget(b)
            return row

        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(1, 8)
        self.speed.setValue(4)
        self.speed_label = QLabel("מהירות: 4")
        self.speed.valueChanged.connect(lambda v: self.speed_label.setText(f"מהירות: {v}"))

        self.preset = QSpinBox()
        self.preset.setRange(1, 255)
        go = QPushButton("עבור")
        go.clicked.connect(lambda: self.ctl.goto_preset(self.cfg.channel, self.preset.value()))
        save = QPushButton("שמור")
        save.setToolTip("שומר את המיקום הנוכחי של המצלמה בתור נקודה זו")
        save.clicked.connect(lambda: self.ctl.set_preset(self.cfg.channel, self.preset.value()))
        presets = QHBoxLayout()
        presets.addWidget(QLabel("נקודה:"))
        presets.addWidget(self.preset)
        presets.addWidget(go)
        presets.addWidget(save)

        self.status = QLabel("החזק כפתור כדי להזיז, שחרר כדי לעצור")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#9fb4ff;")

        root = QVBoxLayout(self)
        root.addLayout(grid)
        root.addWidget(self.speed_label)
        root.addWidget(self.speed)
        root.addLayout(pair("זום", "zoom_out", "zoom_in"))
        root.addLayout(pair("פוקוס", "focus_near", "focus_far"))
        root.addLayout(pair("צמצם", "iris_close", "iris_open"))
        root.addLayout(presets)
        root.addWidget(self.status)
        self.setFixedWidth(250)

    def _start(self, name: str):
        self._stop()
        self._active = name
        self.ctl.move(self.cfg.channel, name, self.speed.value())

    def _stop(self):
        if self._active:
            self.ctl.stop(self.cfg.channel, self._active, self.speed.value())
            self._active = None

    def _show_result(self, ok: bool, text: str):
        self.status.setStyleSheet("color:#2ecc71;" if ok else "color:#e74c3c; font-weight:600;")
        self.status.setText(text)

    def hideEvent(self, event):
        self._stop()                          # never leave the camera moving
        super().hideEvent(event)

    def closeEvent(self, event):
        self._stop()
        super().closeEvent(event)
