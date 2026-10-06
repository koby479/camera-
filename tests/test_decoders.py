"""The decoder backends: pictures come out of a real H.264 stream through ffmpeg.exe / OpenCV, the choice logic
falls back sensibly, and to_rgb accepts every kind of frame. PyAV itself is not needed for these tests."""
import base64
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from app.core import decoders, dvrip, settings


def _clip() -> bytes:
    return base64.b64decode(decoders._TEST_CLIP_B64)


def _decode_all(name: str) -> list:
    be = decoders._CLASSES[name]("h264", None)
    try:
        frames = []
        data = _clip()
        for i in range(0, len(data), 1000):
            frames += list(be.feed(data[i:i + 1000]))
        return frames + list(be.finish())
    finally:
        be.close()


class FrameTests(unittest.TestCase):
    def test_npframe_to_rgb_scales_and_is_writable(self):
        f = decoders.NpFrame(np.zeros((100, 160, 3), np.uint8))
        out = decoders.to_rgb(f, 80)
        self.assertEqual(out.shape, (50, 80, 3))
        self.assertTrue(out.flags.writeable and out.flags.c_contiguous)
        self.assertEqual(decoders.to_rgb(f, None).shape, (100, 160, 3))
        self.assertEqual(decoders.to_rgb(f, 1000).shape, (100, 160, 3))      # never enlarged

    def test_bgr_view(self):
        rgb = np.zeros((2, 2, 3), np.uint8)
        rgb[..., 0] = 200
        self.assertEqual(int(decoders.NpFrame(rgb).to_ndarray("bgr24")[0, 0, 2]), 200)


@unittest.skipUnless(shutil.which("ffmpeg"), "no ffmpeg on this machine")
class FFmpegBackendTests(unittest.TestCase):
    def test_decodes_every_picture(self):
        frames = _decode_all("ffmpeg")
        self.assertEqual(len(frames), 10)
        self.assertEqual((frames[0].width, frames[0].height), (64, 48))

    def test_wide_pictures_are_capped(self):
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "wide.h264"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=25",
                            "-frames:v", "5", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                            "-x264-params", "keyint=5:repeat-headers=1", "-f", "h264", str(raw)], check=True)
            be = decoders.FFmpegBackend("h264", 640)
            try:
                frames = list(be.feed(raw.read_bytes())) + list(be.finish())
            finally:
                be.close()
            self.assertEqual(len(frames), 5)
            self.assertEqual(frames[0].width, 1280)

    def test_dead_process_does_not_raise(self):
        be = decoders.FFmpegBackend("h264", None)
        be.close()
        self.assertEqual(list(be.feed(b"\x00\x00\x01\x67")), [])


class OpenCVBackendTests(unittest.TestCase):
    def setUp(self):
        try:
            import cv2                                      # noqa: F401
        except ImportError:
            self.skipTest("no opencv")

    def test_decodes_every_picture(self):
        frames = _decode_all("opencv")
        self.assertEqual(len(frames), 10)
        self.assertEqual((frames[0].width, frames[0].height), (64, 48))


class ChoiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self._old_file, self._old_cache = settings.SETTINGS_FILE, settings._cache
        settings.SETTINGS_FILE, settings._cache = self._tmp / "settings.json", None
        self._old_avail = dict(decoders._avail)
        decoders.reset()

    def tearDown(self):
        settings.SETTINGS_FILE, settings._cache = self._old_file, self._old_cache
        decoders._avail.clear()
        decoders._avail.update(self._old_avail)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_auto_skips_unavailable_backends(self):
        decoders._avail.update({"pyav": "missing", "ffmpeg": None, "opencv": None})
        self.assertEqual(decoders.candidates(), ["ffmpeg", "opencv"])

    def test_explicit_choice_is_the_only_candidate(self):
        decoders._avail.update({"pyav": None, "ffmpeg": None, "opencv": None})
        settings.put("decoder", "opencv")
        self.assertEqual(decoders.candidates(), ["opencv"])

    def test_nothing_available_explains_why(self):
        decoders._avail.update({"pyav": "no dll", "ffmpeg": "no exe", "opencv": "no cv2"})
        with self.assertRaises(RuntimeError) as cm:
            decoders.candidates()
        self.assertIn("no dll", str(cm.exception))

    def test_open_backend_falls_through_to_the_next(self):
        class Bad:
            def __init__(self, *a):
                raise RuntimeError("blocked")

        class Good:
            name = "good"

            def __init__(self, *a):
                pass

        saved = dict(decoders._CLASSES)
        decoders._CLASSES.update({"pyav": Bad, "ffmpeg": Good})
        try:
            self.assertEqual(decoders.open_backend("h264", ["pyav", "ffmpeg"]).name, "good")
        finally:
            decoders._CLASSES.update(saved)

    def test_front_decoder_waits_for_a_key_frame_then_uses_backend(self):
        decoders._avail.update({"pyav": "x", "ffmpeg": None, "opencv": "x"})
        if not shutil.which("ffmpeg"):
            self.skipTest("no ffmpeg on this machine")
        d = dvrip.H264Decoder()
        self.assertEqual(list(d.decode_raw(b"\x00\x00\x01\x41\x00")), [])          # P frame: nothing yet
        frames = []
        data = _clip() * 8                                   # a live stream keeps going: pictures are held back a few frames
        for i in range(0, len(data), 1000):
            frames += list(d.decode_raw(data[i:i + 1000]))
        deadline = time.time() + 5
        while not frames and time.time() < deadline:        # ffmpeg answers a little later than it is fed
            frames += list(d.decode_raw(b""))
            time.sleep(0.05)
        d.close()
        self.assertEqual((d.codec, d.backend), ("h264", "ffmpeg"))
        self.assertGreaterEqual(len(frames), 1)

    def _fake_backends(self, first_gives_pictures: bool):
        class Silent:
            name = "pyav"
            closed = False

            def __init__(self, *a):
                pass

            def feed(self, data):
                return iter(())

            def close(self):
                Silent.closed = True

        class Works:
            name = "ffmpeg"

            def __init__(self, *a):
                pass

            def feed(self, data):
                yield decoders.NpFrame(np.zeros((4, 4, 3), np.uint8))

            def close(self):
                pass

        class WorksFirst(Works):
            name = "pyav"

        saved = dict(decoders._CLASSES)
        decoders._CLASSES.update({"pyav": WorksFirst if first_gives_pictures else Silent, "ffmpeg": Works})
        decoders._avail.update({"pyav": None, "ffmpeg": None, "opencv": "x"})
        self.addCleanup(decoders._CLASSES.update, saved)
        return Silent

    KEY = b"\x00\x00\x01\x67\x00\x00\x00"

    def test_front_decoder_moves_on_when_a_backend_never_produces_a_picture(self):
        Silent = self._fake_backends(first_gives_pictures=False)
        d = dvrip.H264Decoder()
        out = []
        for _ in range(dvrip.H264Decoder.FALLBACK_AFTER + 3):
            out += list(d.decode_raw(self.KEY))
        self.assertEqual(d.backend, "ffmpeg")
        self.assertTrue(Silent.closed)
        self.assertGreaterEqual(len(out), 1)

    def test_front_decoder_stays_when_pictures_come(self):
        self._fake_backends(first_gives_pictures=True)
        d = dvrip.H264Decoder()
        for _ in range(dvrip.H264Decoder.FALLBACK_AFTER + 3):
            list(d.decode_raw(self.KEY))
        self.assertEqual(d.backend, "pyav")

    def test_no_backend_can_start_raises_a_stream_error_not_a_crash(self):
        class Bad:
            def __init__(self, *a):
                raise RuntimeError("blocked")

        saved = dict(decoders._CLASSES)
        decoders._CLASSES.update({"pyav": Bad, "ffmpeg": Bad})
        self.addCleanup(decoders._CLASSES.update, saved)
        decoders._avail.update({"pyav": None, "ffmpeg": None, "opencv": "x"})
        d = dvrip.H264Decoder()
        with self.assertRaises(dvrip.DVRIPError):
            list(d.decode_raw(self.KEY))

    def test_several_opencv_backends_can_start_at_once(self):
        try:
            import cv2                                      # noqa: F401
        except ImportError:
            self.skipTest("no opencv")
        import threading
        results = []

        def run():
            try:
                results.append(len(_decode_all("opencv")))
            except Exception as exc:  # noqa: BLE001
                results.append(repr(exc))

        threads = [threading.Thread(target=run) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(results, [10] * 5)

    def test_self_test_reports_each_backend(self):
        decoders._avail.update({"pyav": "missing", "ffmpeg": "missing", "opencv": "missing"})
        res = decoders.self_test()
        self.assertEqual(set(res), set(decoders.BACKENDS))
        self.assertTrue(all(not ok for ok, _ in res.values()))


if __name__ == "__main__":
    unittest.main()
