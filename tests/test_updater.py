import hashlib
import http.server
import os
import tempfile
import threading
import unittest
from pathlib import Path

from app.core import updater

PAYLOAD = b"MZ" + b"\x00" * 5000          # looks like a tiny EXE; tests pass min_bytes explicitly
MIN = 1000


class _Handler(http.server.BaseHTTPRequestHandler):
    latest = 28
    serve_sha = True
    corrupt_sha = False
    truncate = False

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        if self.path == "/releases/latest":
            self.send_response(302)
            self.send_header("Location", f"/releases/tag/build-{self.latest}")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path.endswith(".exe.sha256"):
            if not self.serve_sha:
                self.send_response(404)
                self.end_headers()
                return
            digest = hashlib.sha256(PAYLOAD).hexdigest()
            if self.corrupt_sha:
                digest = "0" * 64
            body = (digest + "  UniversalCamViewer.exe\n").encode()
        elif self.path.endswith("/UniversalCamViewer.exe") and f"build-{self.latest}" in self.path:
            body = PAYLOAD
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body) + (10 if self.truncate else 0)))
        self.end_headers()
        self.wfile.write(body)


class UpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Handler.latest, _Handler.serve_sha, _Handler.corrupt_sha, _Handler.truncate = 28, True, False, False
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_build_tag(self):
        self.assertEqual(updater.parse_build_tag("https://github.com/x/y/releases/tag/build-27"), 27)
        self.assertIsNone(updater.parse_build_tag("https://github.com/x/y/releases"))

    def test_latest_build_reads_the_redirect(self):
        self.assertEqual(updater.latest_build(self.base), 28)

    def test_latest_build_without_internet_is_a_readable_error(self):
        with self.assertRaises(updater.UpdateError):
            updater.latest_build("http://127.0.0.1:1", timeout=2)

    def test_download_verifies_and_reports_progress(self):
        seen = []
        dest = self.dir / "x.tmp"
        digest = updater.download(28, dest, progress=lambda d, t: seen.append((d, t)), base=self.base, min_bytes=MIN)
        self.assertEqual(dest.read_bytes(), PAYLOAD)
        self.assertEqual(digest, hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(seen[-1][0], len(PAYLOAD))

    def test_download_without_sha_file_still_works(self):
        _Handler.serve_sha = False
        dest = self.dir / "x.tmp"
        updater.download(28, dest, base=self.base, min_bytes=MIN)
        self.assertTrue(dest.exists())

    def test_wrong_sha_is_rejected_and_nothing_is_left(self):
        _Handler.corrupt_sha = True
        dest = self.dir / "x.tmp"
        with self.assertRaises(updater.UpdateError):
            updater.download(28, dest, base=self.base, min_bytes=MIN)
        self.assertFalse(dest.exists())

    def test_missing_release_says_it_is_still_building(self):
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download(99, self.dir / "x.tmp", base=self.base, min_bytes=MIN)
        self.assertIn("בבנייה", str(cm.exception))

    def test_cancel_removes_the_partial_file(self):
        dest = self.dir / "x.tmp"
        with self.assertRaises(updater.UpdateError):
            updater.download(28, dest, base=self.base, cancelled=lambda: True, min_bytes=MIN)
        self.assertFalse(dest.exists())

    def test_verify_rejects_a_non_exe_and_a_tiny_file(self):
        txt = self.dir / "a.exe"
        txt.write_bytes(b"hello" * 1000)
        with self.assertRaises(updater.UpdateError):
            updater.verify_exe(txt, MIN)
        tiny = self.dir / "b.exe"
        tiny.write_bytes(b"MZ" + b"0" * 10)
        with self.assertRaises(updater.UpdateError):
            updater.verify_exe(tiny, MIN)

    def test_apply_swaps_and_keeps_the_previous_version(self):
        exe = self.dir / "UniversalCamViewer.exe"
        exe.write_bytes(b"MZ-old")
        new = self.dir / "UniversalCamViewer.exe.update.tmp"
        new.write_bytes(b"MZ-new")
        updater.apply(new, exe)
        self.assertEqual(exe.read_bytes(), b"MZ-new")
        self.assertEqual(updater.old_path(exe).read_bytes(), b"MZ-old")
        self.assertFalse(new.exists())

    def test_apply_replaces_an_older_previous_version(self):
        exe = self.dir / "UniversalCamViewer.exe"
        exe.write_bytes(b"MZ-2")
        updater.old_path(exe).write_bytes(b"MZ-1")
        new = self.dir / "n.tmp"
        new.write_bytes(b"MZ-3")
        updater.apply(new, exe)
        self.assertEqual(updater.old_path(exe).read_bytes(), b"MZ-2")

    def test_apply_undoes_itself_when_the_new_file_is_missing(self):
        exe = self.dir / "UniversalCamViewer.exe"
        exe.write_bytes(b"MZ-keep")
        with self.assertRaises(updater.UpdateError):
            updater.apply(self.dir / "missing.tmp", exe)
        self.assertEqual(exe.read_bytes(), b"MZ-keep")

    def test_rollback_swaps_back_and_can_be_undone(self):
        exe = self.dir / "UniversalCamViewer.exe"
        big = b"MZ" + b"A" * updater.MIN_EXE_BYTES
        big2 = b"MZ" + b"B" * updater.MIN_EXE_BYTES
        exe.write_bytes(big2)
        updater.old_path(exe).write_bytes(big)
        self.assertTrue(updater.rollback_available(exe))
        updater.rollback(exe)
        self.assertEqual(exe.read_bytes(), big)
        self.assertEqual(updater.old_path(exe).read_bytes(), big2)

    def test_cleanup_removes_temp_files_but_keeps_the_previous_version(self):
        exe = self.dir / "UniversalCamViewer.exe"
        exe.write_bytes(b"MZ")
        for name in ("UniversalCamViewer.exe.update.tmp", "UniversalCamViewer.exe.old1700000000",
                     "UniversalCamViewer.exe.rollback.tmp", "UniversalCamViewer.exe.old", "other.txt"):
            (self.dir / name).write_bytes(b"x")
        updater.cleanup_leftovers(exe)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["UniversalCamViewer.exe", "UniversalCamViewer.exe.old", "other.txt"])

    def test_relaunch_command_waits_then_starts(self):
        args = updater.relaunch_args(Path(r"C:\Program Files\Cam\UniversalCamViewer.exe"), 3)
        self.assertEqual(args[:3], ["cmd.exe", "/c", "ping"])
        self.assertIn("start", args)
        self.assertEqual(args[-1], r"C:\Program Files\Cam\UniversalCamViewer.exe")

    def test_build_info_roundtrip(self):
        out = self.dir / "info.txt"
        updater.write_build_info(str(out))
        from app import BUILD, VERSION
        self.assertEqual(out.read_text(encoding="utf-8"), f"{VERSION}|{BUILD}")

    @unittest.skipIf(os.name == "nt", "uses a shell script as the fake program")
    def test_candidate_info_reads_the_version_another_copy_reports(self):
        fake = self.dir / "fake.exe"
        fake.write_text('#!/bin/sh\necho "1.1|30" > "$2"\n')
        fake.chmod(0o755)
        self.assertEqual(updater.candidate_info(fake, timeout=10), ("1.1", 30))

    @unittest.skipIf(os.name == "nt", "uses a shell script as the fake program")
    def test_candidate_info_gives_up_on_a_copy_that_does_not_know_the_flag(self):
        fake = self.dir / "old.exe"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        self.assertIsNone(updater.candidate_info(fake, timeout=5))

    def test_dropin_name_sits_next_to_the_program(self):
        self.assertEqual(updater.dropin_path(self.dir / "UniversalCamViewer.exe"), self.dir / "UniversalCamViewer.new.exe")


if __name__ == "__main__":
    unittest.main()
