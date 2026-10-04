"""A real video player for downloaded recordings: seek bar you can scrub, play/pause, jump back and
forward, frame-by-frame, play BACKWARDS, speeds from 0.25x to 16x, A-B loop, zoom, snapshot, full screen.

The decoding runs in app.core.playerengine; this file is only the window."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider, QStyle, QVBoxLayout, QWidget,
)

from app.core import recfmt
from app.core.playclock import SPEEDS, fmt_clock
from app.core.playerengine import PlayerEngine
from app.core.zoom import ZoomState, fit_rect

_OPEN: list = []          # open player windows (a top-level window with no owner would be garbage collected)
_BACKGROUND: list = []    # engines that were asked to stop but have not finished yet

HELP = ("רווח: ניגון/עצירה   ←/→: 5 שנ'   Shift: 30 שנ'   Ctrl: דקה   J/L: 10 שנ'   ,/.: פריים אחורה/קדימה   "
        "[ ]: מהירות   R: ניגון אחורה   A/B: לולאה   C: ניקוי לולאה   S: צילום   F: מסך מלא   גלגלת: זום")


class _SeekSlider(QSlider):
    """A slider where a click jumps straight to that spot (the default only moves one page)."""
    seek_requested = pyqtSignal(int)
    scrubbing = pyqtSignal(bool)

    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self._down = False

    def _value_at(self, event) -> int:
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(event.position().x()), self.width())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.scrubbing.emit(True)
            v = self._value_at(event)
            self.setValue(v)
            self.seek_requested.emit(v)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._down:
            v = self._value_at(event)
            self.setValue(v)
            self.seek_requested.emit(v)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._down and event.button() == Qt.MouseButton.LeftButton:
            self._down = False
            self.scrubbing.emit(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _VideoView(QLabel):
    double_clicked = pyqtSignal()
    zoom_changed = pyqtSignal(float)

    def __init__(self):
        super().__init__("טוען...")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(480, 270)
        self.setStyleSheet("background:#000; color:#8a8f9a; font-size:15px;")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._img: QImage | None = None
        self._zoom = ZoomState()
        self._drag = None

    def image(self) -> QImage | None:
        return self._img

    def set_image(self, img: QImage):
        self._img = img
        self._render()

    def reset_zoom(self):
        self._zoom.reset()
        self.zoom_changed.emit(self._zoom.zoom)
        self._render()

    def _render(self):
        img = self._img
        if img is None:
            return
        if self._zoom.active:
            x, y, w, h = self._zoom.crop_rect(img.width(), img.height())
            img = img.copy(x, y, w, h)
        self.setPixmap(QPixmap.fromImage(img).scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))

    def _geometry(self):
        img = self._img
        if img is None:
            return 0.0, 0.0, 0.0, 0.0
        if self._zoom.active:
            _x, _y, w, h = self._zoom.crop_rect(img.width(), img.height())
        else:
            w, h = img.width(), img.height()
        return fit_rect(w, h, self.width(), self.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    def wheelEvent(self, event):
        dy = event.angleDelta().y()
        if dy == 0 or self._img is None:
            return
        ox, oy, w, h = self._geometry()
        pos = event.position()
        u = (pos.x() - ox) / w if w else 0.5
        v = (pos.y() - oy) / h if h else 0.5
        self._zoom.wheel(dy / 120.0, u, v)
        self.setCursor(Qt.CursorShape.OpenHandCursor if self._zoom.active else Qt.CursorShape.ArrowCursor)
        self.zoom_changed.emit(self._zoom.zoom)
        self._render()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._zoom.active:
            self._drag = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            _ox, _oy, w, h = self._geometry()
            if w and h:
                d = event.position() - self._drag
                self._zoom.pan(d.x() / w, d.y() / h)
                self._render()
            self._drag = event.position()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._zoom.active else Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()


class PlayerWindow(QWidget):
    def __init__(self, path: str):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.path = path
        self.start_time: datetime | None = recfmt.parse_stamp(Path(path).name)
        self.setWindowTitle(f"נגן הקלטות - {Path(path).name}")
        self.resize(1100, 740)
        self.duration = 0.0
        self.fps = 25.0
        self._t = 0.0
        self._speed = 1.0
        self._paused = False
        self._loop_a: float | None = None
        self._loop_b: float | None = None
        self._scrubbing = False
        self._fullscreen = False
        self._was_maximized = False

        self.view = _VideoView()
        self.view.double_clicked.connect(self._toggle_fullscreen)
        self.view.zoom_changed.connect(self._on_zoom)

        self.slider = _SeekSlider()
        self.slider.setRange(0, 0)
        self.slider.seek_requested.connect(lambda ms: self.engine.seek(ms / 1000.0))
        self.slider.scrubbing.connect(lambda on: setattr(self, "_scrubbing", on))

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("font-size:15px; font-weight:600;")
        self.wall_label = QLabel("")
        self.wall_label.setStyleSheet("font-size:15px; color:#9fb4ff;")
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color:#8a8f9a;")

        def button(text, tip, slot, width=46):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedWidth(width)
            b.clicked.connect(lambda _checked=False: slot())
            return b

        self.play_btn = button("⏸", "ניגון / עצירה (רווח)", lambda: self.engine.toggle_pause(), 56)
        transport = [
            button("⏮", "להתחלה (Home)", lambda: self.engine.seek(0)),
            button("⏪", "10 שניות אחורה (J)", lambda: self.engine.seek_relative(-10)),
            button("◀|", "פריים אחורה (,)", lambda: self.engine.step(-1)),
            self.play_btn,
            button("|▶", "פריים קדימה (.)", lambda: self.engine.step(1)),
            button("⏩", "10 שניות קדימה (L)", lambda: self.engine.seek_relative(10)),
            button("⏭", "לסוף (End)", lambda: self.engine.seek(max(0.0, self.duration - 1.0))),
        ]
        row_transport = QHBoxLayout()
        row_transport.addStretch(1)
        for b in transport:
            row_transport.addWidget(b)
        row_transport.addStretch(1)

        self.reverse_btn = QPushButton("◀ ניגון אחורה")
        self.reverse_btn.setCheckable(True)
        self.reverse_btn.setToolTip("מנגן את ההקלטה לאחור (R). המהירות נקבעת לפי הבורר משמאל")
        self.reverse_btn.toggled.connect(self._on_reverse)
        self.speed_combo = QComboBox()
        for s in SPEEDS:
            self.speed_combo.addItem(f"{s:g}×", s)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.setToolTip("מהירות ניגון ([ ] משנים)")
        self.speed_combo.currentIndexChanged.connect(self._on_speed)
        self.a_btn = button("A", "מסמן תחילת קטע לחזרה (A)", lambda: self._mark("a"), 34)
        self.b_btn = button("B", "מסמן סוף קטע לחזרה (B)", lambda: self._mark("b"), 34)
        self.clear_btn = button("✖", "מנקה את הקטע החוזר (C)", self._clear_loop, 34)
        self.loop_label = QLabel("")
        self.loop_label.setStyleSheet("color:#f1c40f;")
        self.loop_all = QCheckBox("🔁 חזור מההתחלה")
        self.loop_all.toggled.connect(lambda on: self.engine.set_loop_all(on))
        self.snap_btn = button("📸", "שמירת התמונה הנוכחית (S)", self._snapshot, 40)
        self.full_btn = button("⛶", "מסך מלא (F או לחיצה כפולה)", self._toggle_fullscreen, 40)
        self.zoom_label = QLabel("")
        self.msg_label = QLabel("")
        self.msg_label.setStyleSheet("color:#2ecc71; font-weight:600;")
        self._msg_timer = QTimer(self)            # parented: dies with the window
        self._msg_timer.setSingleShot(True)
        self._msg_timer.timeout.connect(lambda: self.msg_label.setText(""))

        row_options = QHBoxLayout()
        row_options.addWidget(self.reverse_btn)
        row_options.addWidget(QLabel("מהירות:"))
        row_options.addWidget(self.speed_combo)
        row_options.addSpacing(10)
        for w in (self.a_btn, self.b_btn, self.clear_btn, self.loop_label):
            row_options.addWidget(w)
        row_options.addSpacing(10)
        row_options.addWidget(self.loop_all)
        row_options.addStretch(1)
        for w in (self.zoom_label, self.msg_label, self.snap_btn, self.full_btn):
            row_options.addWidget(w)

        row_time = QHBoxLayout()
        row_time.addWidget(self.time_label)
        row_time.addSpacing(16)
        row_time.addWidget(self.wall_label)
        row_time.addStretch(1)
        row_time.addWidget(self.info_label)

        help_label = QLabel(HELP)
        help_label.setStyleSheet("color:#6c7280; font-size:11px;")
        help_label.setWordWrap(True)

        self.panel = QWidget()
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(6, 2, 6, 6)
        pl.addWidget(self.slider)
        pl.addLayout(row_time)
        pl.addLayout(row_transport)
        pl.addLayout(row_options)
        pl.addWidget(help_label)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.view, 1)
        root.addWidget(self.panel)

        for w in self.findChildren((QPushButton, QComboBox, QCheckBox, QSlider)):
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)      # so the keyboard shortcuts reach the window

        self.engine = PlayerEngine(path)
        self.engine.opened.connect(self._on_opened)
        self.engine.frame.connect(self._on_frame)
        self.engine.paused_changed.connect(self._on_paused)
        self.engine.error.connect(self._on_error)
        self.engine.start()

    # ---- from the engine ------------------------------------------------------
    def _on_opened(self, duration: float, fps: float, w: int, h: int):
        self.duration, self.fps = duration, fps
        self.slider.setRange(0, int(duration * 1000))
        self.info_label.setText(f"{w}×{h}  {fps:.0f} fps")
        self._show_time(0.0)

    def _on_frame(self, img, t: float):
        self.view.set_image(img)
        self.engine.pending = False
        self._t = t
        if not self._scrubbing:
            self.slider.blockSignals(True)
            self.slider.setValue(int(t * 1000))
            self.slider.blockSignals(False)
        self._show_time(t)

    def _on_paused(self, paused: bool):
        self._paused = paused
        self.play_btn.setText("▶" if paused else "⏸")

    def _on_error(self, message: str):
        self.view.setText(message)

    def _show_time(self, t: float):
        self.time_label.setText(f"{fmt_clock(t)} / {fmt_clock(self.duration)}")
        if self.start_time is not None:
            self.wall_label.setText((self.start_time + timedelta(seconds=t)).strftime("🕒 %Y-%m-%d  %H:%M:%S"))

    def _on_zoom(self, zoom: float):
        self.zoom_label.setText(f"🔍 x{zoom:.1f}" if zoom > 1.0001 else "")

    # ---- controls ---------------------------------------------------------------
    def _on_reverse(self, on: bool):
        self.engine.set_reverse(on)
        if on:
            self.engine.set_paused(False)         # pressing "backwards" starts playing backwards at once

    def _on_speed(self, index: int):
        self._speed = float(self.speed_combo.itemData(index))
        self.engine.set_speed(self._speed)

    def _speed_step(self, direction: int):
        i = max(0, min(len(SPEEDS) - 1, self.speed_combo.currentIndex() + direction))
        self.speed_combo.setCurrentIndex(i)

    def _mark(self, which: str):
        if which == "a":
            self._loop_a = self._t
            if self._loop_b is not None and self._loop_b <= self._loop_a:
                self._loop_b = None
        else:
            self._loop_b = self._t
            if self._loop_a is None or self._loop_a >= self._loop_b:
                self._loop_a = None
        self._apply_loop()

    def _clear_loop(self):
        self._loop_a = self._loop_b = None
        self._apply_loop()

    def _apply_loop(self):
        self.engine.set_loop(self._loop_a, self._loop_b)
        if self._loop_a is not None and self._loop_b is not None:
            self.loop_label.setText(f"חוזר: {fmt_clock(self._loop_a)} → {fmt_clock(self._loop_b)}")
        elif self._loop_a is not None:
            self.loop_label.setText(f"A = {fmt_clock(self._loop_a)}  (סמן B)")
        elif self._loop_b is not None:
            self.loop_label.setText(f"B = {fmt_clock(self._loop_b)}  (סמן A)")
        else:
            self.loop_label.setText("")

    def _flash(self, text: str, ok: bool = True):
        self.msg_label.setStyleSheet(f"color:{'#2ecc71' if ok else '#e74c3c'}; font-weight:600;")
        self.msg_label.setText(text)
        self._msg_timer.start(3000)

    def _snapshot(self):
        img = self.view.image()
        if img is None:
            return
        folder = Path.home() / "Pictures" / "CameraSnapshots"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stem = re.sub(r'[\\/:*?"<>|]+', "_", Path(self.path).stem)
            target = folder / f"{stem}_{fmt_clock(self._t).replace(':', '-')}_{datetime.now():%H%M%S}.png"
            ok = img.save(str(target))
        except OSError:
            ok = False
        self._flash("📸 נשמר ב-Pictures\\CameraSnapshots" if ok else "שמירת התמונה נכשלה", ok)

    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        on = self._fullscreen
        self.panel.setVisible(not on)
        self.engine.max_width = 1920 if on else 1280
        if on:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        else:
            self.showMaximized() if self._was_maximized else self.showNormal()

    # ---- keyboard -----------------------------------------------------------------
    def keyPressEvent(self, event):
        key, mods = event.key(), event.modifiers()
        K = Qt.Key
        big = 60 if mods & Qt.KeyboardModifier.ControlModifier else (30 if mods & Qt.KeyboardModifier.ShiftModifier else 5)
        if key in (K.Key_Space, K.Key_K):
            self.engine.toggle_pause()
        elif key == K.Key_Left:
            self.engine.seek_relative(-big)
        elif key == K.Key_Right:
            self.engine.seek_relative(big)
        elif key == K.Key_J:
            self.engine.seek_relative(-10)
        elif key == K.Key_L:
            self.engine.seek_relative(10)
        elif key == K.Key_Comma:
            self.engine.step(-1)
        elif key == K.Key_Period:
            self.engine.step(1)
        elif key in (K.Key_BracketRight, K.Key_Plus, K.Key_Equal, K.Key_Up):
            self._speed_step(+1)
        elif key in (K.Key_BracketLeft, K.Key_Minus, K.Key_Down):
            self._speed_step(-1)
        elif key == K.Key_R:
            self.reverse_btn.toggle()
        elif key == K.Key_A:
            self._mark("a")
        elif key == K.Key_B:
            self._mark("b")
        elif key == K.Key_C:
            self._clear_loop()
        elif key == K.Key_S:
            self._snapshot()
        elif key == K.Key_Home:
            self.engine.seek(0)
        elif key == K.Key_End:
            self.engine.seek(max(0.0, self.duration - 1.0))
        elif key in (K.Key_F, K.Key_F11):
            self._toggle_fullscreen()
        elif key == K.Key_Escape and self._fullscreen:
            self._toggle_fullscreen()
        elif key == K.Key_0:
            self.view.reset_zoom()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.engine.request_stop()
        self.engine.wait(1500)
        if self.engine.isRunning():                  # never drop a running QThread
            _BACKGROUND.append(self.engine)
        _BACKGROUND[:] = [e for e in _BACKGROUND if e.isRunning()]
        if self in _OPEN:
            _OPEN.remove(self)
        super().closeEvent(event)


def open_player(path: str) -> PlayerWindow:
    w = PlayerWindow(path)
    _OPEN.append(w)
    w.show()
    return w
