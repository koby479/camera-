import json
import socket
import unittest

from app.core import dvrip, recfmt


class FmtTests(unittest.TestCase):
    def test_size(self):
        self.assertEqual(recfmt.fmt_size(0), "-")
        self.assertEqual(recfmt.fmt_size(500 * 1024), "500 KB")
        self.assertEqual(recfmt.fmt_size(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(recfmt.fmt_size(3 * 1024 ** 3), "3.00 GB")

    def test_duration(self):
        self.assertEqual(recfmt.duration_seconds("2026-08-30 04:00:00", "2026-08-30 04:10:30"), 630)
        self.assertEqual(recfmt.fmt_duration(630), "10:30")
        self.assertEqual(recfmt.fmt_duration(3725), "1:02:05")
        self.assertIsNone(recfmt.duration_seconds("bad", "worse"))

    def test_group_newest_day_first(self):
        fs = [{"begin": "2026-08-29 10:00:00"}, {"begin": "2026-08-30 09:00:00"}, {"begin": "2026-08-30 01:00:00"}]
        g = recfmt.group_by_day(fs)
        self.assertEqual([d for d, _ in g], ["2026-08-30", "2026-08-29"])
        self.assertEqual([f["begin"][11:13] for f in g[0][1]], ["01", "09"])

    def test_filename(self):
        n = recfmt.safe_filename(1, {"begin": "2026-08-30 04:00:00"})
        self.assertEqual(n, "channel2_2026-08-30_04-00-00.h264")


class _Sock:
    def settimeout(self, _t):
        pass


class Scripted(dvrip.DVRIPClient):
    def __init__(self, packets):
        super().__init__("x", 1, "u", "p")
        self.packets = list(packets)
        self.media_sock = _Sock()

    def _recv_packet(self, sock):
        if not self.packets:
            raise socket.timeout("timed out")
        return self.packets.pop(0)


class DownloadTests(unittest.TestCase):
    def test_kilobyte_size_becomes_bytes(self):
        c = Scripted([])
        c.sock = object()
        c._send = lambda *a, **k: None
        c._recv_packet = lambda s: (1441, 0, 0, json.dumps({"Ret": 100, "OPFileQuery": [
            {"FileName": "a", "BeginTime": "b", "EndTime": "e", "FileLength": "0x400"}]}).encode())
        out = c.query_files(0, "2026-08-30 00:00:00", "2026-08-30 02:00:00")
        self.assertEqual(out[0]["size"], 0x400 * 1024)

    def test_download_stops_at_empty_packet(self):
        c = Scripted([(1426, 0, 0, b"aa"), (1426, 0, 0, b"bb"), (1426, 0, 0, b"")])
        self.assertEqual(list(c.read_download_payloads()), [b"aa", b"bb"])

    def test_download_stops_at_eof_message(self):
        c = Scripted([(1426, 0, 0, b"aa"), (1423, 0, 0, b"{}")])
        self.assertEqual(list(c.read_download_payloads()), [b"aa"])


if __name__ == "__main__":
    unittest.main()
