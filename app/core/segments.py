"""A recording that is fetched from the NVR in pieces, so the player can jump to any time without downloading
everything before it.

The NVR can be asked to send a recorded file starting at a later time. The controller keeps one piece per such
request on disk (raw video + frame index), runs ONE download at a time (these NVRs serve one playback per camera),
and tells the player which stretches of the recording are playable. A jump to a stretch that is not there stops
the running download (what it fetched stays) and starts a new piece at the target. Whether the NVR really honours
the start time is checked from the real time stamps in the key frames; if it does not, the controller falls back to
one sequential download and a jump simply waits until the download reaches it. Qt-free, unit-tested with a fake NVR."""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from app.core import frameindex, recfmt, resumable, streamcache
from app.core.streamsource import RawStreamSource
from app.core.timeline import DEFAULT_FPS
from app.core.videosource import WAITING, VideoSource

LEAD = 6.0              # start a piece this many seconds before the target, so it begins with a key frame at or before it
COVER_SLACK = 3.0       # a piece that is still downloading counts as covering this far beyond its last frame
STARTUP_SLACK = 12.0    # a piece that has not delivered a frame yet counts as covering this far from its start
VERIFY_WINDOW = 25.0    # the first key frame may differ this much from the requested start (key-frame spacing)
MODES = ("ByName", "ByTime")


def _log(text: str):
    print(f"[segments] {text}", file=sys.stderr)


@dataclass(eq=False)
class Part:
    offset: float                           # requested start, seconds from the start of the recording
    paths: streamcache.CachePaths
    state: streamcache.StreamState
    source: RawStreamSource
    mode: str = "ByName"
    verified: bool = True                   # the NVR really started where we asked (checked for offset > 0)
    cancel: bool = False
    seek_failed: bool = False
    thread: threading.Thread | None = None
    bytes: int = 0

    @property
    def active(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class StreamController:
    def __init__(self, host: str, channel: int, rec: dict, stream_type: int, open_stream, cache_folder=None):
        """open_stream(item, mode, stream_type) -> (client, iterator of payload chunks); the client needs
        keepalive_if_due() and close()."""
        self.host, self.channel, self.rec, self.stream_type = host, channel, rec, stream_type
        self._open_stream = open_stream
        self.folder = cache_folder
        self.origin = frameindex.wall_seconds(str(rec.get("begin")))
        self.declared = float(recfmt.duration_seconds(rec.get("begin"), rec.get("end")) or 0)
        self.expected_size = int(rec.get("size") or 0)
        self.parts: list[Part] = []
        self.seek_supported: bool | None = None
        self.status = ""
        self.rate: float | None = None              # download speed, bytes per second
        self._mode_i = 0
        self._lock = threading.RLock()
        self._closed = False
        self._last_request: tuple[float | None, float] = (None, 0.0)
        self._last_prog: tuple[float, int] | None = None

    # ---- the pieces -----------------------------------------------------------
    def _new_part(self, offset: float, mode: str) -> Part:
        paths = streamcache.cache_paths(self.host, self.channel, str(self.rec.get("begin")), self.stream_type,
                                        folder=self.folder, segment=int(offset))
        state = streamcache.StreamState(declared=self.declared)
        source = RawStreamSource(paths.raw, paths.idx, state, origin=self.origin, start_guess=float(offset))
        return Part(offset, paths, state, source, mode=mode, verified=offset <= 0)

    def start(self):
        """Open the recording from its beginning (reusing what an earlier visit left in the cache)."""
        with self._lock:
            main = self._new_part(0.0, MODES[0])
            meta = streamcache.load_meta(main.paths)
            if meta:
                main.state.fps, main.state.codec = meta.get("fps"), meta.get("codec")
            if meta and meta.get("complete") and main.paths.raw.exists() and main.paths.idx.exists():
                main.state.complete = True
            self.parts.append(main)
            if not main.state.complete:
                self._launch(main)

    @property
    def main(self) -> Part:
        return self.parts[0]

    def whole_ready(self) -> bool:
        return bool(self.parts) and self.parts[0].offset == 0 and self.parts[0].state.complete

    def is_active(self, part: Part) -> bool:
        return part.active

    def _launch(self, part: Part):
        with self._lock:
            if self._closed:
                return
            previous = []
            for p in self.parts:
                if p is not part and p.active:
                    p.cancel = True                 # one download at a time: what it has fetched stays
                    previous.append(p.thread)
            part.cancel = False
            part.thread = threading.Thread(target=self._run, args=(part, previous), daemon=True,
                                           name=f"segment-{int(part.offset)}")
            part.thread.start()

    # ---- what the player asks ------------------------------------------------------
    def _span(self, p: Part):
        p.source.refresh()
        if p.source.frame_count == 0:
            return None
        return p.source.start_time, p.source.buffered_until

    def covers(self, t: float) -> bool:
        return self.part_for(t) is not None

    def part_for(self, t: float) -> Part | None:
        best = None
        for p in list(self.parts):
            span = self._span(p)
            if span is None:
                if p.active and p.offset - LEAD <= t <= p.offset + STARTUP_SLACK:
                    start, ok = p.offset, True
                else:
                    continue
            else:
                start, end = span
                ok = start - 0.5 <= t <= end + (COVER_SLACK if p.active else 0.0)
            if ok and (best is None or start > best[0]):
                best = (start, p)
        return best[1] if best else None

    def ranges(self) -> list[tuple[float, float]]:
        spans = sorted(s for s in (self._span(p) for p in list(self.parts)) if s is not None)
        merged: list[list[float]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1] + 0.5:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        return [(s, e) for s, e in merged]

    def duration(self) -> float:
        ends = [e for _s, e in self.ranges()]
        return max([self.declared] + ends)

    def fps(self) -> float:
        for p in self.parts:
            if p.source.frame_count:
                return p.source.fps
        return DEFAULT_FPS

    def request(self, t: float):
        """The player wants to show time t: make sure a download that reaches it is running."""
        with self._lock:
            if self._closed or self.covers(t):
                return
            now = time.monotonic()
            last_t, last_when = self._last_request
            if last_t is not None and abs(t - last_t) < 4.0 and now - last_when < 10.0:
                return                              # the same jump again: the piece for it is already on its way
            self._last_request = (t, now)
            if self.seek_supported is False:
                self._set_status("ה-NVR לא יודע להתחיל מאמצע ההקלטה: מחכה שההורדה תגיע לשם")
                if not self.main.active and not self.main.state.complete:
                    self._launch(self.main)
                return
            top = self.declared - 5.0 if self.declared > 10 else float("inf")
            part = self._new_part(max(0.0, min(t - LEAD, top)), MODES[self._mode_i])
            self.parts.append(part)
            _log(f"jump to {t:.0f}s: new piece from {part.offset:.0f}s ({part.mode})")
            self._set_status(f"מבקש מה-NVR להתחיל מ-{recfmt.clock_text(self.origin + part.offset)}...")
            self._launch(part)

    # ---- download thread ------------------------------------------------------------
    def _set_status(self, text: str):
        self.status = text

    def _item_for(self, part: Part) -> dict:
        item = dict(self.rec)
        if part.offset > 0 and self.origin is not None:
            item["begin"] = datetime.utcfromtimestamp(self.origin + part.offset).strftime("%Y-%m-%d %H:%M:%S")
        return item

    def _run(self, part: Part, previous: list):
        for th in previous:
            th.join(8.0)                            # the NVR serves one playback at a time
        if part.cancel or self._closed:
            return
        try:
            part.paths.raw.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            part.state.failed = f"לא ניתן לכתוב את קובץ הביניים: {exc}"
            self._set_status(part.state.failed)
            return
        dl = resumable.ResumableDownload(
            lambda: self._open_stream(self._item_for(part), part.mode, self.stream_type), part.paths.raw,
            identity={"channel": self.channel, "begin": self.rec.get("begin"), "end": self.rec.get("end"),
                      "stream": self.stream_type, "offset": int(part.offset)},
            expected=self.expected_size if part.offset <= 0 else 0,
            progress=lambda done, _total: self._progress(part, dl, done),
            status=self._set_status, cancelled=lambda: part.cancel or self._closed,
            on_chunk=lambda client: client.keepalive_if_due(),
            index=frameindex.IndexWriter(part.paths.idx))
        try:
            res = dl.run()
        except resumable.Cancelled:
            self._after_cancel(part)
            return
        except resumable.DownloadFailed as exc:
            part.state.failed = f"שגיאה - {exc}"
            self._set_status(part.state.failed)
            _log(f"piece {int(part.offset)}s failed: {exc}")
            return
        except OSError as exc:
            part.state.failed = f"שגיאה בכתיבת הקובץ: {exc}"
            self._set_status(part.state.failed)
            return
        if res.size == 0:
            if part.offset > 0:
                part.seek_failed = True
                self._after_cancel(part)
            else:
                part.state.failed = "ה-NVR לא שלח נתונים"
                self._set_status(part.state.failed)
            return
        part.state.codec = res.codec or part.state.codec
        part.state.complete = True
        self._set_status("")
        _log(f"piece {int(part.offset)}s complete: {res.frames} frames, {res.size} bytes, reconnects {res.reconnects}")
        if part.offset <= 0:
            try:
                streamcache.save_meta(part.paths, complete=True, fps=part.state.fps, codec=part.state.codec,
                                      declared=self.declared, frames=res.frames, size=res.size, short=not res.complete)
            except OSError:
                pass

    def _progress(self, part: Part, dl, done: int):
        part.bytes = done
        if dl.codec and not part.state.codec:
            part.state.codec = dl.codec
        if dl.fps_hint and not part.state.fps:
            part.state.fps = float(dl.fps_hint)
        now = time.monotonic()
        if self._last_prog is None or part.offset != getattr(self, "_prog_offset", None):
            self._last_prog, self._prog_offset = (now, done), part.offset
        elif now - self._last_prog[0] >= 1.0:
            inst = (done - self._last_prog[1]) / (now - self._last_prog[0])
            self.rate = inst if self.rate is None else 0.7 * self.rate + 0.3 * inst
            self._last_prog = (now, done)
        if not part.verified and dl.first_stamp is not None and self.origin is not None:
            wanted = self.origin + part.offset
            if abs(dl.first_stamp - wanted) <= VERIFY_WINDOW:
                part.verified = True
                self.seek_supported = True
                self._set_status("")
                _log(f"the NVR started at the requested time ({part.mode}): first key frame {dl.first_stamp - wanted:+.0f}s")
            else:
                part.seek_failed = True
                part.cancel = True
                _log(f"the NVR ignored the start time ({part.mode}): first key frame {dl.first_stamp - wanted:+.0f}s from it")

    def _after_cancel(self, part: Part):
        if part.seek_failed:
            self._seek_failed(part)

    def _seek_failed(self, part: Part):
        with self._lock:
            self._discard(part)
            if self._closed:
                return
            if self.seek_supported is not True and self._mode_i + 1 < len(MODES):
                self._mode_i += 1
                retry = self._new_part(part.offset, MODES[self._mode_i])
                self.parts.append(retry)
                _log(f"trying the other way of asking ({retry.mode})")
                self._launch(retry)
            else:
                self.seek_supported = False
                _log("this NVR does not start from the middle: sequential download only")
                self._set_status("ה-NVR לא יודע להתחיל מאמצע ההקלטה: מחכה שההורדה תגיע לשם")
                if not self.main.state.complete:
                    self._launch(self.main)

    def _discard(self, part: Part):
        part.source.close()
        if part in self.parts:
            self.parts.remove(part)
        for p in (part.paths.raw, part.paths.idx, part.paths.raw.with_name(part.paths.raw.name + ".json")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ---- for the window --------------------------------------------------------------
    def snapshot(self) -> dict:
        parts = list(self.parts)
        active = next((p for p in parts if p.active), None)
        failed = next((p.state.failed for p in parts if p.state.failed and not p.state.complete and not p.active), "")
        return {"active_offset": active.offset if active else None, "bytes": active.bytes if active else 0,
                "rate": self.rate, "status": self.status, "failed": failed, "seek_supported": self.seek_supported,
                "whole_ready": self.whole_ready(), "origin": self.origin}

    def close(self):
        with self._lock:
            self._closed = True
            threads = []
            for p in self.parts:
                p.cancel = True
                if p.thread is not None and p.thread.is_alive():
                    threads.append(p.thread)
        for th in threads:
            th.join(2.0)
        for p in self.parts:
            p.source.close()


class SegmentedSource:
    """What the player engine sees: one source for the whole recording, made of the pieces that exist."""

    def __init__(self, controller: StreamController):
        self.ctl = controller
        self.width = self.height = 0

    to_rgb = staticmethod(VideoSource.to_rgb)

    @property
    def fps(self) -> float:
        return self.ctl.fps()

    @property
    def duration(self) -> float:
        return self.ctl.duration()

    @property
    def buffered_until(self) -> float:
        return self.duration                        # how far the player may go is decided by covers()

    @property
    def complete(self) -> bool:
        return self.ctl.whole_ready()

    def covers(self, t: float) -> bool:
        return self.ctl.covers(t)

    def ranges(self):
        return self.ctl.ranges()

    def request(self, t: float):
        self.ctl.request(t)

    def frames_from(self, t: float):
        cur = max(0.0, t)
        while True:
            part = self.ctl.part_for(cur)
            if part is None:
                self.ctl.request(cur)
                yield WAITING
                continue
            last = None
            ran_dry = True
            for item in part.source.frames_from(cur):
                if item is WAITING:
                    if self.ctl.is_active(part):
                        yield WAITING
                        continue
                    ran_dry = False                 # nothing more will come into this piece
                    break
                last = item[0]
                if not self.width:
                    self.width, self.height = part.source.width, part.source.height
                yield item
            if ran_dry and part.state.complete:
                return                              # the recording ends here
            cur = (last if last is not None else cur) + 0.5 / max(1.0, self.fps)
            if last is None and not self.ctl.is_active(part):
                self.ctl.request(cur)
                yield WAITING

    def close(self):
        self.ctl.close()
