"""Jumping to any time of a recording without downloading what is before it: the time line, and the controller
that fetches the recording in pieces, tested against a fake NVR that can be made to honour the start time or not."""
import itertools
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.core import dvrip, frameindex, segments, streamcache
from app.core.timeline import Timeline
from app.core.videosource import WAITING

FPS, KEY, SECONDS = 5, 10, 600                      # 10 minutes, 5 frames per second, a key frame every 2 s
BEGIN = "2026-10-01 17:00:00"
END = "2026-10-01 17:10:00"
ORIGIN = frameindex.wall_seconds(BEGIN)


def payload(n):
    if n % KEY == 0:
        return b"\x00\x00\x00\x01\x67\x42\x00\x00\x00\x01\x65" + n.to_bytes(4, "big")
    return b"\x00\x00\x00\x01\x41" + n.to_bytes(4, "big")


def wire_from(first):
    out = bytearray()
    for n in range(first, SECONDS * FPS):
        p = payload(n)
        if n % KEY == 0:
            stamp = frameindex.seconds_to_stamp(ORIGIN + n // FPS)
            out += b"\x00\x00\x01\xfc" + bytes([2, FPS, 0, 0]) + stamp.to_bytes(4, "little") + len(p).to_bytes(4, "little") + p
        else:
            out += b"\x00\x00\x01\xfd" + len(p).to_bytes(4, "little") + p
    return bytes(out)


class FakeFrame:
    width, height = 8, 8

    def __init__(self, n):
        self.n = n


class NumDecoder:
    def decode_raw(self, data):
        yield FakeFrame(int.from_bytes(data[-4:], "big"))


class FakeClient:
    def keepalive_if_due(self):
        pass

    def close(self):
        pass


class FakeNVR:
    """honour: the modes in which the start time is respected (an NVR that cannot seek: empty)."""
    def __init__(self, honour=("ByName", "ByTime"), delay=0.004):
        self.honour, self.delay, self.calls = set(honour), delay, []

    def open(self, item, mode, stream_type):
        self.calls.append((item["begin"][11:], mode))
        first = 0
        if mode in self.honour:
            start = frameindex.wall_seconds(item["begin"]) - ORIGIN
            first = int(start * FPS) // KEY * KEY            # the NVR starts at the key frame at or before it
        wire = wire_from(first)

        def chunks():
            for i in range(0, len(wire), 256):
                time.sleep(self.delay)
                yield wire[i:i + 256]
        return FakeClient(), chunks()


def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TimelineTests(unittest.TestCase):
    def test_without_stamps_it_counts_from_the_guess(self):
        t = Timeline(ORIGIN, 25, start_guess=100.0)
        self.assertEqual(t.time_of(0), 100.0)
        self.assertAlmostEqual(t.time_of(50), 102.0)
        self.assertEqual(t.index_at(102.0), 50)

    def test_the_first_stamp_places_the_piece_in_the_recording(self):
        t = Timeline(ORIGIN, 5)
        t.add_key(0, ORIGIN + 294)                       # the NVR started this piece at 17:04:54
        self.assertAlmostEqual(t.time_of(0), 294.5)
        self.assertAlmostEqual(t.time_of(25), 299.5)
        self.assertEqual(t.index_at(299.5), 25)

    def test_the_rate_in_the_stream_is_kept_unless_the_stamps_disagree_over_a_long_stretch(self):
        t = Timeline(ORIGIN, 25)
        for k in range(0, 40):                           # really 12.5 fps: a key frame (25 frames) every 2 s
            t.add_key(k * 25, ORIGIN + k * 2)
        self.assertAlmostEqual(t.fps, 12.5, delta=0.3)   # 78 s of stamps: the measured rate wins
        short = Timeline(ORIGIN, 25)
        for k in range(0, 10):
            short.add_key(k * 25, ORIGIN + k * 2)
        self.assertEqual(short.fps, 25)                  # only 18 s: trust the stream

    def test_stamps_from_another_scale_are_ignored(self):
        t = Timeline(None, 5, start_guess=7.0)
        t.add_key(0, 12345)
        self.assertEqual(t.time_of(0), 7.0)

    def test_stamp_round_trip(self):
        self.assertEqual(frameindex.stamp_to_seconds(frameindex.seconds_to_stamp(ORIGIN + 3661)), ORIGIN + 3661)
        self.assertIsNone(frameindex.stamp_to_seconds(0))                    # not a date


def fc_stamped(nal, stamp, fps):
    return (b"\x00\x00\x01\xfc" + bytes([2, fps, 0, 0]) + stamp.to_bytes(4, "little")
            + len(nal).to_bytes(4, "little") + nal)


def fd_plain(nal):
    return b"\x00\x00\x01\xfd" + len(nal).to_bytes(4, "little") + nal


class ParserStampTests(unittest.TestCase):
    """What the XM headers tell: the real time of a key frame, and the frame rate (byte 5, not byte 4)."""

    def test_key_frame_time_and_frame_rate(self):
        stamp = frameindex.seconds_to_stamp(ORIGIN + 125)
        key, p_frame = b"\x00\x00\x00\x01\x65abc", b"\x00\x00\x00\x01\x41xyz"
        parser = dvrip.XMFrameParser()
        out = parser.feed(fc_stamped(key, stamp, 12) + fd_plain(p_frame))
        self.assertEqual(out, [key, p_frame])
        self.assertEqual(parser.stamps, [stamp, 0])                 # one entry per frame returned, 0 for a P frame
        self.assertEqual(parser.fps_hint, 12)                       # not 2, which is the media type in byte 4

    def test_stamps_stay_with_their_frames_when_headers_are_embedded_in_a_frame(self):
        s1, s2 = frameindex.seconds_to_stamp(ORIGIN + 10), frameindex.seconds_to_stamp(ORIGIN + 12)
        k1, k2 = b"\x00\x00\x00\x01\x65aaa", b"\x00\x00\x00\x01\x65bbb"
        p_frame = b"\x00\x00\x00\x01\x41ccc"
        # the NVR declared a length that also covers the frames that follow: their headers are inside the data
        body = k1 + fd_plain(p_frame) + fc_stamped(k2, s2, 25)
        parser = dvrip.XMFrameParser()
        out = parser.feed(fc_stamped(body, s1, 25))
        self.assertEqual(out, [k1, p_frame, k2])
        self.assertEqual(parser.stamps, [s1, 0, s2])

    def test_stamps_are_aligned_across_chunk_boundaries(self):
        wire = b"".join(fc_stamped(b"\x00\x00\x00\x01\x65" + bytes([i]),
                                   frameindex.seconds_to_stamp(ORIGIN + i), 25) for i in range(5))
        parser, stamps = dvrip.XMFrameParser(), []
        for i in range(0, len(wire), 7):
            frames = parser.feed(wire[i:i + 7])
            self.assertEqual(len(frames), len(parser.stamps))
            stamps += parser.stamps
        self.assertEqual([frameindex.stamp_to_seconds(x) for x in stamps], [ORIGIN + i for i in range(5)])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._dec = dvrip.H264Decoder
        dvrip.H264Decoder = NumDecoder
        video_bytes = sum(len(payload(n)) for n in range(SECONDS * FPS))        # the size the NVR announces is video only
        self.rec = {"name": "f", "begin": BEGIN, "end": END, "size": video_bytes}
        self.ctl = None

    def tearDown(self):
        if self.ctl:
            self.ctl.close()
        dvrip.H264Decoder = self._dec
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, nvr):
        self.nvr = nvr
        self.ctl = segments.StreamController("1.2.3.4", 3, self.rec, 0, nvr.open, cache_folder=self.dir)
        self.ctl.start()
        return segments.SegmentedSource(self.ctl)

    def first_frames(self, src, t, n=3, timeout=8.0):
        out, end = [], time.monotonic() + timeout
        for item in src.frames_from(t):
            if item is WAITING:
                if time.monotonic() > end:
                    break
                time.sleep(0.02)
                continue
            out.append((round(item[0], 2), item[1].n))
            if len(out) >= n:
                break
        return out

    def test_a_sequential_download_plays_from_the_start_to_the_end(self):
        src = self.make(FakeNVR(delay=0))
        self.assertTrue(wait_for(self.ctl.whole_ready))
        got = [(t, f.n) for t, f in src.frames_from(0) if t is not WAITING]
        self.assertEqual(len(got), SECONDS * FPS)
        self.assertTrue(all(abs(t - n / FPS) < 0.6 for t, n in got[::97]))
        self.assertEqual(self.nvr.calls, [("17:00:00", "ByName")])

    def test_a_jump_starts_a_piece_at_the_target_and_plays_from_there(self):
        src = self.make(FakeNVR())
        self.ctl.request(300.0)
        self.assertTrue(wait_for(lambda: (self.ctl.part_for(300.0) is not None
                                          and self.ctl.part_for(300.0).source.frame_count > 0)))
        self.assertIn(("17:04:54", "ByName"), self.nvr.calls)                  # 6 s early: a key frame before the target
        (t, n), *_ = self.first_frames(src, 300.0, 1)
        self.assertAlmostEqual(t, 300.0, delta=0.7)
        self.assertAlmostEqual(n, 1500, delta=5)                                # within about a second (time stamps are whole seconds)
        self.assertTrue(wait_for(lambda: self.ctl.seek_supported is True))
        self.assertFalse(self.ctl.main.state.complete)                          # the first piece was stopped, not finished

    def test_playing_goes_on_to_the_end_of_the_recording_from_a_jump(self):
        src = self.make(FakeNVR(delay=0))
        self.ctl.request(560.0)
        got, end = [], time.monotonic() + 15
        for item in src.frames_from(560.0):
            if item is WAITING:
                self.assertLess(time.monotonic(), end)
                time.sleep(0.01)
                continue
            got.append(item[1].n)
        self.assertAlmostEqual(got[0], 2800, delta=5)
        self.assertEqual(got[-1], SECONDS * FPS - 1)                           # ends by itself, not stuck waiting

    def test_an_nvr_that_ignores_the_start_time_falls_back_to_one_sequential_download(self):
        src = self.make(FakeNVR(honour=(), delay=0.002))
        self.ctl.request(300.0)
        self.assertTrue(wait_for(lambda: self.ctl.seek_supported is False))
        self.assertIn(("17:04:54", "ByName"), self.nvr.calls)                   # both ways of asking were tried...
        self.assertIn(("17:04:54", "ByTime"), self.nvr.calls)                   # ...before giving up
        self.assertTrue(wait_for(self.ctl.whole_ready, timeout=20))              # the sequential download finishes
        self.assertEqual(len(self.ctl.parts), 1)                                 # the useless pieces were removed
        before = len(self.nvr.calls)
        self.ctl.request(100.0)                                                  # no more attempts to jump
        time.sleep(0.2)
        self.assertEqual(len(self.nvr.calls), before)
        self.assertAlmostEqual(self.first_frames(src, 300.0, 1)[0][1], 1500, delta=5)

    def test_the_other_way_of_asking_is_tried_when_the_first_is_ignored(self):
        src = self.make(FakeNVR(honour=("ByTime",)))
        self.ctl.request(300.0)
        self.assertTrue(wait_for(lambda: self.ctl.seek_supported is True, timeout=15))
        self.assertTrue(any(m == "ByTime" for _t, m in self.nvr.calls))
        self.assertAlmostEqual(self.first_frames(src, 300.0, 1)[0][1], 1500, delta=5)

    def test_pieces_are_reported_as_ranges(self):
        self.make(FakeNVR(delay=0.001))
        self.ctl.request(400.0)
        self.assertTrue(wait_for(lambda: self.ctl.covers(400.0)))
        time.sleep(0.5)
        r = self.ctl.ranges()
        self.assertTrue(any(s <= 400.0 <= e for s, e in r), r)
        self.assertGreater(self.ctl.duration(), 590)

    def test_a_jump_back_into_a_piece_that_already_exists_needs_no_download(self):
        src = self.make(FakeNVR(delay=0))
        self.assertTrue(wait_for(self.ctl.whole_ready))
        calls = len(self.nvr.calls)
        self.ctl.request(123.0)
        self.assertEqual(len(self.nvr.calls), calls)
        self.assertAlmostEqual(self.first_frames(src, 123.0, 1)[0][1], 615, delta=5)

    def test_opening_a_recording_that_is_complete_in_the_cache_downloads_nothing(self):
        self.make(FakeNVR(delay=0))
        self.assertTrue(wait_for(self.ctl.whole_ready))
        self.ctl.close()
        nvr2 = FakeNVR()
        ctl2 = segments.StreamController("1.2.3.4", 3, self.rec, 0, nvr2.open, cache_folder=self.dir)
        ctl2.start()
        try:
            self.assertTrue(ctl2.whole_ready())
            self.assertEqual(nvr2.calls, [])
        finally:
            ctl2.close()


if __name__ == "__main__":
    unittest.main()
