import json
import socket
import unittest

from app.core import dvrip


class Scripted(dvrip.DVRIPClient):
    """Replies to file queries from a script: a list of item-lists, or None = the NVR stays silent."""

    def __init__(self, script):
        super().__init__("x", 1, "u", "p")
        self.script = list(script)
        self.sock = object()
        self.sent = 0

    def _send(self, sock, msgid, obj):
        self.sent += 1

    def _recv_packet(self, sock):
        if not self.script:
            raise socket.timeout("timed out")
        step = self.script.pop(0)
        if step is None:
            raise socket.timeout("timed out")
        payload = json.dumps({"Name": "OPFileQuery", "Ret": 100, "OPFileQuery": step}).encode()
        return 1441, 0, 0, payload


def files(n, start=0):
    return [{"FileName": f"f{start + i}", "BeginTime": f"2026-09-30 00:{(start + i) % 60:02d}:00",
             "EndTime": f"2026-09-30 00:{(start + i) % 60:02d}:30", "FileLength": "0x10"} for i in range(n)]


class QueryPageTests(unittest.TestCase):
    def test_short_page_is_complete_and_no_second_query_is_sent(self):
        c = Scripted([files(2)])
        out = c.query_files(0, "2026-09-30 00:00:00", "2026-09-30 02:00:00")
        self.assertEqual(len(out), 2)
        self.assertEqual(c.sent, 1)              # the follow-up that this NVR never answered is gone

    def test_silence_on_the_second_page_keeps_the_first(self):
        c = Scripted([files(64), None])
        out = c.query_files(0, "2026-09-30 00:00:00", "2026-09-30 02:00:00")
        self.assertEqual(len(out), 64)
        self.assertTrue(c.needs_reconnect)

    def test_silence_on_first_query_raises_until_the_nvr_has_answered_once(self):
        c = Scripted([None])
        with self.assertRaises(socket.timeout):
            c.query_files(0, "2026-09-30 00:00:00", "2026-09-30 02:00:00")
        c2 = Scripted([files(1), None])
        c2.query_files(0, "2026-09-30 00:00:00", "2026-09-30 02:00:00")
        self.assertEqual(c2.query_files(0, "2026-09-30 02:00:00", "2026-09-30 04:00:00"), [])

    def test_full_pages_are_followed(self):
        c = Scripted([files(64), files(3, 64)])
        out = c.query_files(0, "2026-09-30 00:00:00", "2026-09-30 02:00:00")
        self.assertEqual(len(out), 67)


if __name__ == "__main__":
    unittest.main()
