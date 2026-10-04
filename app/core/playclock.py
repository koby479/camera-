"""Player maths with no Qt / PyAV in it, so it is unit-tested: speed steps, the playback clock,
the short history of recent frames (for instant step-back) and time formatting."""
from __future__ import annotations

import time

SPEEDS = (0.25, 0.5, 1.0, 1.5, 2.0, 4.0, 8.0, 16.0)


def nearest_speed_index(speed: float) -> int:
    return min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - speed))


def step_speed(speed: float, direction: int) -> float:
    """The next faster (+1) or slower (-1) entry of SPEEDS, never past either end."""
    i = nearest_speed_index(speed) + (1 if direction > 0 else -1)
    return SPEEDS[max(0, min(len(SPEEDS) - 1, i))]


def fmt_clock(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        seconds = 0
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class PlaybackClock:
    """Maps wall-clock time to media time. direction +1 = forward, -1 = backward."""

    def __init__(self, now=time.monotonic):
        self._now = now
        self._media = 0.0
        self._wall = now()
        self.speed = 1.0
        self.direction = 1

    def start(self, media_time: float, speed: float | None = None, direction: int | None = None):
        if speed is not None:
            self.speed = speed
        if direction is not None:
            self.direction = direction
        self._media = media_time
        self._wall = self._now()

    def media_time(self) -> float:
        return self._media + self.direction * self.speed * (self._now() - self._wall)

    def due_in(self, frame_time: float) -> float:
        """Seconds of real time until this frame should be on screen (negative = already late)."""
        return self.direction * (frame_time - self.media_time()) / self.speed


class FrameHistory:
    """The last few shown frames, so a click on 'previous frame' is instant instead of a seek +
    decode. `cursor` is the frame on screen; stepping back moves it, stepping forward moves it
    back towards the newest."""

    def __init__(self, maxlen: int = 30):
        self.maxlen = maxlen
        self._items: list = []
        self._i = -1

    def clear(self):
        self._items, self._i = [], -1

    def __len__(self):
        return len(self._items)

    def push(self, t: float, image):
        del self._items[self._i + 1:]            # whatever lay "ahead" of the cursor is stale now
        self._items.append((t, image))
        if len(self._items) > self.maxlen:
            del self._items[0]
        self._i = len(self._items) - 1

    def back(self):
        if self._i > 0:
            self._i -= 1
            return self._items[self._i]
        return None

    def forward(self):
        if 0 <= self._i < len(self._items) - 1:
            self._i += 1
            return self._items[self._i]
        return None
