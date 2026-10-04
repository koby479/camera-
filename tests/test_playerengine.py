"""The player's decoding thread, driven with a fake video (10 s, 10 fps, a key frame every 2 s).
Works with or without PyQt6 installed (a tiny stand-in is used when it is missing)."""
import time
import unittest

import numpy as np

_undo = None


def setUpModule():
    global _undo
    try:
        import PyQt6.QtCore  # noqa: F401
    except ImportError:
        import os
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        import qtstub
        _undo = qtstub.install()


def tearDownModule():
    if _undo:
        _undo()


class FakeSource:
    fps, duration, width, height = 10.0, 10.0, 8, 8
    KEY = 20                                         # frames between key frames

    def __init__(self, path=""):
        self.decoded = 0                             # how many frames were decoded: shows what a jump costs

    def frames_from(self, t):
        dt = 1.0 / self.fps
        i = max(0, int(t / dt + 1e-9)) // self.KEY * self.KEY      # a seek lands on the key frame before t
        while i < int(self.duration * self.fps):
            ft = round(i * dt, 6)
            i += 1
            self.decoded += 1
            if ft + 0.5 * dt < t:
                continue
            yield ft, i - 1

    @staticmethod
    def to_rgb(payload, max_width=None):
        return np.full((4, 4, 3), payload % 256, dtype="uint8")

    def close(self):
        pass


def make_engine():
    from app.core.playerengine import PlayerEngine
    shown = []

    class Rec(PlayerEngine):
        def _emit(self, img, t):
            shown.append(round(t, 3))
            self.pending = False                     # the "window" always keeps up

    eng = Rec("x.mp4", source_factory=FakeSource)
    return eng, shown


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.eng, self.shown = make_engine()
        self.eng.start()
        self.assertTrue(wait_for(lambda: self.shown))    # first frame appears by itself
        self.eng.set_paused(True)
        self.assertTrue(wait_for(lambda: self.eng._paused))

    def tearDown(self):
        self.eng.stop()

    def settle(self):
        time.sleep(0.15)

    def test_seek_shows_the_frame_at_that_time(self):
        self.eng.seek(5.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 5.0))

    def test_relative_jumps_add_up(self):
        self.eng.seek(4.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 4.0))
        self.eng.seek_relative(2.0)
        self.eng.seek_relative(1.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 7.0))
        self.eng.seek_relative(-100)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 0.0))        # clamped to the start

    def test_seek_past_the_end_shows_the_last_frame(self):
        self.eng.seek(999)
        self.assertTrue(wait_for(lambda: self.shown[-1] >= 9.8))

    def test_step_forward_and_back(self):
        self.eng.seek(3.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 3.0))
        self.eng.step(1)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 3.1))
        self.eng.step(2)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 3.3))
        self.eng.step(-1)                                   # from the cache
        self.assertTrue(wait_for(lambda: self.shown[-1] == 3.2))

    def test_step_back_beyond_the_cache_is_exact(self):
        self.eng.seek(6.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 6.0))
        self.eng.step(-1)                                   # nothing cached before a seek: decodes
        self.assertTrue(wait_for(lambda: self.shown[-1] == 5.9))
        self.eng.step(1)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 6.0))

    def test_play_forward_reaches_the_end_and_pauses(self):
        self.eng.set_speed(16)
        self.eng.seek(7.0)
        self.eng.set_paused(False)
        self.assertTrue(wait_for(lambda: self.eng._paused and self.shown[-1] >= 9.8))
        times = [t for t in self.shown if t >= 7.0]
        self.assertEqual(times, sorted(times))              # never goes backwards

    def test_play_at_the_end_starts_over(self):
        self.eng.seek(999)
        self.assertTrue(wait_for(lambda: self.shown[-1] >= 9.8))
        self.eng.set_speed(16)
        self.eng.set_paused(False)                          # tries to play on, reaches the end, pauses itself
        self.assertTrue(wait_for(lambda: self.eng._paused and self.eng._eof))
        mark = len(self.shown)
        self.eng.set_paused(False)                          # pressing play at the end: from the beginning
        self.assertTrue(wait_for(lambda: any(t < 1.0 for t in self.shown[mark:])))

    def test_backwards_play_goes_down_to_the_start(self):
        self.eng.set_speed(16)
        self.eng.seek(5.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 5.0))
        mark = len(self.shown)
        self.eng.set_reverse(True)
        self.eng.set_paused(False)
        self.assertTrue(wait_for(lambda: self.eng._paused and self.shown[-1] <= 0.1))
        back = self.shown[mark:]
        self.assertEqual(back, sorted(back, reverse=True))  # strictly descending
        self.assertTrue(all(t < 5.0 for t in back))

    def test_forward_again_after_backwards_does_not_repeat_a_frame(self):
        self.eng.seek(5.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 5.0))
        self.eng.set_reverse(True)
        self.eng.step(-1)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 4.9))
        self.eng.set_reverse(False)
        self.eng.step(1)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 5.0))

    def test_ab_loop_stays_inside_the_loop(self):
        self.eng.set_speed(16)
        self.eng.seek(2.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 2.0))
        self.eng.set_loop(2.0, 3.0)
        mark = len(self.shown)
        self.eng.set_paused(False)
        time.sleep(1.0)
        self.eng.set_paused(True)
        self.settle()
        part = self.shown[mark:]
        self.assertGreater(len(part), 20)
        self.assertTrue(all(1.99 <= t <= 3.05 for t in part), part)
        self.assertGreater(sum(1 for a, b in zip(part, part[1:]) if b < a), 1)   # it wrapped around more than once

    def test_loop_all_restarts_at_the_end(self):
        self.eng.set_speed(16)
        self.eng.set_loop_all(True)
        self.eng.seek(8.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 8.0))
        mark = len(self.shown)
        self.eng.set_paused(False)
        self.assertTrue(wait_for(lambda: any(b < a for a, b in zip(self.shown[mark:], self.shown[mark + 1:]))))
        self.assertFalse(self.eng._paused)

    def test_a_dropped_frame_when_the_window_is_busy_does_not_stall(self):
        self.eng.pending = True                             # the window never draws
        self.eng.set_speed(16)
        self.eng.seek(1.0)
        self.assertTrue(wait_for(lambda: self.shown[-1] == 1.0))   # explicit jumps are always shown
        self.eng.set_paused(False)
        self.assertTrue(wait_for(lambda: self.eng._paused, timeout=6))   # still reaches the end


class OpenFailureTests(unittest.TestCase):
    def test_unreadable_file_ends_the_thread_cleanly(self):
        from app.core.playerengine import PlayerEngine

        def boom(_path):
            raise ValueError("no video")

        eng = PlayerEngine("bad.mp4", source_factory=boom)
        eng.start()
        eng.wait(2000)
        self.assertFalse(eng.isRunning())


if __name__ == "__main__":
    unittest.main()
