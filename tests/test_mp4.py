import unittest
from fractions import Fraction

from app.core import mp4


class FrameRateTests(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(mp4.frame_rate(15000, 600), Fraction(25))

    def test_fractional_rate_is_kept_exact(self):
        self.assertEqual(mp4.frame_rate(17982, 600), Fraction(17982, 600).limit_denominator(1000))

    def test_missing_or_absurd_duration_falls_back(self):
        self.assertEqual(mp4.frame_rate(100, None), Fraction(25))
        self.assertEqual(mp4.frame_rate(100, 0), Fraction(25))
        self.assertEqual(mp4.frame_rate(100000, 10), Fraction(25))     # 10000 fps is nonsense
        self.assertEqual(mp4.frame_rate(0, 60), Fraction(25))


if __name__ == "__main__":
    unittest.main()


def test_raw_to_mp4_roundtrip(tmp_path):
    import pytest
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    raw, out = tmp_path / "a.h264", tmp_path / "a.mp4"
    c = av.open(str(raw), "w", format="h264")
    st = c.add_stream("libx264", rate=25)
    st.width, st.height, st.pix_fmt = 320, 240, "yuv420p"
    for i in range(50):
        f = av.VideoFrame.from_ndarray(np.full((240, 320, 3), i * 4, dtype=np.uint8), format="rgb24")
        for p in st.encode(f):
            c.mux(p)
    for p in st.encode():
        c.mux(p)
    c.close()
    n = mp4.raw_to_mp4(str(raw), str(out), "h264", 2.04)      # 24.5 fps, not a round rate
    assert n == 50
    with av.open(str(out)) as r:
        assert abs(float(r.duration) / 1_000_000 - 2.04) < 0.1


def test_ffmpeg_fallback_when_pyav_fails(tmp_path, monkeypatch):
    import shutil
    import pytest
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    raw, out = tmp_path / "b.h265", tmp_path / "b.mp4"
    c = av.open(str(raw), "w", format="hevc")
    st = c.add_stream("libx265", rate=25)
    st.width, st.height, st.pix_fmt = 320, 240, "yuv420p"
    for i in range(40):
        f = av.VideoFrame.from_ndarray(np.full((240, 320, 3), i * 5, dtype=np.uint8), format="rgb24")
        for p in st.encode(f):
            c.mux(p)
    for p in st.encode():
        c.mux(p)
    c.close()
    assert mp4.count_frames(str(raw), "hevc") == 40
    monkeypatch.setattr(mp4, "_pyav_remux", lambda *a: (_ for _ in ()).throw(ValueError("boom")))
    assert mp4.raw_to_mp4(str(raw), str(out), "hevc", 1.6) == 40
    with av.open(str(out)) as r:
        assert abs(float(r.duration) / 1_000_000 - 1.6) < 0.1
