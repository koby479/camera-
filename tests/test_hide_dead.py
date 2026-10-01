import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.core.camera import CameraConfig, CameraStatus  # noqa: E402


@pytest.fixture
def win(monkeypatch):
    from app import main as m
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(m, "load_all", lambda: ([], []))
    w = m.MainWindow()
    yield w
    w.close()
    del app


def add(win, name, status):
    from app.ui.video_tile import VideoTile
    cfg = CameraConfig(name=name)
    t = VideoTile(cfg)
    t.set_status(status)
    win.grid.add_tile(t)
    return cfg


def test_hide_dead_keeps_working_cameras(win):
    ok = add(win, "ok", CameraStatus.ACTIVE)
    bad = add(win, "bad", CameraStatus.OFFLINE)
    auth = add(win, "auth", CameraStatus.AUTH_FAILED)
    wait = add(win, "connecting", CameraStatus.UNKNOWN)
    win._hide_dead_tiles()
    assert set(win.grid.tiles) == {ok.id, wait.id}
    assert win._session_hidden == {bad.id, auth.id}


def test_hidden_camera_skipped_by_connect_all_but_not_by_explicit_open(win):
    bad = add(win, "bad", CameraStatus.DEAD)
    win._hide_tile(bad.id)
    assert not win.grid.has_tile(bad.id)
    win._start_channels([bad], False)
    assert not win._connect_queue                      # "connect all" leaves it out
    assert bad.id in win._session_hidden


def test_nothing_persisted(win, monkeypatch):
    saved = []
    from app import main as m
    monkeypatch.setattr(m, "save_all", lambda *a: saved.append(a))
    bad = add(win, "bad", CameraStatus.OFFLINE)
    win._hide_dead_tiles()
    assert not saved                                   # removal never touches the saved camera list
