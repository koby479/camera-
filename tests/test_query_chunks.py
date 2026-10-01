import socket
import unittest

from app.core import dvrip


class FakeClient(dvrip.DVRIPClient):
    """No network: query_files answers from a table, or times out for chosen windows."""

    def __init__(self, slow_hours=(), files_per_window=2):
        super().__init__("x", 1, "u", "p")
        self.slow_hours = set(slow_hours)
        self.files_per_window = files_per_window
        self.calls = []
        self.reconnects = 0

    def connect(self):
        self.reconnects += 1
        self.sock = _FakeSock()

    def login(self):
        return {}

    def close(self):
        self.sock = self.media_sock = None

    def query_files(self, channel, begin, end, file_type="h264", variant=None):
        self.calls.append((begin, end))
        hour = int(begin[11:13])
        if hour in self.slow_hours:
            raise socket.timeout("timed out")
        return [{"name": f"{begin}-{i}", "begin": begin, "end": end, "size": 1}
                for i in range(self.files_per_window)]


class _FakeSock:
    def settimeout(self, _t):
        pass


def run(client):
    client.sock = _FakeSock()
    return client.query_files_chunked(0, "2026-08-30 00:00:00", "2026-08-30 23:59:59", chunk_hours=2)


class ChunkedQueryTests(unittest.TestCase):
    def test_all_windows_answer(self):
        files, bad = run(FakeClient())
        self.assertEqual(bad, [])
        self.assertEqual(len(files), 12 * 2)                      # 12 two-hour windows
        self.assertEqual(files, sorted(files, key=lambda f: f["begin"]))

    def test_covers_the_whole_day_without_gaps(self):
        c = FakeClient()
        run(c)
        self.assertEqual(c.calls[0][0], "2026-08-30 00:00:00")
        self.assertEqual(c.calls[-1][1], "2026-08-30 23:59:59")
        for (_b, e), (b, _e) in zip(c.calls, c.calls[1:]):
            self.assertEqual(e, b)

    def test_one_slow_window_keeps_the_rest(self):
        c = FakeClient(slow_hours={4})
        files, bad = run(c)
        self.assertEqual(bad, ["04:00-06:00"])
        self.assertEqual(len(files), 11 * 2)
        self.assertEqual(c.reconnects, 1)                         # reconnected once after the timeout

    def test_gives_up_after_three_silent_windows_in_a_row(self):
        c = FakeClient(slow_hours=set(range(24)))
        with self.assertRaises(dvrip.DVRIPError):
            run(c)
        self.assertEqual(len(c.calls), 3)                         # did not grind through all 12

    def test_partial_result_kept_when_it_goes_silent_later(self):
        c = FakeClient(slow_hours=set(range(10, 24)))
        files, bad = run(c)
        self.assertEqual(len(files), 5 * 2)                       # windows 00-10
        self.assertEqual(len(bad), 3)

    def test_duplicate_file_across_windows_counted_once(self):
        class Dup(FakeClient):
            def query_files(self, channel, begin, end, file_type="h264", variant=None):
                return [{"name": "same.h264", "begin": "2026-08-30 01:59:00", "end": end, "size": 1}]
        files, _bad = run(Dup())
        self.assertEqual(len(files), 1)

    def test_progress_reports_each_window(self):
        seen = []
        c = FakeClient(slow_hours={0})
        c.sock = _FakeSock()
        c.query_files_chunked(0, "2026-08-30 00:00:00", "2026-08-30 05:59:59", chunk_hours=2,
                              progress=lambda w, n, s, e: seen.append((w, n)))
        self.assertEqual(seen[0], ("00:00-02:00", None))
        self.assertEqual([n for _w, n in seen[1:]], [2, 2])


if __name__ == "__main__":
    unittest.main()
