"""Right-click "reconnect" on a tile: same tile, same place in the grid, a new connection.
(Needs PyQt6; the real camera threads are replaced by do-nothing stand-ins.)"""
import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.core.camera import NO_SUB, CameraConfig, CameraStatus  # noqa: E402


class FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, f):
        self.slots.append(f)

    def disconnect(self):
        self.slots.clear()


class FakeWorker:
    made = []

    def __init__(self, cfg):
        self.cfg = cfg
        self.force_main = False
        self.frame_ready, self.status_changed = FakeSignal(), FakeSignal()
        self.path_resolved, self.audio_ready = FakeSignal(), FakeSignal()
        self.started = self.stopped = False
        FakeWorker.made.append(self)

    def _update_width(self):
        pass

    def start(self):
        self.started = True

    def request_stop(self):
        self.stopped = True

    def stop(self):
        self.stopped = True

    def isRunning(self):
        return False

    def wait(self, ms=0):
        pass

    def set_quality(self, main):
        self.force_main = main


@pytest.fixture
def win(monkeypatch):
    from app import main as m
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(m, "load_all", lambda: ([], []))
    monkeypatch.setattr(m, "CameraWorker", FakeWorker)
    FakeWorker.made = []
    w = m.MainWindow()
    yield w
    w.close()
    del app


def open_cams(win, n=3, **kw):
    cfgs = [CameraConfig(name=f"cam{i}", host="1.2.3.4", port=34567, channel=i, **kw) for i in range(n)]
    for c in cfgs:
        win.on_toggle_camera(c)
    return cfgs


def test_reconnect_replaces_only_the_worker(win):
    a, b, c = open_cams(win)
    order = list(win.grid.tiles)
    tile = win.grid.tiles[b.id]
    tile.set_status(CameraStatus.OFFLINE)
    old = win.workers[b.id]
    win._reconnect_tile(b.id)
    new = win.workers[b.id]
    assert new is not old and new.started
    assert old.stopped and old.frame_ready.slots == [] and old.status_changed.slots == []   # cannot overwrite the new one
    assert win.grid.tiles[b.id] is tile and list(win.grid.tiles) == order                   # same place in the grid
    assert tile.status == CameraStatus.UNKNOWN                                              # "connecting" again


def test_reconnect_can_switch_stream_and_keeps_it_otherwise(win):
    (a,) = open_cams(win, 1, protocol="dvrip")
    win._reconnect_tile(a.id, main=True)
    assert win.workers[a.id].force_main and win.grid.tiles[a.id].hd_btn.isChecked()
    win._reconnect_tile(a.id)                          # no choice: stays on the main stream
    assert win.workers[a.id].force_main
    win._reconnect_tile(a.id, main=False)
    assert not win.workers[a.id].force_main and not win.grid.tiles[a.id].hd_btn.isChecked()


def test_reconnect_retries_a_stream_that_was_marked_missing(win):
    (a,) = open_cams(win, 1, protocol="dvrip")
    NO_SUB.add((a.host, a.port, a.channel))
    win._reconnect_tile(a.id)
    assert (a.host, a.port, a.channel) not in NO_SUB


def test_new_details_go_to_the_tile_and_its_worker(win):
    (a,) = open_cams(win, 1)
    edited = CameraConfig(id=a.id, name="renamed", host="9.9.9.9", port=34567)
    win._reconnect_tile(a.id, cfg=edited)
    assert win.workers[a.id].cfg is edited and win.grid.tiles[a.id].cfg is edited


def test_reconnect_of_a_closed_tile_does_nothing(win):
    win._reconnect_tile("no-such-id")
    assert not FakeWorker.made
