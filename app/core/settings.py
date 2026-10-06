"""Small user settings file (%APPDATA%\\UniversalCamViewer\\settings.json): which video decoder to use and where
ffmpeg.exe is. Plain JSON, tolerant of a damaged or missing file, no Qt dependency."""
from __future__ import annotations

import json
import os

from app.core.store import APP_DIR

SETTINGS_FILE = APP_DIR / "settings.json"
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            _cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def get(key: str, default=None):
    return _load().get(key, default)


def put(key: str, value) -> None:
    data = _load()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        pass                                    # a setting that cannot be saved is only forgotten at the next start


def reload() -> None:
    global _cache
    _cache = None
