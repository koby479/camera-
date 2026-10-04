"""The decoding thread of the local player: play, pause, seek, step, backwards, speed, A-B loop.

All state lives in this thread; the window only posts commands (thread-safe) and receives frames.
The video source is injected, so the logic is tested with a fake one."""
from __future__ import annotations

import sys
import threading
import time
import traceback

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

from app.core.playclock import FrameHistory, PlaybackClock, clamp
from app.core.videosource import VideoSource

REVERSE_CHUNK = 1.0     # seconds of video decoded at a time when playing backwards
LATE = 0.12             # a frame later than this is skipped instead of shown, so the speed stays right
MAX_SKIP_GAP = 0.10     # ...but something is shown at least this often


class PlayerEngine(QThread):
    frame = pyqtSignal(object, float)                # QImage, seconds from the start of the file
    opened = pyqtSignal(float, float, int, int)      # duration, fps, width, height
    paused_changed = pyqtSignal(bool)
    error = pyqtSignal(str)

    def __init__(self, path: str, source_factory=VideoSource, max_width: int = 1280):
        super().__init__()
        self.path = path
        self._factory = source_factory
        self.max_width = max_width
        self.pending = False                         # the window has not drawn the last frame yet
        self._lock = threading.Lock()
        self._cmd: dict = {}
        self._dirty = False
        self._running = True
        self.source = None
        self.duration = 0.0
        self.fps = 25.0
        # --- state below belongs to the engine thread ---
        self._paused = False
        self._speed = 1.0
        self._reverse = False
        self._loop_ab: tuple[float, float] | None = None
        self._loop_all = False
        self._pos = 0.0
        self._eof = False
        self._it = None
        self._carry = None                           # a frame already decoded but not yet shown
        self._rev: list = []                         # backwards: decoded (time, image) of the current chunk
        self.history = FrameHistory(60)
        self._clock = PlaybackClock()
        self._last_shown = 0.0

    # ---- commands (called from the window) -------------------------------
    def _post(self, **kw):
        with self._lock:
            if "seek" in kw:
                self._cmd.pop("seek_rel", None)      # an absolute jump cancels earlier relative ones
            if "seek_rel" in kw:
                kw["seek_rel"] += self._cmd.get("seek_rel", 0.0)
            if "step" in kw:
                kw["step"] += self._cmd.get("step", 0)
            self._cmd.update(kw)
            self._dirty = True

    def set_paused(self, paused: bool):
        self._post(paused=paused)

    def toggle_pause(self):
        with self._lock:
            now = self._cmd.get("paused", self._paused)
        self._post(paused=not now)

    def seek(self, seconds: float):
        self._post(seek=float(seconds))

    def seek_relative(self, seconds: float):
        self._post(seek_rel=float(seconds))

    def step(self, frames: int):
        self._post(step=int(frames))

    def set_speed(self, speed: float):
        self._post(speed=float(speed))

    def set_reverse(self, reverse: bool):
        self._post(reverse=bool(reverse))

    def set_loop(self, a: float | None, b: float | None):
        self._post(loop=(a, b) if (a is not None and b is not None and b > a) else None)

    def set_loop_all(self, on: bool):
        self._post(loop_all=bool(on))

    def request_stop(self):
        self._running = False
        self._dirty = True

    def stop(self):
        self.request_stop()
        self.wait(2000)

    # ---- thread ------------------------------------------------------------
    def run(self):
        try:
            self.source = self._factory(self.path)
        except Exception as exc:                     # noqa: BLE001 - shown in the window
            print(f"[player] open failed: {exc!r}", file=sys.stderr)
            self.error.emit(f"לא ניתן לפתוח את הקובץ: {exc}")
            return
        try:
            self.duration = float(self.source.duration or 0.0)
            self.fps = float(self.source.fps or 25.0)
            self.opened.emit(self.duration, self.fps, int(self.source.width), int(self.source.height))
            self._goto(0.0)
            self._loop()
        except Exception as exc:                     # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            self.error.emit(f"שגיאת נגן: {exc!r}")
        finally:
            self.source.close()

    def _loop(self):
        while self._running:
            if self._dirty and self._apply_commands():
                continue
            if self._paused:
                time.sleep(0.02)
                continue
            if self._reverse:
                self._reverse_tick()
            else:
                self._forward_tick()

    # ---- forward ------------------------------------------------------------
    def _forward_tick(self):
        item = self._pull()
        if item is None:
            self._reached_end()
            return
        t = item[0]
        wait = self._clock.due_in(t)
        if wait > 0:
            self._sleep(wait)
            if self._dirty:                          # a command came in while waiting: keep this frame for later
                self._carry = item
                return
        elif wait < -LATE and time.monotonic() - self._last_shown < MAX_SKIP_GAP:
            self._pos = t                            # too late: skip it, catch up
            return
        self._display(item, push=True)
        if self._loop_ab and t >= self._loop_ab[1] - 0.5 / self.fps:
            self._goto(self._loop_ab[0])

    def _start_forward(self, t: float):
        self._carry = None
        self._it = self.source.frames_from(max(0.0, t))

    def _pull(self):
        if self._carry is not None:
            item, self._carry = self._carry, None
            return item
        if self._it is None:
            return None
        try:
            return next(self._it)
        except StopIteration:
            self._it = None
            return None
        except Exception as exc:                     # noqa: BLE001 - a damaged tail must not kill the player
            print(f"[player] decode stopped: {exc!r}", file=sys.stderr)
            self._it = None
            return None

    def _resync_forward(self):
        """Continue forward from the frame on screen (after backwards play or a step)."""
        self._start_forward(self._pos)
        first = self._pull()
        if first is not None and first[0] > self._pos + 0.5 / self.fps:
            self._carry = first                      # the on-screen frame was skipped by the seek; this is the next one

    # ---- backwards ----------------------------------------------------------
    def _reverse_tick(self):
        if not self._rev:
            end = self._pos
            if end <= 1e-6:
                self._reached_end()
                return
            start = max(0.0, end - REVERSE_CHUNK)
            frames = []
            for t, payload in self.source.frames_from(start):
                if t >= end - 0.25 / self.fps:
                    break
                frames.append((t, self._to_image(payload)))
                if self._dirty:
                    return                           # interrupted by a command; it will be applied
            if not frames:
                self._pos = start
                if start <= 0.0:
                    self._reached_end()
                return
            self._rev = frames
        t, img = self._rev[-1]
        wait = self._clock.due_in(t)
        if wait > 0:
            self._sleep(wait)
            if self._dirty:
                return
        elif wait < -LATE and time.monotonic() - self._last_shown < MAX_SKIP_GAP:
            self._rev.pop()
            self._pos = t
            return
        self._rev.pop()
        self._pos = t
        self._emit(img, t)
        if self._loop_ab and t <= self._loop_ab[0] + 0.5 / self.fps:
            self._goto(self._loop_ab[1])

    # ---- jumping and stepping -----------------------------------------------
    def _goto(self, t: float):
        hi = self.duration - 1.0 / self.fps if self.duration > 0 else 1e9
        t = clamp(t, 0.0, max(0.0, hi))
        self.history.clear()
        self._rev = []
        self._eof = False
        self._start_forward(t)
        item = self._pull()
        if item is None:                             # past the last frame: show the last one instead
            self._start_forward(max(0.0, self.duration - 2.0 / self.fps))
            item = self._pull()
        if item is None:
            self._eof = True
            return
        self._display(item, force=True, push=True)
        self._clock_restart()

    def _do_step(self, n: int):
        self._set_paused(True)                       # (a frame already decoded, in _carry, is used by the first step)
        for _ in range(abs(n)):
            self._step_forward() if n > 0 else self._step_back()

    def _step_forward(self):
        shown = self.history.forward()
        if shown is not None:
            self._show_history(shown)
            return
        if self._it is None or self._rev:
            self._rev = []
            self._resync_forward()
        item = self._pull()
        if item is None:
            self._eof = True
            return
        self._display(item, force=True, push=True)

    def _step_back(self):
        shown = self.history.back()
        if shown is not None:
            self._show_history(shown)
            return
        prev = self._pos - 1.0 / self.fps
        if prev < -1e-6:
            return
        self._goto(prev)                             # one seek: slower than the cache, but exact

    def _show_history(self, shown):
        t, img = shown
        self._pos = t
        self._emit(img, t)

    # ---- helpers --------------------------------------------------------------
    def _apply_commands(self) -> bool:
        """Apply what the window asked for. True when the picture position changed."""
        with self._lock:
            cmd, self._cmd, self._dirty = self._cmd, {}, False
        if not cmd:
            return False
        moved = False
        if "loop" in cmd:
            self._loop_ab = cmd["loop"]
        if "loop_all" in cmd:
            self._loop_all = cmd["loop_all"]
        if "speed" in cmd:
            self._speed = cmd["speed"]
        if "reverse" in cmd and cmd["reverse"] != self._reverse:
            self._reverse = cmd["reverse"]
            self._rev = []
            self._eof = False
            self.history.clear()
            if not self._reverse:
                self._resync_forward()
        if "paused" in cmd:
            self._set_paused(cmd["paused"])
        if "seek" in cmd:
            self._goto(cmd["seek"])
            moved = True
        if "seek_rel" in cmd:
            self._goto(self._pos + cmd["seek_rel"])
            moved = True
        if cmd.get("step"):
            self._do_step(cmd["step"])
            moved = True
        self._clock_restart()
        return moved

    def _set_paused(self, paused: bool):
        if paused == self._paused:
            return
        self._paused = paused
        if not paused and self._eof:                 # play pressed at the end: start over from the other side
            self._goto(self.duration if self._reverse else 0.0)
        self._clock_restart()
        self.paused_changed.emit(paused)

    def _reached_end(self):
        if self._loop_all:
            self._goto(self.duration if self._reverse else 0.0)
            return
        self._eof = True
        self._set_paused(True)

    def _clock_restart(self):
        self._clock.start(self._pos, self._speed, -1 if self._reverse else 1)

    def _sleep(self, seconds: float):
        end = time.monotonic() + seconds
        while self._running and not self._dirty:
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.02, left))

    def _to_image(self, payload):
        arr = self.source.to_rgb(payload, self.max_width)
        h, w, _ = arr.shape
        return QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()

    def _display(self, item, force: bool = False, push: bool = False):
        t, payload = item
        self._pos = t
        if self.pending and not force:
            return                                   # the window is still drawing the last one: skip the conversion
        img = self._to_image(payload)
        if push:
            self.history.push(t, img)
        self._emit(img, t)

    def _emit(self, img, t: float):
        self._last_shown = time.monotonic()
        self.pending = True
        self.frame.emit(img, t)
