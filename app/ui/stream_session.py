"""Opening a recording in the streaming player (the controller fetches it from the NVR in pieces), and saving the result as an MP4."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from app.core import dvrip, mp4, recfmt, streamcache
from app.core.config import CameraConfig
from app.core.segments import SegmentedSource, StreamController


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
    """Everything the player window needs to play a recording straight from the NVR."""
    controller: StreamController
    factory: object
    paths: streamcache.CachePaths          # of the piece that starts at the beginning of the recording
    title: str
    start_time: datetime | None
    save_path: Path
    seconds: float | None


def prepare_stream(cfg: CameraConfig, channel: int, rec: dict, stream_type: int) -> StreamPlayback:
    def open_stream(item: dict, mode: str, stream_type: int):
        c = dvrip.DVRIPClient(cfg.host, cfg.port, cfg.username, cfg.password, timeout=15)
        try:
            c.connect()
            c.login()
            c.start_download(channel, item, stream_type=stream_type, mode=mode)
            return c, c.read_download_payloads()
        except BaseException:
            c.close()
            raise

    paths = streamcache.cache_paths(cfg.host, channel, str(rec.get("begin")), stream_type)
    streamcache.cleanup(keep_stems={paths.raw.name.split(".")[0]})
    controller = StreamController(cfg.host, channel, rec, stream_type, open_stream)
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
        controller=controller, factory=lambda _path: SegmentedSource(controller), paths=paths,
        title=f"{cfg.name}  ·  ערוץ {channel + 1}  ·  {rec.get('begin')}",
        start_time=start, save_path=save, seconds=recfmt.duration_seconds(rec.get("begin"), rec.get("end")))
