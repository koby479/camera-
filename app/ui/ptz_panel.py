"""Small floating window that moves a camera (PTZ): direction pad, zoom, focus, iris, speed and presets.
Hold a button to move, release to stop. Works for cameras on a DVRIP recorder; whether the camera itself can move
is decided by the camera - a refusal from the recorder is shown in the status line."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget)

from app.core.ptz import STOP_MODES, PtzController

HOLD_AFTER_MS = 180      # pressed longer than this = continuous movement; shorter = a small nudge
NUDGE_MS = 150           # how long a nudge moves the camera (at the lowest speed)

PAD = [("↖", "up_left"), ("↑", "up"), ("↗", "up_right"),
       ("←", "left"), ("■", None), ("→", "right"),
       ("↙", "down_left"), ("↓", "down"), ("↘", "down_right")]


class PtzPanel(QWidget):
    _result = pyqtSignal(bool, str)          # from the network thread to the window

    def __init__(self, cfg, parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.cfg = cfg
        self._active: str | None = None
        self._pressed: str | None = None
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._begin_hold)
        self._nudge_timer = QTimer(self)
        self._nudge_timer.setSingleShot(True)
        self._nudge_timer.timeout.connect(self._end_nudge)
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
                b.pressed.connect(lambda n=name: self._press(n))
                b.released.connect(self._release)
            else:
                b.setToolTip("עצירת חירום: שולח עצירה לכל הכיוונים")
                b.clicked.connect(self._emergency_stop)
            grid.addWidget(b, i // 3, i % 3)

        def pair(title, minus, plus):
            row = QHBoxLayout()
            row.addWidget(QLabel(title))
            row.addStretch(1)
            for text, name in ((("−"), minus), (("+"), plus)):
                b = QPushButton(text)
                b.setFixedSize(40, 30)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.pressed.connect(lambda n=name: self._press(n))
                b.released.connect(self._release)
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

        self.stop_mode = QComboBox()
        for key, label in STOP_MODES.items():
            self.stop_mode.addItem(label, key)
        self.stop_mode.setToolTip("אם המצלמה לא נעצרת אחרי לחיצה, נסה שיטת עצירה אחרת")
        self.stop_mode.currentIndexChanged.connect(
            lambda _i: setattr(self.ctl, "stop_mode", self.stop_mode.currentData()))
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("שיטת עצירה:"))
        mode_row.addWidget(self.stop_mode, 1)

        self.status = QLabel("לחיצה קצרה = הזזה קטנה, החזקה = תנועה רצופה")
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
        root.addLayout(mode_row)
        root.addWidget(self.status)
        self.setFixedWidth(250)

    def _press(self, name: str):
        self._stop()
        self._pressed = name
        self._hold_timer.start(HOLD_AFTER_MS)      # not yet moving: a quick click must stay a small step

    def _begin_hold(self):
        if self._pressed:
            self._active = self._pressed
            self._active_speed = self.speed.value()
            self.ctl.move(self.cfg.channel, self._active, self._active_speed)

    def _release(self):
        name, self._pressed = self._pressed, None
        if self._hold_timer.isActive():            # short click: a tiny movement at the lowest speed
            self._hold_timer.stop()
            if name:
                self._active, self._active_speed = name, 1
                self.ctl.move(self.cfg.channel, name, 1)
                self._nudge_timer.start(NUDGE_MS)
        else:
            self._stop()

    def _emergency_stop(self):
        self._stop()
        self.ctl.stop_all(self.cfg.channel)

    def _end_nudge(self):
        self._stop()

    def _stop(self):
        self._hold_timer.stop()
        self._nudge_timer.stop()
        self._pressed = None
        if self._active:
            self.ctl.stop(self.cfg.channel, self._active, getattr(self, "_active_speed", self.speed.value()))
            self._active = None

    def _show_result(self, ok: bool, text: str):
        self.status.setStyleSheet("color:#2ecc71;" if ok else "color:#e74c3c; font-weight:600;")
        self.status.setText(text)

    def showEvent(self, event):
        self.ctl.warm_up()                    # connect before the first click
        super().showEvent(event)

    def hideEvent(self, event):
        self._stop()                          # never leave the camera moving
        super().hideEvent(event)

    def closeEvent(self, event):
        self._stop()
        super().closeEvent(event)
