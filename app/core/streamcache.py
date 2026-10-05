"""Where the player keeps recordings while it plays them, what is shared between the download and the
player, and how the timeline's frame rate is chosen. Qt-free, unit-tested."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

CACHE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "UniversalCamViewer" / "cache"
MAX_CACHE_BYTES = 12 * 1024 ** 3
DEFAULT_FPS = 25.0


@dataclass
class StreamState:
    """Written by the download thread, read by the player (plain attributes: one writer each)."""
    declared: float = 0.0            # length of the recording as the NVR lists it (seconds)
    fps: float | None = None
    codec: str | None = None
    complete: bool = False
    failed: str = ""                 # why the download ended for good, if it did


@dataclass
class CachePaths:
    raw: Path
    idx: Path
    meta: Path


def cache_paths(host: str, channel: int, begin: str, stream_type: int, folder: Path | None = None,
                segment: int = 0) -> CachePaths:
    """segment: seconds from the start of the recording where this piece starts (0 = the beginning)."""
    name = f"{host}_ch{channel + 1}_{begin}_s{stream_type}" + (f"_o{segment}" if segment else "")
    stem = re.sub(r"[^0-9A-Za-z]+", "-", name).strip("-")
    d = Path(folder) if folder is not None else CACHE_DIR
    return CachePaths(d / f"{stem}.raw", d / f"{stem}.idx2", d / f"{stem}.meta.json")


def load_meta(paths: CachePaths) -> dict | None:
    try:
        return json.loads(paths.meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_meta(paths: CachePaths, **data):
    paths.meta.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.meta.with_name(paths.meta.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, paths.meta)


def pick_fps(hint: float | None, frames: int, declared: float, complete: bool) -> float:
    """Frame rate for the timeline. The rate written in the stream is used from the start; once the whole
    recording is here it is checked against frames / length, and the measured value wins if they disagree."""
    if complete and frames > 0 and declared > 0:
        measured = frames / declared
        if 1.0 <= measured <= 60.0 and (not hint or abs(measured / hint - 1.0) > 0.15):
            return measured
    return float(hint) if hint and 1 <= hint <= 60 else DEFAULT_FPS


def cleanup(keep_stems: set[str] = frozenset(), max_bytes: int = MAX_CACHE_BYTES, folder: Path | None = None):
    """Delete the oldest cached recordings until the folder is below max_bytes. Never touches keep_stems."""
    d = Path(folder) if folder is not None else CACHE_DIR
    try:
        files = [p for p in d.iterdir() if p.is_file()]
    except OSError:
        return
    groups: dict[str, list[Path]] = {}
    for p in files:
        groups.setdefault(p.name.split(".")[0], []).append(p)
    total = sum(p.stat().st_size for p in files)
    for stem, members in sorted(groups.items(), key=lambda kv: max(p.stat().st_mtime for p in kv[1])):
        if total <= max_bytes:
            break
        if stem in keep_stems:
            continue
        for p in members:
            try:
                total -= p.stat().st_size
                p.unlink()
            except OSError:
                pass
