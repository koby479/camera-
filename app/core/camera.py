"""
Core camera model + a background thread that pulls frames from an
RTSP stream and reports connection status (ACTIVE / DEAD / OFFLINE).

ACTIVE  - stream is connecting and frames are arriving normally
DEAD    - we can open a connection / the device answers, but no valid
          frames come through (common with cheap Chinese cameras whose
          proprietary CMS blocks the plain RTSP port, or a corrupted stream)
OFFLINE - no response from the device at all (wrong IP/port, powered off,
          network unreachable)
"""
from __future__ import annotations

import socket
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

import cv2
from PyQt6.QtCore import QThread, pyqtSignal


class CameraStatus(Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"      # פעילה
    DEAD = "dead"           # מתה (מגיבה אך אין תמונה תקינה)
    OFFLINE = "offline"     # לא מחוברת


@dataclass
class CameraConfig:
    """Persisted definition of a single camera (manual or discovered from an NVR)."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "מצלמה חדשה"
    host: str = ""
    port: int = 554
    username: str = ""
    password: str = ""
    rtsp_path: str = "/"          # e.g. /Streaming/Channels/101 or /cam/realmonitor
    onvif_port: int = 80          # used only for discovery / re-probing, not for the stream
    parent_nvr_id: str | None = None   # None => "single camera" entry, else id of the owning NVR
    transport: str = "tcp"        # tcp | udp

    @property
    def rtsp_url(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        path = self.rtsp_path if self.rtsp_path.startswith("/") else f"/{self.rtsp_path}"
        return f"rtsp://{auth}{self.host}:{self.port}{path}"


class CameraWorker(QThread):
    """One thread per camera tile. Emits frames + status changes."""

    frame_ready = pyqtSignal(str, object)          # camera_id, numpy frame (BGR)
    status_changed = pyqtSignal(str, object)        # camera_id, CameraStatus

    def __init__(self, cfg: CameraConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._running = True
        self._last_status: CameraStatus | None = None

    def stop(self):
        self._running = False
        self.wait(2000)

    def _emit_status(self, status: CameraStatus):
        if status != self._last_status:
            self._last_status = status
            self.status_changed.emit(self.cfg.id, status)

    def _tcp_reachable(self, timeout=2.0) -> bool:
        try:
            with socket.create_connection((self.cfg.host, self.cfg.port), timeout=timeout):
                return True
        except OSError:
            return False

    def run(self):
        retry_delay = 3
        while self._running:
            if not self._tcp_reachable():
                self._emit_status(CameraStatus.OFFLINE)
                time.sleep(retry_delay)
                continue

            options = (
                "rtsp_transport;tcp" if self.cfg.transport == "tcp" else "rtsp_transport;udp"
            )
            import os
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = options

            cap = cv2.VideoCapture(self.cfg.rtsp_url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                self._emit_status(CameraStatus.DEAD)
                cap.release()
                time.sleep(retry_delay)
                continue

            consecutive_failures = 0
            while self._running:
                ok, frame = cap.read()
                if not ok or frame is None:
                    consecutive_failures += 1
                    if consecutive_failures > 10:
                        self._emit_status(CameraStatus.DEAD)
                        break
                    time.sleep(0.2)
                    continue

                consecutive_failures = 0
                self._emit_status(CameraStatus.ACTIVE)
                self.frame_ready.emit(self.cfg.id, frame)

            cap.release()
            time.sleep(retry_delay)
