"""Video source for one piece of a recording that is being downloaded: reads the raw file and its frame index
while the download keeps appending to both. Same interface as VideoSource, plus:
  start_time / buffered_until   the stretch (seconds from the start of the recording) that can be played now
  complete                      the whole piece is here
and frames_from() yields WAITING while the wanted frame has not arrived, so the player can show
"buffering" and stay responsive instead of freezing."""
from __future__ import annotations

import bisect

from app.core import dvrip, frameindex
from app.core.streamcache import StreamState
from app.core.timeline import Timeline
from app.core.videosource import WAITING, VideoSource

LAG = 8       # frames the decoder holds back (threads): the last few downloaded frames are not offered yet


class RawStreamSource:
    def __init__(self, raw_path, idx_path, state: StreamState, origin: int | None = None, start_guess: float = 0.0):
        self.raw_path, self.idx_path, self.state = raw_path, idx_path, state
        self.origin, self.start_guess = origin, start_guess
        self._recs: list[tuple[int, int]] = []     # (offset, length) per frame
        self._keys: list[int] = []                 # frame numbers of the key frames
        self._timeline = Timeline(origin, state.fps, start_guess)
        self._fh = None
        self.width = self.height = 0
        self._refresh()

    to_rgb = staticmethod(VideoSource.to_rgb)

    # ---- what the player asks about ---------------------------------------
    @property
    def fps(self) -> float:
        self._timeline.hint = float(self.state.fps) if self.state.fps else self._timeline.hint
        return self._timeline.fps

    @property
    def complete(self) -> bool:
        return bool(self.state.complete)

    @property
    def frame_count(self) -> int:
        return len(self._recs)

    @property
    def start_time(self) -> float:
        return self._timeline.time_of(0)

    @property
    def duration(self) -> float:
        n = len(self._recs)
        end = self._timeline.time_of(n)
        if self.complete:
            return end
        return max(float(self.state.declared or 0.0), end)

    @property
    def buffered_until(self) -> float:
        n = len(self._recs)
        if n == 0:
            return self.start_guess
        return self._timeline.time_of(n - 1 if self.complete else max(0, n - 1 - LAG))

    # ---- reading ------------------------------------------------------------------
    def _refresh(self):
        total = frameindex.count_records(self.idx_path)
        if total < len(self._recs):                # the download started over: forget everything
            self._recs, self._keys = [], []
            self._timeline = Timeline(self.origin, self.state.fps, self.start_guess)
        if total > len(self._recs):
            for off, length, key, stamp in frameindex.read_records(self.idx_path, len(self._recs), total):
                if key:
                    self._keys.append(len(self._recs))
                    if stamp:
                        self._timeline.add_key(len(self._recs), frameindex.stamp_to_seconds(stamp))
                self._recs.append((off, length))
        if self._recs and not self._keys:          # key frames not recognised: the download starts at the very beginning,
            self._keys.append(0)                   # which is always a key frame

    def refresh(self):
        self._refresh()

    def _open(self):
        if self._fh is None:
            self._fh = open(self.raw_path, "rb")
        return self._fh

    def _dead(self) -> bool:
        return self.complete or bool(self.state.failed)

    def frames_from(self, t: float):
        """Yield (time, frame) from time t on, or WAITING while the data is not there yet."""
        while True:                                # wait for the wanted frame and a key frame before it
            self._refresh()
            start = self._timeline.index_at(t)
            at = bisect.bisect_right(self._keys, start) - 1
            if at >= 0 and start < len(self._recs):
                break
            if self._dead():
                return
            yield WAITING
        first = self._keys[at]
        decoder = dvrip.H264Decoder()
        fh = self._open()
        i, out = first, 0
        while True:
            if i >= len(self._recs):
                self._refresh()
                if i >= len(self._recs):
                    if self._dead():
                        return
                    yield WAITING
                    continue
            offset, length = self._recs[i]
            fh.seek(offset)
            data = fh.read(length)
            if len(data) < length:                 # not on disk yet (should not happen): try again shortly
                if self._dead():
                    return
                yield WAITING
                continue
            i += 1
            for frame in decoder.decode_raw(data):
                number = first + out
                out += 1
                if number < start:
                    continue
                if not self.width:
                    self.width, self.height = frame.width, frame.height
                yield self._timeline.time_of(number), frame

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None
