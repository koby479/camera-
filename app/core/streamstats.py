"""Tiny per-stream health counter, so a stutter can be traced to its cause from app.log.

It tells three things apart:
  * gaps between NETWORK chunks long  -> the connection / the NVR is the bottleneck
  * chunks fine but gaps between decoded frames long -> decoding (CPU) is the bottleneck
  * frames dropped because the window was busy -> drawing (GUI thread) is the bottleneck
No Qt in here, so it is unit-tested.
"""
from __future__ import annotations

import time

STALL_SECONDS = 0.5


class StreamStats:
    def __init__(self, interval: float = 10.0, clock=time.monotonic):
        self._clock = clock
        self.interval = interval
        self.reports = 0
        self.notable = False                 # the last report contained a stall or dropped frames
        self._last_chunk: float | None = None   # kept across reports: a gap may span two of them
        self._last_frame: float | None = None
        self._reset(clock())

    def _reset(self, now: float):
        self._start = now
        self._frames = self._dropped = self._stalls = 0
        self._max_frame_gap = self._max_chunk_gap = 0.0

    def chunk(self):
        now = self._clock()
        if self._last_chunk is not None:
            self._max_chunk_gap = max(self._max_chunk_gap, now - self._last_chunk)
        self._last_chunk = now

    def frame(self):
        now = self._clock()
        if self._last_frame is not None:
            gap = now - self._last_frame
            self._max_frame_gap = max(self._max_frame_gap, gap)
            if gap > STALL_SECONDS:
                self._stalls += 1
        self._last_frame = now
        self._frames += 1

    def dropped(self):
        self._dropped += 1

    def report(self) -> str | None:
        """One summary line once per interval, else None."""
        now = self._clock()
        elapsed = now - self._start
        if elapsed < self.interval:
            return None
        self.reports += 1
        self.notable = self._stalls > 0 or self._dropped > 0
        line = (f"{self._frames / elapsed:.1f} fps, longest gap between frames {self._max_frame_gap * 1000:.0f} ms "
                f"(between network chunks {self._max_chunk_gap * 1000:.0f} ms), "
                f"stalls over {int(STALL_SECONDS * 1000)} ms: {self._stalls}, "
                f"frames dropped because the window was busy: {self._dropped}")
        self._reset(now)
        return line
