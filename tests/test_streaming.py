"""Playing a recording while it downloads: the frame index, the cache, the growing-file source and how the
player engine behaves when the data it wants has not arrived yet."""
import itertools
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from app.core import dvrip, frameindex, streamcache
from app.core.videosource import WAITING

_undo = None


def setUpModule():
    global _undo
    try:
        import PyQt6.QtCore  # noqa: F401
    except ImportError:
        sys.path.insert(0, os.path.dirname(__file__))
        import qtstub
        _undo = qtstub.install()


def tearDownModule():
    if _undo:
        _undo()


class KeyFrameTests(unittest.TestCase):
    def test_h264(self):
        sc = b"\x00\x00\x00\x01"
        self.assertTrue(frameindex.is_key_frame(sc + b"\x67\x42" + sc + b"\x68\xce" + sc + b"\x65\x88", "h264"))
        self.assertTrue(frameindex.is_key_frame(sc + b"\x65\x88\x80", "h264"))
        self.assertFalse(frameindex.is_key_frame(sc + b"\x41\x9a\x20", "h264"))        # a P slice

    def test_hevc(self):
        sc = b"\x00\x00\x00\x01"
        self.assertTrue(frameindex.is_key_frame(sc + b"\x40\x01" + sc + b"\x26\x01", "hevc"))   # VPS + IDR_W_RADL
        self.assertTrue(frameindex.is_key_frame(sc + b"\x26\x01\xaf", "hevc"))
        self.assertFalse(frameindex.is_key_frame(sc + b"\x02\x01\xd0", "hevc"))                 # TRAIL_R

    def test_unknown_codec_is_never_a_key_frame(self):
        self.assertFalse(frameindex.is_key_frame(b"\x00\x00\x01\x65", None))


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "a.idx"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_round_trip_and_partial_record_is_ignored(self):
        w = frameindex.IndexWriter(self.path)
        w.add(0, 100, True)
        w.add(100, 40, False)
        w.flush()
        w.add(140, 30, True, 12345)
        w.flush()
        self.assertEqual(frameindex.read_records(self.path, 2, 3), [(140, 30, True, 12345)])    # the time stamp is kept
        frameindex.IndexWriter(self.path).truncate(2)
        with open(self.path, "ab") as f:
            f.write(b"\x01\x02")                                   # a record still being written
        self.assertEqual(frameindex.count_records(self.path), 2)
        self.assertEqual(frameindex.read_records(self.path, 0, 3), [(0, 100, True, 0), (100, 40, False, 0)])
        self.assertEqual(frameindex.read_records(self.path, 1, 2), [(100, 40, False, 0)])

    def test_truncate_and_reset(self):
        w = frameindex.IndexWriter(self.path)
        for i in range(5):
            w.add(i * 10, 10, i == 0)
        w.close()
        self.assertTrue(w.truncate(3))
        self.assertEqual(frameindex.count_records(self.path), 3)
        self.assertFalse(w.truncate(9))                            # fewer records than the saved state says
        w.reset()
        self.assertEqual(frameindex.count_records(self.path), 0)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_paths_are_filename_safe_and_stable(self):
        p = streamcache.cache_paths("83.229.22.45", 2, "2026-10-01 08:00:01", 0, self.dir)
        self.assertEqual(p.raw.name, "83-229-22-45-ch3-2026-10-01-08-00-01-s0.raw")
        self.assertEqual(p, streamcache.cache_paths("83.229.22.45", 2, "2026-10-01 08:00:01", 0, self.dir))

    def test_meta_round_trip(self):
        p = streamcache.cache_paths("h", 0, "b", 0, self.dir)
        self.assertIsNone(streamcache.load_meta(p))
        streamcache.save_meta(p, complete=True, fps=25.0)
        self.assertEqual(streamcache.load_meta(p), {"complete": True, "fps": 25.0})

    def test_cleanup_removes_oldest_first_and_spares_kept(self):
        for i, name in enumerate(["old", "mid", "new"]):
            (self.dir / f"{name}.raw").write_bytes(b"x" * 100)
            (self.dir / f"{name}.idx").write_bytes(b"x" * 10)
            os.utime(self.dir / f"{name}.raw", (1000 + i, 1000 + i))
            os.utime(self.dir / f"{name}.idx", (1000 + i, 1000 + i))
        streamcache.cleanup(keep_stems={"old"}, max_bytes=250, folder=self.dir)
        left = sorted(p.name.split(".")[0] for p in self.dir.iterdir())
        self.assertEqual(sorted(set(left)), ["new", "old"])        # "mid" went, "old" was protected

    def test_pick_fps(self):
        pick = streamcache.pick_fps
        self.assertEqual(pick(25, 0, 3600, False), 25.0)
        self.assertEqual(pick(None, 0, 3600, False), 25.0)
        self.assertEqual(pick(25, 90000, 3600, True), 25.0)         # agrees: the timeline stays as it was
        self.assertAlmostEqual(pick(25, 45000, 3600, True), 12.5)   # the header said 25 but there are 12.5 per second
        self.assertEqual(pick(99, 0, 0, False), 25.0)               # nonsense hint


class FakeFrame:
    width, height = 8, 8

    def __init__(self, n):
        self.n = n


class FakeDecoder:
    """Decodes one frame per call, carrying the number that was stored in the first byte."""
    def decode_raw(self, data):
        yield FakeFrame(data[0])


def build_stream(directory, frames, fps_key=10):
    raw, idx = directory / "s.raw", directory / "s.idx"
    w = frameindex.IndexWriter(idx)
    offset = 0
    with open(raw, "ab") as f:
        for i in frames:
            data = bytes([i]) + b"payload"
            f.write(data)
            w.add(offset, len(data), i % fps_key == 0)
            offset += len(data)
    w.close()
    return raw, idx


class StreamSourceTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._old = dvrip.H264Decoder
        dvrip.H264Decoder = FakeDecoder
        from app.core.streamsource import RawStreamSource
        self.Source = RawStreamSource
        self.state = streamcache.StreamState(declared=10.0, fps=10.0)

    def tearDown(self):
        dvrip.H264Decoder = self._old
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_starts_at_the_requested_time_after_decoding_from_the_key_frame(self):
        raw, idx = build_stream(self.dir, range(100))
        self.state.complete = True
        src = self.Source(raw, idx, self.state)
        got = [(round(t, 3), f.n) for t, f in itertools.islice(src.frames_from(3.5), 3)]
        self.assertEqual(got, [(3.5, 35), (3.6, 36), (3.7, 37)])
        times = [t for t, _f in src.frames_from(0)]
        self.assertEqual(len(times), 100)
        self.assertEqual(src.duration, 10.0)

    def test_waits_for_frames_that_have_not_arrived_then_continues(self):
        raw, idx = build_stream(self.dir, range(50))
        src = self.Source(raw, idx, self.state)                    # download still running
        self.assertAlmostEqual(src.buffered_until, (50 - 1 - 8) / 10)
        self.assertEqual(src.duration, 10.0)                       # the length the NVR announced
        gen = src.frames_from(4.0)
        seen = []
        for item in gen:
            if item is WAITING:
                break
            seen.append(item[1].n)
        self.assertEqual(seen[0], 40)
        self.assertEqual(seen[-1], 49)
        # the download goes on: 50 more frames, then it is finished
        with open(raw, "ab") as f, open(idx, "ab") as fi:
            offset = os.path.getsize(raw)
            for i in range(50, 100):
                data = bytes([i]) + b"payload"
                f.write(data)
                fi.write(frameindex.REC.pack(offset, len(data), 1 if i % 10 == 0 else 0, 0))
                offset += len(data)
        self.state.complete = True
        rest = [item[1].n for item in gen if item is not WAITING]
        self.assertEqual(rest, list(range(50, 100)))

    def test_nothing_to_play_yet_gives_waiting_and_a_failed_download_ends_the_stream(self):
        raw, idx = build_stream(self.dir, [])
        src = self.Source(raw, idx, self.state)
        self.assertIs(next(src.frames_from(0)), WAITING)
        self.state.failed = "bad password"
        self.assertEqual(list(src.frames_from(0)), [])

    def test_plays_even_when_no_key_frame_was_recognised(self):
        raw, idx = build_stream(self.dir, range(30), fps_key=10 ** 6)   # only frame 0 would count: use none at all
        frameindex.IndexWriter(idx).reset()
        w = frameindex.IndexWriter(idx)
        offset = 0
        for i in range(30):
            w.add(offset, len(bytes([i]) + b"payload"), False)
            offset += len(bytes([i]) + b"payload")
        w.close()
        self.state.complete = True
        src = self.Source(raw, idx, self.state)
        self.assertEqual([f.n for _t, f in src.frames_from(0)], list(range(30)))

    def test_download_that_starts_over_is_noticed(self):
        raw, idx = build_stream(self.dir, range(30))
        src = self.Source(raw, idx, self.state)
        self.assertEqual(len(src._recs), 30)
        frameindex.IndexWriter(idx).reset()
        src._refresh()
        self.assertEqual(len(src._recs), 0)


class FakeGrowing:
    """10 s at 10 fps, key frame every 2 s; only the first `avail` frames have 'arrived'."""
    fps, width, height, total = 10.0, 8, 8, 100

    def __init__(self, avail):
        self.avail, self.complete = avail, False

    @property
    def duration(self):
        return self.total / self.fps

    @property
    def buffered_until(self):
        return (self.total - 1) / self.fps if self.complete else max(0.0, (self.avail - 1) / self.fps)

    def frames_from(self, t):
        i = max(0, int(t * self.fps + 1e-9)) // 20 * 20
        while i < self.total:
            if i >= self.avail:
                yield WAITING
                continue
            ft = round(i / self.fps, 6)
            i += 1
            if ft + 0.05 < t:
                continue
            yield ft, i - 1

    @staticmethod
    def to_rgb(payload, max_width=None):
        return np.full((4, 4, 3), payload % 256, dtype="uint8")

    def close(self):
        pass


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class EngineWithDownloadTests(unittest.TestCase):
    def setUp(self):
        from app.core.playerengine import PlayerEngine
        self.src = FakeGrowing(avail=30)
        self.shown = []
        shown = self.shown

        class Rec(PlayerEngine):
            def _emit(self, img, t):
                shown.append(round(t, 3))
                self.pending = False

        self.eng = Rec("x", source_factory=lambda _p: self.src)
        self.eng.start()
        self.assertTrue(wait_for(lambda: self.shown))

    def tearDown(self):
        self.eng.stop()

    def test_play_reaches_the_end_of_what_arrived_then_waits_then_goes_on(self):
        self.eng.set_speed(16)
        self.assertTrue(wait_for(lambda: self.eng._buffering))              # at the end of the 3 s that are here
        self.assertLessEqual(self.shown[-1], 3.0)
        self.assertFalse(self.eng._paused)                                   # waiting is not the end of the file
        self.src.avail = 100
        self.src.complete = True
        self.assertTrue(wait_for(lambda: self.eng._paused and self.shown[-1] >= 9.8))   # now it runs to the real end
        self.assertFalse(self.eng._buffering)

    def test_jump_ahead_waits_for_the_download_then_happens_by_itself(self):
        self.eng.set_paused(True)
        self.assertTrue(wait_for(lambda: self.eng._paused))
        before = self.shown[-1]
        self.eng.seek(8.0)                                                  # only 3 s have arrived
        self.assertTrue(wait_for(lambda: self.eng._deferred == 8.0))
        time.sleep(0.3)
        self.assertEqual(self.shown[-1], before)                           # the picture did not move
        self.src.avail = 100                                                # the download got there
        self.assertTrue(wait_for(lambda: self.shown[-1] == 8.0))
        self.assertIsNone(self.eng._deferred)

    def test_a_new_jump_cancels_the_one_that_was_waiting(self):
        self.eng.set_paused(True)
        self.assertTrue(wait_for(lambda: self.eng._paused))
        self.eng.seek(8.0)
        self.assertTrue(wait_for(lambda: self.eng._deferred == 8.0))
        self.eng.seek(1.0)                                                  # inside what is here: happens now
        self.assertTrue(wait_for(lambda: self.shown[-1] == 1.0 and self.eng._deferred is None))
        self.src.avail = 100
        time.sleep(0.4)
        self.assertEqual(self.shown[-1], 1.0)                               # the old jump did not come back

    def test_playing_continues_while_a_jump_waits(self):
        self.eng.set_speed(1)
        self.eng.seek(8.0)
        self.assertTrue(wait_for(lambda: self.eng._deferred == 8.0))
        self.assertFalse(self.eng._paused)
        n = len(self.shown)
        self.assertTrue(wait_for(lambda: len(self.shown) > n + 3))         # still showing frames meanwhile

    def test_backwards_works_on_what_is_already_here(self):
        self.eng.set_paused(True)
        self.assertTrue(wait_for(lambda: self.eng._paused))
        self.eng.seek(2.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 2.0))
        self.eng.set_speed(16)
        self.eng.set_reverse(True)
        self.eng.set_paused(False)
        self.assertTrue(wait_for(lambda: self.eng._paused and self.shown[-1] <= 0.1))


if __name__ == "__main__":
    unittest.main()


class EndToEndTests(unittest.TestCase):
    """The whole path behind the play button with a fake NVR: prepare -> controller downloads -> player source."""

    def test_prepare_download_and_play(self):
        from app.core.config import CameraConfig
        from app.ui import stream_session
        from tests.test_xm_embedded import fc, fd, nal

        frames = [nal(200 + i, i % 250 + 1) for i in range(60)]
        wire = fc(frames[0]) + b"".join(fd(f) for f in frames[1:])          # the helper's key frame says: 25 fps

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def connect(self):
                pass

            def login(self):
                pass

            def start_download(self, channel, item, stream_type=0, mode="ByName"):
                pass

            def read_download_payloads(self):
                for i in range(0, len(wire), 50):
                    yield wire[i:i + 50]

            def keepalive_if_due(self):
                pass

            def close(self):
                pass

        tmp = Path(tempfile.mkdtemp())
        old_client, old_cache, old_dec = dvrip.DVRIPClient, streamcache.CACHE_DIR, dvrip.H264Decoder
        dvrip.DVRIPClient, streamcache.CACHE_DIR, dvrip.H264Decoder = FakeClient, tmp, FakeDecoder
        pb = again = None
        try:
            cfg = CameraConfig(name="nvr", host="1.2.3.4", port=34567, protocol="dvrip")
            rec = {"name": "f", "begin": "2026-10-01 08:00:00", "end": "2026-10-01 08:00:06", "size": len(wire)}
            pb = stream_session.prepare_stream(cfg, 0, rec, 0)
            self.assertEqual(pb.title.split("·")[1].strip(), "ערוץ 1")
            self.assertAlmostEqual(pb.controller.declared, 6.0)
            pb.controller.start()
            self.assertTrue(wait_for(pb.controller.whole_ready), pb.controller.status)
            src = pb.factory(None)
            times = [round(t, 3) for t, _f in src.frames_from(0)]
            self.assertEqual(times, [round(i / 25, 3) for i in range(60)])      # 25 fps, as the stream says
            self.assertEqual([round(t, 3) for t, _f in itertools.islice(src.frames_from(1.0), 2)], [1.0, 1.04])
            pb.controller.close()
            # the next time the same recording is opened nothing is downloaded
            again = stream_session.prepare_stream(cfg, 0, rec, 0)
            again.controller.start()
            self.assertTrue(again.controller.whole_ready())
            self.assertFalse(again.controller.main.active)
        finally:
            for item in (pb, again):
                if item is not None:
                    item.controller.close()
            dvrip.DVRIPClient, streamcache.CACHE_DIR, dvrip.H264Decoder = old_client, old_cache, old_dec
            shutil.rmtree(tmp, ignore_errors=True)


class FakePieces:
    """What the engine sees of a recording fetched in pieces (see SegmentedSource): only some stretches are here."""
    fps, width, height, total = 10.0, 8, 8, 100
    duration, buffered_until, complete = 10.0, 10.0, False

    def __init__(self):
        self.have = [(0.0, 3.0)]
        self.requested = []

    def covers(self, t):
        return any(s <= t <= e for s, e in self.have)

    def ranges(self):
        return list(self.have)

    def request(self, t):
        self.requested.append(t)

    def frames_from(self, t):
        i = max(0, int(t * self.fps + 1e-9))
        while i < self.total:
            ft = round(i / self.fps, 6)
            if not self.covers(ft):
                yield WAITING
                continue
            i += 1
            yield ft, i - 1

    to_rgb = staticmethod(FakeGrowing.to_rgb)

    def close(self):
        pass


class EngineWithPiecesTests(unittest.TestCase):
    def setUp(self):
        from app.core.playerengine import PlayerEngine
        self.src = FakePieces()
        self.shown = []
        shown = self.shown

        class Rec(PlayerEngine):
            def _emit(self, img, t):
                shown.append(round(t, 3))
                self.pending = False

        self.eng = Rec("x", source_factory=lambda _p: self.src)
        self.eng.start()
        self.assertTrue(wait_for(lambda: self.shown))
        self.eng.set_paused(True)
        self.assertTrue(wait_for(lambda: self.eng._paused))

    def tearDown(self):
        self.eng.stop()

    def test_a_jump_to_a_stretch_that_is_not_here_asks_for_it_and_happens_when_it_arrives(self):
        before = self.shown[-1]
        self.eng.seek(8.0)
        self.assertTrue(wait_for(lambda: self.eng._deferred == 8.0))
        self.assertIn(8.0, self.src.requested)                               # the piece for it was asked for
        self.assertEqual(self.shown[-1], before)                             # and nothing moved meanwhile
        self.src.have.append((7.0, 10.0))                                    # the NVR delivered it
        self.assertTrue(wait_for(lambda: self.shown[-1] == 8.0))
        self.assertIsNone(self.eng._deferred)

    def test_a_jump_inside_what_is_here_happens_at_once(self):
        self.eng.seek(2.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 2.0))
        self.assertEqual(self.src.requested, [])

    def test_playing_into_a_gap_waits_and_goes_on_when_the_next_piece_arrives(self):
        self.eng.set_speed(16)
        self.eng.seek(2.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 2.0))
        self.eng.set_paused(False)
        self.assertTrue(wait_for(lambda: self.eng._buffering))               # reached the end of the piece: waiting
        self.src.have.append((3.0, 10.0))
        self.assertTrue(wait_for(lambda: self.eng._paused and self.shown[-1] >= 9.8))


if __name__ == "__main__":
    unittest.main()
