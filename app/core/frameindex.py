"""An index of the frames of a raw video file that is still being written (offset, size, key frame?),
so the player can jump to any already-downloaded point without a container format. Qt-free, unit-tested."""
from __future__ import annotations

import os
import struct
from pathlib import Path

REC = struct.Struct("<QIB")          # offset in the raw file, length, 1 = key frame
REC_SIZE = REC.size


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


def read_records(path, start: int, stop: int) -> list[tuple[int, int, bool]]:
    """Records start..stop-1 (only whole records are ever returned)."""
    try:
        with open(path, "rb") as f:
            f.seek(start * REC_SIZE)
            blob = f.read(max(0, stop - start) * REC_SIZE)
    except OSError:
        return []
    return [(o, n, bool(k)) for o, n, k in REC.iter_unpack(blob[:len(blob) // REC_SIZE * REC_SIZE])]


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

    def add(self, offset: int, length: int, key: bool):
        self._open()
        self._fh.write(REC.pack(offset, length, 1 if key else 0))

    def flush(self):
        if self._fh is not None:
            self._fh.flush()
