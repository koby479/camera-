"""An index of the frames of a raw video file that is still being written (offset, size, key frame?),
so the player can jump to any already-downloaded point without a container format. Qt-free, unit-tested."""
from __future__ import annotations

import calendar
import os
import struct
from datetime import datetime
from pathlib import Path

REC = struct.Struct("<QIBI")         # offset in the raw file, length, 1 = key frame, time stamp of a key frame (XM format, 0 = none)
REC_SIZE = REC.size


def stamp_to_seconds(raw: int) -> int | None:
    """The time the NVR writes into every key-frame header (4 bytes, bit-packed) as seconds since 1970 on the
    NVR's own wall clock, or None when it is not a valid date."""
    try:
        dt = datetime((raw >> 26) + 2000, (raw >> 22) & 0xF, (raw >> 17) & 0x1F,
                      (raw >> 12) & 0x1F, (raw >> 6) & 0x3F, raw & 0x3F)
    except ValueError:
        return None
    return calendar.timegm(dt.timetuple())


def seconds_to_stamp(seconds: int) -> int:
    d = datetime.utcfromtimestamp(seconds)
    return (d.second | d.minute << 6 | d.hour << 12 | d.day << 17 | d.month << 22 | (d.year - 2000) << 26)


def wall_seconds(text: str) -> int | None:
    """'2026-10-01 17:10:00' (how the NVR lists recordings) -> seconds on the same wall-clock scale."""
    try:
        return calendar.timegm(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timetuple())
    except (ValueError, TypeError):
        return None


def is_key_frame(data: bytes, codec: str | None) -> bool:
    """True when this frame can be decoded on its own (it carries an IDR / IRAP picture or parameter sets)."""
    if codec not in ("h264", "hevc"):
        return False
    i = data.find(b"\x00\x00\x01")
    while i != -1 and i + 3 < len(data):
        b = data[i + 3]
        if codec == "hevc":
            t = (b >> 1) & 0x3F
            if 16 <= t <= 21 or 32 <= t <= 34:          # IRAP pictures, VPS / SPS / PPS
                return True
        elif b & 0x80 == 0 and (b & 0x1F) in (5, 7):    # IDR slice, SPS
            return True
        i = data.find(b"\x00\x00\x01", i + 3)
    return False


def count_records(path) -> int:
    try:
        return os.path.getsize(path) // REC_SIZE
    except OSError:
        return 0


def read_records(path, start: int, stop: int) -> list[tuple[int, int, bool, int]]:
    """Records start..stop-1 (only whole records are ever returned)."""
    try:
        with open(path, "rb") as f:
            f.seek(start * REC_SIZE)
            blob = f.read(max(0, stop - start) * REC_SIZE)
    except OSError:
        return []
    return [(o, n, bool(k), st) for o, n, k, st in REC.iter_unpack(blob[:len(blob) // REC_SIZE * REC_SIZE])]


class IndexWriter:
    """Appends records; the downloader calls flush() after the video bytes were flushed, so the index
    never points past the end of the video file."""

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None

    def _open(self):
        if self._fh is None:
            self._fh = open(self.path, "ab")

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def reset(self):
        self.close()
        self.path.write_bytes(b"")

    def truncate(self, frames: int) -> bool:
        """Keep only the first `frames` records. False when the file has fewer (it cannot be trusted)."""
        self.close()
        if count_records(self.path) < frames:
            return False
        with open(self.path, "r+b") as f:
            f.truncate(frames * REC_SIZE)
        return True

    def add(self, offset: int, length: int, key: bool, stamp: int = 0):
        self._open()
        self._fh.write(REC.pack(offset, length, 1 if key else 0, stamp))

    def flush(self):
        if self._fh is not None:
            self._fh.flush()
