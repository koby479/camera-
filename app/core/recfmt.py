"""Formatting helpers for the recordings list (no Qt, unit-tested)."""
from __future__ import annotations

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
