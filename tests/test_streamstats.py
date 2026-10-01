import unittest

from app.core.streamstats import StreamStats


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class StreamStatsTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.s = StreamStats(interval=9, clock=self.clock)

    def tick(self, seconds, *, chunk=False, frame=False):
        self.clock.t += seconds
        if chunk:
            self.s.chunk()
        if frame:
            self.s.frame()

    def test_no_report_before_interval(self):
        self.tick(5, chunk=True, frame=True)
        self.assertIsNone(self.s.report())

    def test_smooth_stream_is_not_notable(self):
        for _ in range(100):
            self.tick(0.1, chunk=True, frame=True)
        line = self.s.report()
        self.assertIn("10.0 fps", line)
        self.assertFalse(self.s.notable)

    def test_stall_is_counted_and_shows_network_gap(self):
        for _ in range(50):
            self.tick(0.1, chunk=True, frame=True)
        self.tick(2.0, chunk=True, frame=True)          # the NVR went quiet for two seconds
        for _ in range(30):
            self.tick(0.1, chunk=True, frame=True)
        line = self.s.report()
        self.assertTrue(self.s.notable)
        self.assertIn("stalls over 500 ms: 1", line)
        self.assertIn("between network chunks 2000 ms", line)

    def test_decode_stall_without_network_gap(self):
        for _ in range(50):
            self.tick(0.1, chunk=True)
            self.s.frame()
        self.tick(0.0)
        self.clock.t += 1.5
        self.s.frame()                                   # chunks kept coming, but no frame came out
        self.clock.t += 3.5
        line = self.s.report()
        self.assertIn("longest gap between frames 1500 ms", line)

    def test_dropped_frames_make_it_notable(self):
        self.s.dropped()
        self.tick(10)
        self.assertIn("dropped because the window was busy: 1", self.s.report())
        self.assertTrue(self.s.notable)

    def test_counters_reset_after_report(self):
        self.s.dropped()
        self.tick(10)
        self.s.report()
        self.tick(10)
        self.s.report()
        self.assertFalse(self.s.notable)
        self.assertEqual(self.s.reports, 2)


if __name__ == "__main__":
    unittest.main()
