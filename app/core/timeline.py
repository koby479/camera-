"""Media time (seconds from the start of the recording) of every frame of one downloaded piece.

The NVR writes the real time into every key frame (1-second resolution). From the first of them and the frame rate
every frame gets its time, so a piece that was started in the middle of a recording lands at the right place on the
seek bar. The frame rate written in the stream is trusted unless, over a long enough stretch, the stamps say
otherwise. Qt-free, unit-tested."""
from __future__ import annotations

import math

DEFAULT_FPS = 25.0
MIN_MEASURE = 8.0       # seconds of stamps needed before the rate can be measured at all
TRUST_SPAN = 60.0       # beyond this the measured rate replaces the one in the stream if they disagree by > 5 %


class Timeline:
    def __init__(self, origin: int | None, hint_fps: float | None, start_guess: float = 0.0):
        self.origin = origin                  # wall-clock seconds of the start of the recording (None: no stamps used)
        self.hint = float(hint_fps) if hint_fps else None
        self.start_guess = start_guess        # where the piece starts if no stamp is known yet
        self._first: tuple[int, float] | None = None     # (frame number, media seconds) of the first stamped key frame
        self._last: tuple[int, float] | None = None

    def add_key(self, index: int, stamp_seconds: int | None):
        """A key frame number `index` carries the time stamp_seconds (NVR wall clock)."""
        if self.origin is None or stamp_seconds is None:
            return
        t = stamp_seconds + 0.5 - self.origin             # the NVR truncates to the second
        if self._first is None:
            self._first = self._last = (index, t)
        elif index > self._last[0] and t > self._last[1]:
            self._last = (index, t)

    @property
    def fps(self) -> float:
        if self._first is not None and self._last[0] > self._first[0]:
            span = self._last[1] - self._first[1]
            if span >= MIN_MEASURE:
                measured = (self._last[0] - self._first[0]) / span
                if not self.hint:
                    return measured
                if span >= TRUST_SPAN and abs(measured / self.hint - 1.0) > 0.05:
                    return measured
        return self.hint or DEFAULT_FPS

    def time_of(self, index: int) -> float:
        if self._first is None:
            return self.start_guess + index / self.fps
        i0, t0 = self._first
        return t0 + (index - i0) / self.fps

    def index_at(self, t: float) -> int:
        """The first frame that is at or after time t (half a frame of slack)."""
        i0, t0 = self._first if self._first is not None else (0, self.start_guess)
        return max(0, i0 + math.ceil((t - t0) * self.fps - 0.5 - 1e-9))
