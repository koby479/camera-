import pytest

from app.core import dvrip, resumable
from app.core.resumable import ResumableDownload, Cancelled, DownloadFailed
from tests.test_xm_embedded import fc, fd, nal


def make_stream(n_frames=40):
    frames = [nal(200 + i, i % 250 + 1) for i in range(n_frames)]
    wire = fc(frames[0]) + b"".join(fd(f) for f in frames[1:])
    return frames, wire


class FakeNVR:
    """Serves `wire` in 50-byte chunks; each connection may be cut after N chunks or refuse to connect."""
    def __init__(self, wire, plan):
        self.wire, self.plan, self.opened = wire, list(plan), 0

    def open_stream(self):
        self.opened += 1
        step = self.plan.pop(0) if self.plan else None
        if step == "refuse":
            raise ConnectionRefusedError("down")
        wire = self.wire
        class C:
            def close(self_): pass
        def gen():
            for k, i in enumerate(range(0, len(wire), 50)):
                if step is not None and k >= step:
                    raise ConnectionError("cut")
                yield wire[i:i + 50]
        return C(), gen()


def make(tmp_path, nvr, **kw):
    return ResumableDownload(nvr.open_stream, tmp_path / "x.h264.part", {"id": 1}, sleep=lambda s: None, **kw)


def test_clean_download(tmp_path):
    frames, wire = make_stream()
    r = make(tmp_path, FakeNVR(wire, [])).run()
    assert (tmp_path / "x.h264.part").read_bytes() == b"".join(frames)
    assert r.reconnects == 0 and r.complete and not (tmp_path / "x.h264.part.json").exists()


def test_resumes_after_cuts_and_refusals(tmp_path):
    frames, wire = make_stream()
    nvr = FakeNVR(wire, [10, "refuse", 30, "refuse", "refuse", 100])
    r = make(tmp_path, nvr).run()
    assert (tmp_path / "x.h264.part").read_bytes() == b"".join(frames)      # no frame lost, none doubled
    assert r.reconnects == 6 and nvr.opened == 7


def test_resume_after_program_restart(tmp_path):
    frames, wire = make_stream()
    nvr = FakeNVR(wire, [25])
    seen = []

    def cancel_after_cut():
        return nvr.opened >= 2                                  # user closes the program while it waits
    with pytest.raises(Cancelled):
        make(tmp_path, nvr, cancelled=cancel_after_cut).run()
    assert (tmp_path / "x.h264.part.json").exists()
    r = make(tmp_path, FakeNVR(wire, [])).run()                 # a new run picks up the saved state
    assert (tmp_path / "x.h264.part").read_bytes() == b"".join(frames)


def test_different_recording_does_not_resume(tmp_path):
    frames, wire = make_stream()
    nvr = FakeNVR(wire, [25])
    with pytest.raises(Cancelled):
        make(tmp_path, nvr, cancelled=lambda: nvr.opened >= 2).run()
    d = ResumableDownload(FakeNVR(wire, []).open_stream, tmp_path / "x.h264.part", {"id": 2}, sleep=lambda s: None)
    d.run()                                                     # other identity: starts from scratch, no stale mixing
    assert (tmp_path / "x.h264.part").read_bytes() == b"".join(frames)


def test_mismatching_stream_restarts_from_scratch(tmp_path):
    frames, wire = make_stream()
    other_frames, other_wire = make_stream(60)
    other_frames = [nal(500 + i, 7) for i in range(60)]
    other_wire = fc(other_frames[0]) + b"".join(fd(f) for f in other_frames[1:])

    class Switch(FakeNVR):
        def open_stream(self):
            if self.opened >= 1:
                self.wire = other_wire                          # second connection serves different data
            return super().open_stream()
    r = make(tmp_path, Switch(wire, [25])).run()
    assert (tmp_path / "x.h264.part").read_bytes() == b"".join(other_frames)


def test_wrong_password_is_not_retried_forever(tmp_path):
    def bad():
        raise dvrip.DVRIPAuthError("bad password")
    with pytest.raises(DownloadFailed):
        ResumableDownload(bad, tmp_path / "x.h264.part", {}, sleep=lambda s: None).run()


def test_short_stream_is_accepted_after_one_retry(tmp_path):
    frames, wire = make_stream()
    nvr = FakeNVR(wire, [])
    r = make(tmp_path, nvr, expected=len(wire) * 10).run()       # NVR claims 10x more than it ever sends
    assert not r.complete and nvr.opened == 2


def test_frame_index_matches_the_file_after_cuts_and_a_restart(tmp_path):
    from app.core import frameindex
    frames, wire = make_stream()
    nvr = FakeNVR(wire, [25])
    with pytest.raises(Cancelled):
        make(tmp_path, nvr, cancelled=lambda: nvr.opened >= 2, index=frameindex.IndexWriter(tmp_path / "x.idx")).run()
    make(tmp_path, FakeNVR(wire, [10, "refuse", 60]), index=frameindex.IndexWriter(tmp_path / "x.idx")).run()
    raw = (tmp_path / "x.h264.part").read_bytes()
    recs = frameindex.read_records(tmp_path / "x.idx", 0, 10 ** 6)
    assert [raw[o:o + n] for o, n, _key in recs] == frames       # every frame indexed once, in order, at the right place
