"""Formatting helpers for the recordings list (no Qt, unit-tested)."""
from __future__ import annotations

import re
from datetime import datetime

_FMT = "%Y-%m-%d %H:%M:%S"


def fmt_size(nbytes: int) -> str:
    if not nbytes or nbytes <= 0:
        return "-"
    mb = nbytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{nbytes / 1024:.0f} KB"


def duration_seconds(begin: str, end: str) -> int | None:
    try:
        d = datetime.strptime(end, _FMT) - datetime.strptime(begin, _FMT)
    except (ValueError, TypeError):
        return None
    secs = int(d.total_seconds())
    return secs if secs >= 0 else None


def fmt_duration(secs: int | None) -> str:
    if secs is None:
        return "-"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def group_by_day(files) -> list[tuple[str, list[dict]]]:
    """[(day, files sorted by time)] with the newest day first."""
    days: dict[str, list[dict]] = {}
    for f in files:
        days.setdefault(f["begin"][:10], []).append(f)
    return [(d, sorted(days[d], key=lambda x: x["begin"])) for d in sorted(days, reverse=True)]


def safe_filename(channel: int, item: dict, ext: str = "h264") -> str:
    stamp = item["begin"].replace(":", "-").replace(" ", "_")
    return f"channel{channel + 1}_{stamp}.{ext}"


_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})")


def parse_stamp(filename: str) -> datetime | None:
    """Start time of a recording from a name made by safe_filename ('channel3_2026-10-01_14-03-22.mp4')."""
    m = _STAMP.search(filename)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))
    except ValueError:
        return None


def clock_text(wall_seconds: float) -> str:
    """HH:MM:SS of a time on the NVR's wall-clock scale (see frameindex.wall_seconds)."""
    return datetime.utcfromtimestamp(wall_seconds).strftime("%H:%M:%S")
