"""Video source for a recording that is still being downloaded: reads the raw file and its frame index
while the download keeps appending to both. Same interface as VideoSource, plus:
  buffered_until  how far (seconds) the player may go right now
  complete        the whole recording is here
and frames_from() yields WAITING while the wanted frame has not arrived, so the player can show
"buffering" and stay responsive instead of freezing."""
from __future__ import annotations

import bisect
import math

from app.core import dvrip, frameindex
from app.core.streamcache import DEFAULT_FPS, StreamState
from app.core.videosource import WAITING, VideoSource

LAG = 8       # frames the decoder holds back (threads): the last few downloaded frames are not offered yet


class RawStreamSource:
    def __init__(self, raw_path, idx_path, state: StreamState):
        self.raw_path, self.idx_path, self.state = raw_path, idx_path, state
        self._recs: list[tuple[int, int]] = []     # (offset, length) per frame
        self._keys: list[int] = []                 # frame numbers of the key frames
        self._fh = None
        self.width = self.height = 0
        self._refresh()

    to_rgb = staticmethod(VideoSource.to_rgb)

    # ---- what the player asks about ---------------------------------------
    @property
    def fps(self) -> float:
        return float(self.state.fps or DEFAULT_FPS)

    @property
    def complete(self) -> bool:
        return bool(self.state.complete)

    @property
    def duration(self) -> float:
        n = len(self._recs)
        if self.complete:
            return n / self.fps
        return max(float(self.state.declared or 0.0), n / self.fps)

    @property
    def buffered_until(self) -> float:
        n = len(self._recs)
        usable = n - 1 if self.complete else n - 1 - LAG
        return max(0.0, usable / self.fps)

    # ---- reading ------------------------------------------------------------------
    def _refresh(self):
        total = frameindex.count_records(self.idx_path)
        if total < len(self._recs):                # the download started over: forget everything
            self._recs, self._keys = [], []
        if total > len(self._recs):
            for off, length, key in frameindex.read_records(self.idx_path, len(self._recs), total):
                if key:
                    self._keys.append(len(self._recs))
                self._recs.append((off, length))
        if self._recs and not self._keys:          # key frames not recognised: the download starts at the very beginning,
            self._keys.append(0)                   # which is always a key frame

    def _open(self):
        if self._fh is None:
            self._fh = open(self.raw_path, "rb")
        return self._fh

    def _dead(self) -> bool:
        return self.complete or bool(self.state.failed)

    def frames_from(self, t: float):
        """Yield (time, frame) from time t on, or WAITING while the data is not there yet."""
        start = max(0, math.ceil(max(0.0, t) * self.fps - 0.5 - 1e-9))
        while True:                                # wait for the wanted frame and a key frame before it
            self._refresh()
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
                yield number / self.fps, frame

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None
