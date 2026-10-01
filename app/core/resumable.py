"""Download that survives a dropped connection.

The NVR cannot be asked "continue from byte N", so a resume re-requests the recording and skips the
frames that are already on disk. The skipped frames are compared (hash of the last one) with what was
saved, so a resume that does not line up is detected instead of silently gluing two different streams.

Progress (frame count, byte size, hash of the last frame) is saved next to the partial file, so it also
survives closing the program: starting the same download again continues where it stopped.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from app.core import dvrip

RETRY_DELAYS = (3, 5, 10, 20, 30, 60)      # seconds between reconnect attempts, then stays at the last one
COMPLETE_RATIO = 0.9                         # a stream that ends normally below this share is treated as cut short
SAVE_EVERY_BYTES = 8 * 1024 * 1024


class Cancelled(Exception):
    """The user stopped the download; the partial file and its state are kept for a later resume."""


class DownloadFailed(Exception):
    """Gave up (wrong password, a recording the NVR refuses, a resume that cannot be matched...)."""


@dataclass
class Result:
    codec: str | None
    frames: int
    size: int              # video bytes on disk
    reconnects: int
    embedded: int          # stats of the last connection (diagnostics)
    dropped: int
    complete: bool         # False: the NVR ended the stream early twice; what we got is kept


def _h(b: bytes) -> str:
    return hashlib.blake2b(b, digest_size=8).hexdigest()


class ResumableDownload:
    def __init__(self, open_stream: Callable[[], tuple[object, Iterable[bytes]]], raw_path: Path,
                 identity: dict, expected: int = 0,
                 progress: Callable[[int, int], None] = lambda done, total: None,
                 status: Callable[[str], None] = lambda text: None,
                 cancelled: Callable[[], bool] = lambda: False,
                 sleep: Callable[[float], None] = time.sleep,
                 on_chunk: Callable[[object], None] = lambda client: None):
        """open_stream() -> (client, iterator of payload chunks); client.close() is called when done.
        identity: what makes two downloads "the same recording" (channel, begin, end, stream type...)."""
        self.open_stream = open_stream
        self.raw = Path(raw_path)
        self.state_path = Path(str(raw_path) + ".json")
        self.identity = identity
        self.expected = expected
        self.progress, self.status, self.cancelled, self._sleep, self.on_chunk = progress, status, cancelled, sleep, on_chunk
        self.frames = 0
        self.size = 0
        self.last_hash = ""
        self.codec: str | None = None
        self.reconnects = 0
        self.embedded = self.dropped = 0
        self._unsaved = 0
        self._fh = None

    # ---- saved state ---------------------------------------------------
    def _load(self) -> bool:
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
            if st.get("identity") != self.identity or not self.raw.exists() or self.raw.stat().st_size < st["size"]:
                return False
            self.frames, self.size, self.last_hash, self.codec = st["frames"], st["size"], st["last_hash"], st.get("codec")
            return True
        except (OSError, ValueError, KeyError):
            return False

    def _save(self):
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps({"identity": self.identity, "frames": self.frames, "size": self.size,
                                   "last_hash": self.last_hash, "codec": self.codec}), encoding="utf-8")
        os.replace(tmp, self.state_path)
        self._unsaved = 0

    def _fresh(self):
        self.frames = self.size = 0
        self.last_hash, self.codec = "", None
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self.raw.write_bytes(b"")
        self._save()

    def discard(self):
        for p in (self.raw, self.state_path):
            p.unlink(missing_ok=True)

    # ---- one connection ------------------------------------------------
    def _attempt(self) -> tuple[bool, int]:
        """Returns (ended normally, new frames written during this connection)."""
        client, stream = self.open_stream()
        parser = dvrip.XMFrameParser()
        seen, new = 0, 0
        try:
            for chunk in stream:
                if self.cancelled():
                    raise Cancelled()
                if not chunk:
                    continue
                self.on_chunk(client)
                for v in parser.feed(chunk):
                    if seen < self.frames:                      # already on disk: skip, but check it is the same stream
                        seen += 1
                        if seen == self.frames and _h(v) != self.last_hash:
                            raise _Mismatch()
                        continue
                    if self.codec is None:
                        self.codec = dvrip.sniff_codec(v)
                    self._fh.write(v)
                    self.frames += 1
                    seen += 1
                    new += 1
                    self.size += len(v)
                    self._unsaved += len(v)
                    self.last_hash = _h(v)
                self.progress(self.size, self.expected)
                if self._unsaved >= SAVE_EVERY_BYTES:
                    self._save()
            return True, new
        finally:
            self.embedded, self.dropped = parser.embedded, parser.dropped
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _wait(self, seconds: int, why: str, attempt: int):
        left = float(seconds)
        while left > 0:
            if self.cancelled():
                raise Cancelled()
            self.status(f"{why} ממתין לחידוש החיבור (ניסיון {attempt}, בעוד {int(left) + 1} שנ')... ההורדה שמורה ותמשיך מאותה נקודה")
            step = min(1.0, left)
            self._sleep(step)
            left -= step

    # ---- main loop -----------------------------------------------------
    def run(self) -> Result:
        resumed = self._load()
        if resumed:
            self.status(f"ממשיך הורדה קודמת ({self.size // (1024 * 1024)}MB שמורים)...")
            with open(self.raw, "r+b") as f:                    # drop bytes written after the last saved state
                f.truncate(self.size)
        else:
            self._fresh()
        self._fh = open(self.raw, "ab")
        failures = mismatches = attempt = 0
        complete = True
        try:
            while True:
                if self.cancelled():
                    raise Cancelled()
                try:
                    ended, new = self._attempt()
                    self._save()
                    if ended:
                        short = self.expected > 0 and self.size < self.expected * COMPLETE_RATIO
                        if not short or (attempt > 0 and new == 0):
                            complete = not short
                            break
                        why, failures = "ה-NVR סגר את ההורדה לפני הסוף.", failures + 1
                        if failures > 3:                          # it keeps ending early: accept what we have
                            complete = False
                            break
                except _Mismatch:
                    mismatches += 1
                    self._save()
                    if mismatches > 2:
                        raise DownloadFailed("ההמשך לא תאם למה שכבר ירד (ה-NVR שלח זרם שונה). הקובץ החלקי נשמר")
                    self.status("ההמשך לא תאם למה שירד, מתחיל מחדש...")
                    self._fresh()
                    self._fh = open(self.raw, "ab")
                    why = ""
                except Cancelled:
                    self._save()
                    raise
                except dvrip.DVRIPAuthError as exc:
                    self._save()
                    raise DownloadFailed(str(exc)) from exc
                except (OSError, socket.timeout, dvrip.DVRIPError) as exc:
                    self._save()
                    before = self.frames
                    why = f"החיבור נפסק ({exc})."
                    if isinstance(exc, dvrip.DVRIPError) and exc.code is not None:
                        failures += 1                              # the NVR answered with an error code: don't loop forever
                        if failures > 5:
                            raise DownloadFailed(str(exc)) from exc
                    elif before > 0:
                        failures = 0
                attempt += 1
                self.reconnects += 1
                self._wait(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)], why, attempt)
        finally:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
        self.state_path.unlink(missing_ok=True)
        return Result(self.codec, self.frames, self.size, self.reconnects, self.embedded, self.dropped, complete)


class _Mismatch(Exception):
    pass
