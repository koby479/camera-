from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.camera import CameraConfig
from app.core import discovery


class NvrProbeWorker(QThread):
    """Runs enumerate_nvr_channels() off the GUI thread. Without this, clicking
    an NVR would freeze the whole window for as long as the ONVIF call takes
    (which, for an unreachable device, could be a long time) -- from the
    user's point of view that looks exactly like 'the app doesn't work',
    not 'still loading'."""

    succeeded = pyqtSignal(object, list)   # nvr_cfg, list[CameraConfig]
    failed = pyqtSignal(object, str)        # nvr_cfg, error message

    def __init__(self, nvr_cfg: CameraConfig, parent=None):
        super().__init__(parent)
        self.nvr_cfg = nvr_cfg

    def run(self):
        try:
            channels = discovery.enumerate_nvr_channels(self.nvr_cfg)
            self.succeeded.emit(self.nvr_cfg, channels)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.failed.emit(self.nvr_cfg, str(exc))
