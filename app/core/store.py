"""Simple local JSON store so the sidebar tree (NVRs + single cameras)
survives an app restart. No cloud, nothing leaves the machine."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, fields
from pathlib import Path

from app.core.config import CameraConfig

APP_DIR = Path(os.getenv("APPDATA") or Path.home()) / "UniversalCamViewer"
CONFIG_FILE = APP_DIR / "devices.json"

DEFAULT_MAX_TILES = 64  # default grid budget, NOT a hard limit -- see load_all()


def _ensure_dir():
    APP_DIR.mkdir(parents=True, exist_ok=True)


def _from_dict(item) -> CameraConfig | None:
    """Tolerant: fields written by a newer/older version are ignored instead of crashing the start."""
    if not isinstance(item, dict):
        return None
    known = {f.name for f in fields(CameraConfig)}
    try:
        return CameraConfig(**{k: v for k, v in item.items() if k in known})
    except TypeError:
        return None


def load_all() -> tuple[list[CameraConfig], list[CameraConfig]]:
    """Returns (nvrs, single_cameras). No cap is enforced here -- the UI's
    64-tile grid is just the *default* window size; adding more simply grows it.

    A damaged file must never stop the program from starting: it is renamed to
    devices.json.broken (so nothing is lost) and the program starts empty."""
    _ensure_dir()
    if not CONFIG_FILE.exists():
        return [], []
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        try:
            os.replace(CONFIG_FILE, CONFIG_FILE.with_name(CONFIG_FILE.name + ".broken"))
        except OSError:
            pass
        return [], []
    nvrs = [c for c in map(_from_dict, data.get("nvrs", [])) if c]
    singles = [c for c in map(_from_dict, data.get("singles", [])) if c]
    return nvrs, singles


def save_all(nvrs: list[CameraConfig], singles: list[CameraConfig]):
    """Write to a temp file and swap it in, so a crash / power cut mid-save cannot leave a half file."""
    _ensure_dir()
    data = {
        "nvrs": [asdict(c) for c in nvrs],
        "singles": [asdict(c) for c in singles],
    }
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)
