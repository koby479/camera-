import unittest
from datetime import datetime

from app.core import recfmt
from app.core.playclock import FrameHistory, PlaybackClock, SPEEDS, fmt_clock, step_speed


class Now:
    def __init__(self):
        self.t = 50.0

    def __call__(self):
        return self.t


class SpeedTests(unittest.TestCase):
    def test_steps_and_limits(self):
        self.assertEqual(step_speed(1.0, +1), 1.5)
        self.assertEqual(step_speed(1.0, -1), 0.5)
        self.assertEqual(step_speed(SPEEDS[-1], +1), SPEEDS[-1])
        self.assertEqual(step_speed(SPEEDS[0], -1), SPEEDS[0])

    def test_odd_value_snaps_to_nearest(self):
        self.assertEqual(step_speed(3.0, +1), 4.0)            # 3.0 snaps to 2.0 first, then one step up


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.now = Now()
        self.c = PlaybackClock(self.now)

    def test_forward_real_time(self):
        self.c.start(10.0, 1.0, 1)
        self.now.t += 2
        self.assertAlmostEqual(self.c.media_time(), 12.0)

    def test_double_speed_and_due_in(self):
        self.c.start(0.0, 2.0, 1)
        self.assertAlmostEqual(self.c.due_in(4.0), 2.0)       # 4 s of video = 2 s of real time
        self.now.t += 3
        self.assertLess(self.c.due_in(4.0), 0)                # now late

    def test_backward(self):
        self.c.start(30.0, 1.0, -1)
        self.now.t += 5
        self.assertAlmostEqual(self.c.media_time(), 25.0)
        self.assertAlmostEqual(self.c.due_in(20.0), 5.0)      # the frame at 20 s is still 5 s away

    def test_slow_motion(self):
        self.c.start(0.0, 0.25, 1)
        self.assertAlmostEqual(self.c.due_in(1.0), 4.0)


class HistoryTests(unittest.TestCase):
    def test_back_and_forward(self):
        h = FrameHistory(maxlen=5)
        for i in range(4):
            h.push(i, f"f{i}")
        self.assertEqual(h.back(), (2, "f2"))
        self.assertEqual(h.back(), (1, "f1"))
        self.assertEqual(h.forward(), (2, "f2"))
        self.assertEqual(h.forward(), (3, "f3"))
        self.assertIsNone(h.forward())                        # at the newest: caller must decode a new one

    def test_nothing_before_the_oldest(self):
        h = FrameHistory(maxlen=3)
        for i in range(10):
            h.push(i, i)
        self.assertEqual(len(h), 3)
        h.back(); h.back()
        self.assertIsNone(h.back())

    def test_push_after_stepping_back_drops_the_stale_future(self):
        h = FrameHistory()
        for i in range(3):
            h.push(i, i)
        h.back()
        h.push(9, 9)
        self.assertEqual(h.back(), (1, 1))
        self.assertEqual(h.forward(), (9, 9))                 # the old frame 2 is gone, 9 replaced it

    def test_clear(self):
        h = FrameHistory()
        h.push(0, 0)
        h.clear()
        self.assertIsNone(h.back())


class FormatTests(unittest.TestCase):
    def test_fmt_clock(self):
        self.assertEqual(fmt_clock(75), "01:15")
        self.assertEqual(fmt_clock(3725), "1:02:05")
        self.assertEqual(fmt_clock(None), "00:00")

    def test_parse_stamp(self):
        self.assertEqual(recfmt.parse_stamp("channel3_2026-10-01_14-03-22.mp4"), datetime(2026, 10, 1, 14, 3, 22))
        self.assertEqual(recfmt.parse_stamp("channel3_2026-10-01_14-03-22_2.mp4"), datetime(2026, 10, 1, 14, 3, 22))
        self.assertIsNone(recfmt.parse_stamp("holiday.mp4"))
        self.assertIsNone(recfmt.parse_stamp("channel1_2026-13-45_99-99-99.mp4"))


if __name__ == "__main__":
    unittest.main()
