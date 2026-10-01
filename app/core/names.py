"""User-chosen names for NVR channels. Channels are re-discovered from the NVR every
time, so their names are kept in a separate small file keyed by NVR id + channel."""
from __future__ import annotations

import json
import os

from app.core.store import APP_DIR

NAMES_FILE = APP_DIR / "channel_names.json"


def _load() -> dict:
    try:
        return json.loads(NAMES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get_name(nvr_id: str | None, channel: int) -> str | None:
    return _load().get(f"{nvr_id}:{channel}")


def set_name(nvr_id: str | None, channel: int, name: str):
    data = _load()
    data[f"{nvr_id}:{channel}"] = name
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NAMES_FILE.with_name(NAMES_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, NAMES_FILE)
