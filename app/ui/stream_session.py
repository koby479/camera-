"""The background download that feeds the streaming player, and saving the result as an MP4."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from app.core import dvrip, frameindex, mp4, recfmt, resumable, streamcache
from app.core.config import CameraConfig
from app.core.streamsource import RawStreamSource


class StreamSession(QThread):
    progress = pyqtSignal(int, int)       # bytes received, expected bytes (0 = unknown)
    note = pyqtSignal(str)                # e.g. waiting for the NVR to come back
    ended = pyqtSignal(bool, str)         # (the whole recording is here, message)

    def __init__(self, cfg: CameraConfig, channel: int, item: dict, stream_type: int,
                 paths: streamcache.CachePaths, state: streamcache.StreamState):
        super().__init__()
        self.cfg, self.channel, self.item, self.stream_type = cfg, channel, item, stream_type
        self.paths, self.state = paths, state
        self._cancel = False
        self._dl: resumable.ResumableDownload | None = None
        self._saved_fps = None

    def request_stop(self):
        self._cancel = True

    def _open_stream(self):
        c = dvrip.DVRIPClient(self.cfg.host, self.cfg.port, self.cfg.username, self.cfg.password, timeout=15)
        try:
            c.connect()
            c.login()
            c.start_download(self.channel, self.item, stream_type=self.stream_type)
            return c, c.read_download_payloads()
        except BaseException:
            c.close()
            raise

    def _on_progress(self, done: int, total: int):
        dl = self._dl
        if dl is not None:
            if self.state.codec is None and dl.codec:
                self.state.codec = dl.codec
            if self.state.fps is None and dl.fps_hint:         # the first key frame told us the frame rate
                self.state.fps = float(dl.fps_hint)
                self._save_meta(False)
        self.progress.emit(done, total)

    def _save_meta(self, complete: bool, **extra):
        try:
            streamcache.save_meta(self.paths, complete=complete, fps=self.state.fps, codec=self.state.codec,
                                  declared=self.state.declared, **extra)
        except OSError:
            pass

    def run(self):
        try:
            self.paths.raw.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.state.failed = f"לא ניתן לכתוב את קובץ הביניים: {exc}"
            self.ended.emit(False, self.state.failed)
            return
        dl = resumable.ResumableDownload(
            self._open_stream, self.paths.raw,
            identity={"channel": self.channel, "begin": self.item.get("begin"), "end": self.item.get("end"),
                      "stream": self.stream_type},
            expected=int(self.item.get("size") or 0),
            progress=self._on_progress, status=self.note.emit, cancelled=lambda: self._cancel,
            on_chunk=lambda client: client.keepalive_if_due(),
            index=frameindex.IndexWriter(self.paths.idx))
        self._dl = dl
        try:
            res = dl.run()
        except resumable.Cancelled:
            self._save_meta(False)                       # what arrived is kept: opening it again continues
            return
        except resumable.DownloadFailed as exc:
            print(f"[stream] {exc}", file=sys.stderr)
            self.state.failed = f"שגיאה - {exc}"
            self.ended.emit(False, self.state.failed)
            return
        except OSError as exc:                           # disk full / cannot write
            print(f"[stream] {exc!r}", file=sys.stderr)
            self.state.failed = f"שגיאה בכתיבת הקובץ: {exc}"
            self.ended.emit(False, self.state.failed)
            return
        if res.size == 0:
            dl.discard()
            self.state.failed = "ה-NVR לא שלח נתונים"
            self.ended.emit(False, self.state.failed)
            return
        hint = dl.fps_hint
        fps = streamcache.pick_fps(hint, res.frames, self.state.declared, True)
        if hint and abs(fps / hint - 1.0) > 0.01:
            print(f"[stream] frame rate in the stream says {hint}, the recording has {fps:.2f} - timeline corrected",
                  file=sys.stderr)
        print(f"[stream] done: {res.frames} frames, {res.size} bytes, reconnects {res.reconnects}, "
              f"complete={res.complete}", file=sys.stderr)
        self.state.fps, self.state.codec = fps, res.codec          # fps and codec first: the player reads them
        self.state.complete = True
        self._save_meta(True, frames=res.frames, size=res.size, short=not res.complete)
        note = "" if res.complete else "ה-NVR סיים את ההורדה לפני הסוף, ייתכן שחלק מההקלטה חסר"
        self.ended.emit(True, note)


class Mp4Saver(QThread):
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, raw: Path, target: Path, codec: str | None, seconds: float | None):
        super().__init__()
        self.raw, self.target, self.codec, self.seconds = raw, target, codec, seconds

    def run(self):
        try:
            mp4.raw_to_mp4(str(self.raw), str(self.target), self.codec, self.seconds)
        except Exception as exc:                          # noqa: BLE001 - shown in the window
            print(f"[stream] save as MP4: {exc!r}", file=sys.stderr)
            self.target.unlink(missing_ok=True)
            self.failed.emit(str(exc))
            return
        self.done.emit(str(self.target))


@dataclass
class StreamPlayback:
    """Everything the player window needs to play a recording that is being downloaded."""
    factory: object
    state: streamcache.StreamState
    paths: streamcache.CachePaths
    session: StreamSession | None          # None: the whole recording was already in the cache
    title: str
    start_time: datetime | None
    save_path: Path
    seconds: float | None


def prepare_stream(cfg: CameraConfig, channel: int, rec: dict, stream_type: int) -> StreamPlayback:
    paths = streamcache.cache_paths(cfg.host, channel, str(rec.get("begin")), stream_type)
    secs = recfmt.duration_seconds(rec.get("begin"), rec.get("end"))
    state = streamcache.StreamState(declared=float(secs or 0))
    streamcache.cleanup(keep_stems={paths.raw.name.split(".")[0]})
    meta = streamcache.load_meta(paths)
    session = None
    if meta:
        state.fps, state.codec = meta.get("fps"), meta.get("codec")
    if meta and meta.get("complete") and paths.raw.exists() and paths.idx.exists():
        state.complete = True                              # watched (or downloaded) before: nothing to fetch
    else:
        session = StreamSession(cfg, channel, rec, stream_type, paths, state)
    try:
        start = datetime.strptime(str(rec.get("begin")), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        start = None
    folder = Path.home() / "Videos" / "CameraRecordings"
    save = folder / recfmt.safe_filename(channel, rec, "mp4")
    n = 2
    while save.exists():
        save = folder / recfmt.safe_filename(channel, rec, "mp4").replace(".mp4", f"_{n}.mp4")
        n += 1
    return StreamPlayback(
        factory=lambda _path: RawStreamSource(paths.raw, paths.idx, state),
        state=state, paths=paths, session=session,
        title=f"{cfg.name}  ·  ערוץ {channel + 1}  ·  {rec.get('begin')}",
        start_time=start, save_path=save, seconds=secs)
