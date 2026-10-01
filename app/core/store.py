"""Simple local JSON store so the sidebar tree (NVRs + single cameras)
survives an app restart. No cloud, nothing leaves the machine."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from app.core.camera import CameraConfig

APP_DIR = Path(os.getenv("APPDATA") or Path.home()) / "UniversalCamViewer"
CONFIG_FILE = APP_DIR / "devices.json"

DEFAULT_MAX_TILES = 64  # default grid budget, NOT a hard limit -- see load_all()


def _ensure_dir():
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load_all() -> tuple[list[CameraConfig], list[CameraConfig]]:
    """Returns (nvrs, single_cameras). No cap is enforced here -- the UI's
    64-tile grid is just the *default* window size; adding more simply grows it."""
    _ensure_dir()
    if not CONFIG_FILE.exists():
        return [], []

    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    nvrs = [CameraConfig(**item) for item in data.get("nvrs", [])]
    singles = [CameraConfig(**item) for item in data.get("singles", [])]
    return nvrs, singles


def save_all(nvrs: list[CameraConfig], singles: list[CameraConfig]):
    _ensure_dir()
    data = {
        "nvrs": [asdict(c) for c in nvrs],
        "singles": [asdict(c) for c in singles],
    }
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
