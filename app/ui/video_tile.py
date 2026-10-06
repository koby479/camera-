from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import cv2
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QMenu, QSizePolicy

from app.core.camera import CameraConfig, CameraStatus
from app.core.zoom import ZoomState, fit_rect

STATUS_LABELS = {
    CameraStatus.UNKNOWN: ("מתחבר...", "#888888"),
    CameraStatus.ACTIVE: ("פעילה", "#2ecc71"),
    CameraStatus.DEAD: ("מתה", "#e67e22"),
    CameraStatus.OFFLINE: ("לא מחוברת", "#e74c3c"),
    CameraStatus.AUTH_FAILED: ("שם משתמש/סיסמה שגויים", "#e74c3c"),
}


STATUS_HINTS = {
    CameraStatus.OFFLINE: "אין מענה מהכתובת. בדוק כתובת, פורט, אינטרנט ופורט-פורוורד",
    CameraStatus.DEAD: "החיבור נפתח אבל לא מגיעה תמונה. נסה זרם אחר או התחבר מחדש",
    CameraStatus.AUTH_FAILED: "שם המשתמש או הסיסמה שגויים. תקן ב'שנה פרטי חיבור'",
}


class VideoTile(QWidget):
    audio_toggled = pyqtSignal(str, bool)      # camera_id, wants_audio
    activated = pyqtSignal(str)                # double-click: enlarge / restore
    hd_toggled = pyqtSignal(str, bool)         # camera_id, wants main stream
    ptz_requested = pyqtSignal(object)         # CameraConfig: open the camera-control window
    playback_requested = pyqtSignal(object)    # CameraConfig (right-click menu)
    close_requested = pyqtSignal(str)          # camera_id
    reconnect_requested = pyqtSignal(str, object)       # camera_id, None = same stream / True = main / False = sub
    edit_connection_requested = pyqtSignal(object)      # CameraConfig: open the edit dialog of the owning device
    hide_requested = pyqtSignal(str)           # camera_id: remove a broken camera for this session
    close_all_requested = pyqtSignal()
    hide_dead_requested = pyqtSignal()        # remove every camera that is not working (this session only)
    grid_fullscreen_requested = pyqtSignal()   # whole window full screen, every camera visible

    def __init__(self, cfg: CameraConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.status = CameraStatus.UNKNOWN
        self.fullscreen = False                # kept in sync by the main window
        self.grid_full = False                 # whole window full screen
        self._zoom = ZoomState()
        self._last_img: QImage | None = None   # newest whole frame (for zoom re-render + snapshot)
        self._drag_pos = None
        self._flash_timer = QTimer(self)       # parented: dies with the tile, never fires on a deleted widget
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(lambda: self.set_status(self.status))

        self.video_label = QLabel("ממתין לתמונה...")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background:#0d0f13; color:#5a5f6a; border-radius:0 0 8px 8px;")
        self.video_label.setMinimumSize(160, 90)
        # A QLabel that holds a pixmap reports the pixmap's size as its preferred size. Without
        # this, a tile that was enlarged keeps asking for the big size after it is restored and
        # the neighbouring tiles get squeezed. Ignored = the layout decides, the picture adapts.
        self.video_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-weight:600; padding:2px; color:#e6e6e6;")

        self.status_label = QLabel()
        self.status_label.setStyleSheet("padding:2px;")

        header = QWidget()
        h = QHBoxLayout(header)
        h.setContentsMargins(8, 5, 8, 5)
        h.addWidget(self.title_label)
        self.zoom_label = QLabel()
        self.zoom_label.setStyleSheet("color:#f1c40f; font-weight:bold; padding:2px;")
        h.addWidget(self.zoom_label)
        self.zoom_label.setVisible(False)      # after addWidget (see the note on setVisible below)
        h.addStretch()
        self.audio_btn = QPushButton("🔇")
        self.audio_btn.setCheckable(True)
        self.audio_btn.setFixedSize(28, 22)
        self.audio_btn.setToolTip("שמע (זמין למצלמות XM בלבד)")
        self.audio_btn.setStyleSheet("QPushButton{border:none; background:transparent; padding:0px; font-size:14px;}")
        self.audio_btn.toggled.connect(self._on_audio_btn)
        self.hd_btn = QPushButton("HD")
        self.hd_btn.setCheckable(True)
        self.hd_btn.setFixedSize(30, 22)
        self.hd_btn.setToolTip("איכות מלאה (זרם ראשי). כבד למחשב, במיוחד במצלמות 4K")
        self.hd_btn.setStyleSheet("QPushButton{border:none; background:transparent; padding:0px; color:#8a8f9a; font-weight:bold;}"
                                  "QPushButton:checked{color:#2ecc71;}")
        self.hd_btn.toggled.connect(lambda on: self.hd_toggled.emit(self.cfg.id, on))
        self.ptz_btn = QPushButton("PTZ")
        self.ptz_btn.setFixedSize(34, 22)
        self.ptz_btn.setToolTip("שליטה במצלמה: הזזה, זום, פוקוס (למצלמות שתומכות)")
        self.ptz_btn.setStyleSheet("QPushButton{border:none; background:transparent; padding:0px; font-size:14px;}")
        self.ptz_btn.clicked.connect(lambda: self.ptz_requested.emit(self.cfg))
        h.addWidget(self.ptz_btn)
        self.ptz_btn.setVisible(cfg.protocol == "dvrip")
        h.addWidget(self.hd_btn)
        self.hd_btn.setVisible(cfg.protocol == "dvrip")
        h.addWidget(self.audio_btn)
        self.audio_btn.setVisible(cfg.protocol == "dvrip")   # after addWidget: setVisible on a parentless widget opens a stray window
        h.addWidget(self.status_label)
        header.setStyleSheet("background:#1f232c; border-radius:8px 8px 0 0;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self.video_label, stretch=1)

        self.setStyleSheet("border:1px solid #2a2e37; border-radius:8px;")
        self.set_status(CameraStatus.UNKNOWN)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._render)     # re-fit the picture to the new tile size

    def _on_audio_btn(self, on: bool):
        self.audio_btn.setText("🔊" if on else "🔇")
        self.audio_toggled.emit(self.cfg.id, on)

    def set_audio_on(self, on: bool):
        """Sync the button without emitting (used when another tile takes the audio)."""
        self.audio_btn.blockSignals(True)
        self.audio_btn.setChecked(on)
        self.audio_btn.setText("🔊" if on else "🔇")
        self.audio_btn.blockSignals(False)

    def set_status(self, status: CameraStatus):
        self.status = status
        text, color = STATUS_LABELS[status]
        self.title_label.setText(self.cfg.name)
        self.status_label.setText(f"● {text}")
        self.status_label.setStyleSheet(f"color:{color}; font-weight:bold; padding:2px;")
        if status in (CameraStatus.OFFLINE, CameraStatus.DEAD, CameraStatus.AUTH_FAILED):
            self.video_label.setText(f"{text}\n\nקליק ימני ← התחבר מחדש")

    def begin_reconnect(self, main: bool = False):
        """Shown while a fresh connection is being made: clear the old picture and the error."""
        self._last_img = None
        self.video_label.setPixmap(QPixmap())
        self.set_status(CameraStatus.UNKNOWN)
        self.video_label.setText("מתחבר מחדש...")
        if self.cfg.protocol == "dvrip":
            self.hd_btn.blockSignals(True)
            self.hd_btn.setChecked(main)
            self.hd_btn.blockSignals(False)

    def mouseDoubleClickEvent(self, event):
        self.activated.emit(self.cfg.id)

    def set_title(self, name: str):
        self.cfg.name = name
        self.title_label.setText(name)

    def update_frame(self, frame):
        if isinstance(frame, QImage):          # already scaled + converted by the worker thread
            qimg = frame
        else:                                  # RTSP path: BGR ndarray
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()   # copy: we keep it
        self._last_img = qimg
        self._render()

    # ---- digital zoom ---------------------------------------------------
    def _render(self):
        img = self._last_img
        if img is None:
            return
        if self._zoom.active:
            x, y, w, h = self._zoom.crop_rect(img.width(), img.height())
            img = img.copy(x, y, w, h)
        pix = QPixmap.fromImage(img).scaled(
            self.video_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self.video_label.setPixmap(pix)

    def _shown_geometry(self):
        """(offset_x, offset_y, width, height) of the picture inside video_label."""
        img = self._last_img
        if img is None:
            return 0.0, 0.0, 0.0, 0.0
        if self._zoom.active:
            _x, _y, w, h = self._zoom.crop_rect(img.width(), img.height())
        else:
            w, h = img.width(), img.height()
        return fit_rect(w, h, self.video_label.width(), self.video_label.height())

    def _set_zoom(self, factor: float | None):
        """factor None = back to 1x."""
        if factor is None:
            self._zoom.reset()
        else:
            self._zoom.zoom_at(factor)
        self._after_zoom_change()

    def _after_zoom_change(self):
        on = self._zoom.active
        self.zoom_label.setText(f"🔍 x{self._zoom.zoom:.1f}")
        self.zoom_label.setVisible(on)
        self.video_label.setCursor(Qt.CursorShape.OpenHandCursor if on else Qt.CursorShape.ArrowCursor)
        self._render()

    def wheelEvent(self, event):
        dy = event.angleDelta().y()
        if dy == 0 or self._last_img is None:
            return
        ox, oy, w, h = self._shown_geometry()
        pos = self.video_label.mapFrom(self, event.position().toPoint())
        u = (pos.x() - ox) / w if w else 0.5
        v = (pos.y() - oy) / h if h else 0.5
        self._zoom.wheel(dy / 120.0, u, v)
        self._after_zoom_change()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._zoom.active:
            self._drag_pos = event.position()
            self.video_label.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None:
            _ox, _oy, w, h = self._shown_geometry()
            if w and h:
                d = event.position() - self._drag_pos
                self._zoom.pan(d.x() / w, d.y() / h)
                self._render()
            self._drag_pos = event.position()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_pos is not None:
            self._drag_pos = None
            self.video_label.setCursor(Qt.CursorShape.OpenHandCursor if self._zoom.active
                                       else Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    # ---- snapshot -------------------------------------------------------
    def _flash(self, text: str, color: str = "#2ecc71"):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color:{color}; font-weight:bold; padding:2px;")
        self._flash_timer.start(3000)

    def save_snapshot(self):
        if self._last_img is None:
            return
        folder = Path.home() / "Pictures" / "CameraSnapshots"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r'[\\/:*?"<>|]+', "_", self.cfg.name).strip() or "camera"
            path = folder / f"{safe}_{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
            ok = self._last_img.save(str(path))
        except OSError:
            ok = False
        if ok:
            self.status_label.setToolTip(str(path))
            self._flash("📸 נשמר בתיקיית Pictures\\CameraSnapshots")
        else:
            self._flash("שמירת התמונה נכשלה", "#e74c3c")

    # ---- right-click menu -----------------------------------------------
    def contextMenuEvent(self, event):
        menu = QMenu(self)
        broken = self.status in (CameraStatus.OFFLINE, CameraStatus.DEAD, CameraStatus.AUTH_FAILED)
        a_reconnect = menu.addAction("🔄 התחבר מחדש" + ("   ← המצלמה לא מחוברת" if broken else ""))
        if broken:
            f = a_reconnect.font()
            f.setBold(True)
            a_reconnect.setFont(f)
        a_swap = None
        if self.cfg.protocol == "dvrip":
            a_swap = menu.addAction("🔀 התחבר מחדש בזרם משני (קל)" if self.hd_btn.isChecked()
                                    else "🔀 התחבר מחדש בזרם ראשי (HD)")
        a_edit = menu.addAction("✏ שנה פרטי חיבור (כתובת / סיסמה)…")
        if broken:
            why = menu.addAction("ℹ " + STATUS_HINTS[self.status])
            why.setEnabled(False)
        menu.addSeparator()
        a_play = menu.addAction("📼 צפייה בהקלטות (לאחור)")
        zoom_menu = menu.addMenu("🔍 זום דיגיטלי  (גם בגלגלת העכבר)")
        zoom_actions = {zoom_menu.addAction(f"x{f}"): f for f in (2, 4, 8)}
        a_zoom_off = zoom_menu.addAction("איפוס זום")
        a_zoom_off.setEnabled(self._zoom.active)
        a_snap = menu.addAction("📸 צילום תמונה")
        a_snap.setEnabled(self._last_img is not None)
        a_audio = a_hd = None
        if self.cfg.protocol == "dvrip":
            a_audio = menu.addAction("🔊 שמע")
            a_audio.setCheckable(True)
            a_audio.setChecked(self.audio_btn.isChecked())
            a_hd = menu.addAction("HD  איכות מלאה (כבד)")
            a_hd.setCheckable(True)
            a_hd.setChecked(self.hd_btn.isChecked())
        a_full = menu.addAction("🖥 חזרה לתצוגה רגילה" if self.fullscreen else "🖥 מסך מלא")
        a_grid_full = menu.addAction("⛶ יציאה ממסך מלא (F11)" if self.grid_full
                                     else "⛶ כל המצלמות במסך מלא (F11)")
        menu.addSeparator()
        a_close = menu.addAction("✖ סגור מצלמה")
        a_close_all = menu.addAction("✖ סגור את כל המצלמות")
        a_hide = a_hide_dead = None
        menu.addSeparator()
        if broken:
            a_hide = menu.addAction("🗑 הסר מצלמה זו (לא עובדת)")
        a_hide_dead = menu.addAction("🗑 הסר את כל המצלמות שלא עובדות")

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen is a_reconnect:
            self.reconnect_requested.emit(self.cfg.id, None)
        elif a_swap is not None and chosen is a_swap:
            self.reconnect_requested.emit(self.cfg.id, not self.hd_btn.isChecked())
        elif chosen is a_edit:
            self.edit_connection_requested.emit(self.cfg)
        elif chosen is a_play:
            self.playback_requested.emit(self.cfg)
        elif chosen in zoom_actions:
            self._set_zoom(zoom_actions[chosen])
        elif chosen is a_zoom_off:
            self._set_zoom(None)
        elif chosen is a_snap:
            self.save_snapshot()
        elif a_audio is not None and chosen is a_audio:
            self.audio_btn.setChecked(not self.audio_btn.isChecked())
        elif a_hd is not None and chosen is a_hd:
            self.hd_btn.setChecked(not self.hd_btn.isChecked())
        elif chosen is a_full:
            self.activated.emit(self.cfg.id)
        elif chosen is a_grid_full:
            self.grid_fullscreen_requested.emit()
        elif chosen is a_close:
            self.close_requested.emit(self.cfg.id)
        elif chosen is a_close_all:
            self.close_all_requested.emit()
        elif a_hide is not None and chosen is a_hide:
            self.hide_requested.emit(self.cfg.id)
        elif chosen is a_hide_dead:
            self.hide_dead_requested.emit()
