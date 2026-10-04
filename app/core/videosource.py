"""Thin wrapper around PyAV for the local player: open a video file, jump to a time, and yield
decoded frames from there. Everything PyAV-specific lives here (the engine only sees times and
opaque frame objects), so the player logic can be tested with a fake source."""
from __future__ import annotations

import numpy as np

WAITING = object()      # what a source that is still being filled yields when the wanted frame is not there yet


class VideoSource:
    def __init__(self, path: str):
        import av                                   # imported late: only the player needs it
        self._av = av
        self.container = av.open(path)
        if not self.container.streams.video:
            self.container.close()
            raise ValueError("בקובץ אין וידאו")
        self.stream = self.container.streams.video[0]
        try:
            self.stream.thread_type = "AUTO"
        except Exception:                           # noqa: BLE001 - an older PyAV: single-threaded decode still works
            pass
        tb = self.stream.time_base
        self.tb = float(tb) if tb else 1.0 / 90000
        rate = self.stream.average_rate or getattr(self.stream, "guessed_rate", None)
        self.fps = float(rate) if rate else 25.0
        dur = None
        if self.stream.duration is not None:
            dur = float(self.stream.duration * self.tb)
        elif self.container.duration is not None:
            dur = self.container.duration / av.time_base
        self.duration = float(dur or 0.0)
        ctx = self.stream.codec_context
        self.width, self.height = int(ctx.width or 0), int(ctx.height or 0)

    def _time_of(self, frame) -> float | None:
        if frame.pts is None:
            return None
        return float(frame.pts * self.tb)

    def frames_from(self, t: float):
        """Yield (time, frame) for every frame from time t on. The seek lands on the key frame
        before t; the frames between it and t are decoded but not yielded."""
        t = max(0.0, t)
        tol = 0.5 / self.fps
        self.container.seek(int(t / self.tb), stream=self.stream, backward=True, any_frame=False)
        for frame in self.container.decode(self.stream):
            ft = self._time_of(frame)
            if ft is None or ft + tol < t:
                continue
            yield ft, frame

    @staticmethod
    def to_rgb(frame, max_width: int | None = None):
        w, h = frame.width, frame.height
        if max_width and w > max_width:
            h = max(2, int(round(h * max_width / w / 2)) * 2)
            w = max_width
        return np.ascontiguousarray(frame.reformat(width=w, height=h, format="rgb24").to_ndarray())

    def close(self):
        try:
            self.container.close()
        except Exception:                           # noqa: BLE001
            pass
