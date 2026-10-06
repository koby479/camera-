"""Camera definitions with no Qt / OpenCV dependency, so they can be imported (and tested) anywhere."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import quote


class CameraStatus(Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"      # פעילה
    DEAD = "dead"           # מתה (מגיבה אך אין תמונה תקינה)
    OFFLINE = "offline"     # לא מחוברת
    AUTH_FAILED = "auth_failed"   # שם משתמש/סיסמה שגויים
    NO_CAMERA = "no_camera"       # ה-NVR עונה אבל לא מגיע וידאו מהערוץ: אין מצלמה מחוברת


@dataclass
class CameraConfig:
    """Persisted definition of a single camera (manual or discovered from an NVR)."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "מצלמה חדשה"
    host: str = ""
    port: int = 554
    username: str = ""
    password: str = ""
    rtsp_path: str = "auto"       # "auto" = try common brand paths until one gives real frames
    onvif_port: int = 80          # used only for discovery / re-probing, not for the stream
    parent_nvr_id: str | None = None   # None => "single camera" entry, else id of the owning NVR
    transport: str = "tcp"        # tcp | udp
    protocol: str = "rtsp"        # NVR: "onvif" | "dvrip" (XM/Provision, port 34567); camera: "rtsp" | "dvrip"
    channel: int = 0              # channel index (0-based) for protocol == "dvrip"
    stream: str = "Main"          # "Main" | "Extra1" (sub-stream) for protocol == "dvrip"

    def rtsp_url_for(self, path: str) -> str:
        auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@" if self.username else ""
        p = path if path.startswith("/") else f"/{path}"
        return f"rtsp://{auth}{self.host}:{self.port}{p}"

    @property
    def rtsp_url(self) -> str:
        return self.rtsp_url_for(self.rtsp_path)
