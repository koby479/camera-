from __future__ import annotations

import os
import sys
from collections import deque

from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QMainWindow, QSplitter, QMessageBox
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QActionGroup, QKeyEvent, QKeySequence, QShortcut

from app.core.camera import CameraConfig, CameraWorker, CameraStatus, NO_SUB
from app.core.store import load_all, save_all, DEFAULT_MAX_TILES
from app.ui.sidebar import Sidebar
from app.ui.video_grid import VideoGrid
from app.ui.video_tile import VideoTile
from app.ui.add_dialog import AddDeviceDialog
from app.ui.scan_dialog import ScanDialog
from app.ui.nvr_probe_worker import NvrProbeWorker
from app.core.audio import AudioPlayer
from app.core import decoders, names, settings
from app.ui.playback_dialog import PlaybackDialog
from app.ui.event_log import AlarmHub, EventLogDock
from app.ui.ptz_panel import PtzPanel


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("צופה מצלמות אוניברסלי")
        self.resize(1400, 900)

        self.nvrs, self.singles = load_all()
        self.workers: dict[str, CameraWorker] = {}
        self.nvr_items = {}   # nvr id -> tree item, for populating channels later
        self.single_items = {}  # single-camera id -> tree item, so edits can update the label in place
        self.nvr_probe_workers: dict[str, NvrProbeWorker] = {}   # keep refs alive while running
        self.audio_player = AudioPlayer()
        self.audio_camera_id: str | None = None   # only one camera is audible at a time
        self.playback_dialogs: list[PlaybackDialog] = []
        self._stopping: list[CameraWorker] = []            # stopped workers whose thread is still winding down
        self._was_maximized = False
        self._grid_full = False                            # whole window full screen, all cameras visible
        self._session_hidden: set[str] = set()             # removed from view this session only; never saved
        self._connect_all_pending: dict[str, bool] = {}   # nvr id -> main stream? (waiting for the channel list)
        self._connect_queue: deque = deque()               # (CameraConfig, main) opened one at a time
        self._connect_timer = QTimer(self)                 # staggered so the PC and the NVR are not hit at once
        self._connect_timer.setInterval(400)
        self._connect_timer.timeout.connect(self._connect_next)

        self.sidebar = Sidebar()
        self.grid = VideoGrid()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.grid)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 1100])
        self.setCentralWidget(splitter)

        self.sidebar.add_nvr_requested.connect(self.on_add_nvr)
        self.sidebar.add_single_requested.connect(self.on_add_single)
        self.sidebar.scan_network_requested.connect(self.on_scan_network)
        self.sidebar.expand_nvr_requested.connect(self.on_expand_nvr)
        self.sidebar.camera_toggle_requested.connect(self.on_toggle_camera)
        self.sidebar.remove_device_requested.connect(self.on_remove_device)
        self.sidebar.edit_device_requested.connect(self.on_edit_device)
        self.sidebar.renamed.connect(self._on_renamed)
        self.sidebar.playback_requested.connect(self.on_playback)
        self.sidebar.connect_all_requested.connect(self.on_connect_all)
        self.sidebar.grid_fullscreen_requested.connect(self._toggle_grid_fullscreen)
        QShortcut(QKeySequence("F11"), self, activated=self._on_f11)
        self.sidebar.disconnect_all_requested.connect(self._close_all_tiles)

        self._build_decoder_menu()
        self._ptz_panels: dict[str, PtzPanel] = {}
        self.alarm_hub = AlarmHub(self)
        self.event_dock = EventLogDock(self.alarm_hub, self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.event_dock)
        self.event_dock.setMaximumHeight(220)
        self.menuBar().addAction(self.event_dock.toggleViewAction())

        for nvr in self.nvrs:
            item = self.sidebar.add_nvr_node(nvr)
            self.nvr_items[nvr.id] = item
        for cam in self.singles:
            item = self.sidebar.add_single_node(cam)
            self.single_items[cam.id] = item

    # ---- video decoder menu ----------------------------------------
    def _build_decoder_menu(self):
        menu = self.menuBar().addMenu("מפענח וידאו")
        group = QActionGroup(self)
        group.setExclusive(True)
        current = decoders.configured()
        for key, label in decoders.CHOICES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(key == current)
            act.triggered.connect(lambda _checked=False, k=key: self._set_decoder(k))
            group.addAction(act)
            menu.addAction(act)
        menu.addSeparator()
        menu.addAction("בחר קובץ ffmpeg.exe...", self._pick_ffmpeg)
        menu.addAction("בדוק איזה מפענח עובד במחשב הזה", self._test_decoders)
        self._decoder_test = None            # (thread, result dict) while a test runs
        self._decoder_timer = QTimer(self)
        self._decoder_timer.setInterval(200)
        self._decoder_timer.timeout.connect(self._decoder_test_poll)

    def _set_decoder(self, key: str):
        settings.put("decoder", key)
        decoders.reset()
        QMessageBox.information(self, "מפענח וידאו",
                                "הבחירה נשמרה. היא תחול על כל חיבור חדש של מצלמה "
                                "(נתק את המצלמה וחבר אותה שוב, או הפעל את התוכנה מחדש).")

    def _pick_ffmpeg(self):
        path, _ = QFileDialog.getOpenFileName(self, "בחר את ffmpeg.exe", "", "ffmpeg (ffmpeg.exe ffmpeg);;כל הקבצים (*)")
        if path:
            settings.put("ffmpeg_path", path)
            decoders.reset()

    def _test_decoders(self):
        if self._decoder_test:
            return
        result: dict = {}
        import threading
        t = threading.Thread(target=lambda: result.update(decoders.self_test()), daemon=True)
        self._decoder_test = (t, result)
        t.start()
        self.statusBar().showMessage("בודק מפענחים...")
        self._decoder_timer.start()

    def _decoder_test_poll(self):
        if not self._decoder_test or self._decoder_test[0].is_alive():
            return
        self._decoder_timer.stop()
        _t, result = self._decoder_test
        self._decoder_test = None
        self.statusBar().clearMessage()
        lines = []
        for name in decoders.BACKENDS:
            ok, text = result.get(name, (False, "לא נבדק"))
            lines.append(f"{'✔' if ok else '✘'}  {decoders.LABELS[name]}: {text}")
        QMessageBox.information(self, "תוצאות בדיקת המפענחים", "\n".join(lines))

    # ---------------------------------------------------------------
    def on_add_nvr(self):
        dlg = AddDeviceDialog(is_nvr=True, parent=self)
        if dlg.exec():
            cfg = dlg.to_config()
            self.nvrs.append(cfg)
            item = self.sidebar.add_nvr_node(cfg)
            self.nvr_items[cfg.id] = item
            save_all(self.nvrs, self.singles)

    def on_add_single(self):
        dlg = AddDeviceDialog(is_nvr=False, parent=self)
        if dlg.exec():
            cfg = dlg.to_config()
            self.singles.append(cfg)
            item = self.sidebar.add_single_node(cfg)
            self.single_items[cfg.id] = item
            save_all(self.nvrs, self.singles)

    def on_scan_network(self):
        dlg = ScanDialog(self)
        if dlg.exec() and dlg.selected_cfg:
            cfg = dlg.selected_cfg
            if dlg.selected_kind == "nvr":
                self.nvrs.append(cfg)
                item = self.sidebar.add_nvr_node(cfg)
                self.nvr_items[cfg.id] = item
            else:
                self.singles.append(cfg)
                item = self.sidebar.add_single_node(cfg)
                self.single_items[cfg.id] = item
            save_all(self.nvrs, self.singles)

    def on_expand_nvr(self, nvr_cfg: CameraConfig):
        item = self.nvr_items.get(nvr_cfg.id)
        if item is None:
            return
        if nvr_cfg.id in self.nvr_probe_workers:
            return  # already probing this NVR, don't stack up duplicate requests

        item.setText(0, f"⏳ {nvr_cfg.name} — טוען ערוצים...")

        worker = NvrProbeWorker(nvr_cfg)
        worker.succeeded.connect(self._on_nvr_probe_ok)
        worker.failed.connect(self._on_nvr_probe_failed)
        worker.finished.connect(lambda: self.nvr_probe_workers.pop(nvr_cfg.id, None))
        self.nvr_probe_workers[nvr_cfg.id] = worker
        worker.start()

    def _on_nvr_probe_ok(self, nvr_cfg: CameraConfig, channels: list[CameraConfig]):
        item = self.nvr_items.get(nvr_cfg.id)
        if item is None:
            return
        item.setText(0, f"🖥 {nvr_cfg.name} ({nvr_cfg.host})")
        for ch in channels:
            saved = names.get_name(nvr_cfg.id, ch.channel)
            if saved:
                ch.name = saved
        pending_main = self._connect_all_pending.pop(nvr_cfg.id, None)
        if not channels:
            QMessageBox.information(
                self, "אין ערוצים",
                "ה-NVR הגיב אך לא נמצאו ערוצים/פרופילים דרך ONVIF."
            )
            return
        self.sidebar.set_nvr_channels(item, channels)
        if pending_main is not None:
            self._start_channels(channels, pending_main)

    def _on_nvr_probe_failed(self, nvr_cfg: CameraConfig, error: str):
        self._connect_all_pending.pop(nvr_cfg.id, None)
        item = self.nvr_items.get(nvr_cfg.id)
        if item is not None:
            item.setText(0, f"🖥 {nvr_cfg.name} ({nvr_cfg.host})")
        QMessageBox.warning(self, "שגיאת חיבור ל-NVR", error)

    # ---- connect to every channel of an NVR with one click ----------------
    def on_connect_all(self, nvr_cfg: CameraConfig, main: bool):
        if main:
            resp = QMessageBox.question(
                self, "זרם ראשי בכל הערוצים",
                "הזרם הראשי הוא באיכות מלאה (עד 4K) ופענוח שלו בכל הערוצים יכביד מאוד על המחשב. "
                "מומלץ זרם משני. להמשיך בכל זאת?",
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
        item = self.nvr_items.get(nvr_cfg.id)
        channels = []
        if item is not None:
            for i in range(item.childCount()):
                ch = item.child(i).data(0, Qt.ItemDataRole.UserRole + 1)
                if isinstance(ch, CameraConfig):
                    channels.append(ch)
        if channels:
            self._start_channels(channels, main)
        else:                                   # channel list not loaded yet: load it, then connect
            self._connect_all_pending[nvr_cfg.id] = main
            self.on_expand_nvr(nvr_cfg)

    def _start_channels(self, channels: list[CameraConfig], main: bool):
        for ch in channels:
            if not self.grid.has_tile(ch.id) and ch.id not in self._session_hidden:
                self._connect_queue.append((ch, main))
        if self._connect_queue and not self._connect_timer.isActive():
            self._connect_next()
            self._connect_timer.start()

    def _connect_next(self):
        while self._connect_queue:
            cfg, main = self._connect_queue.popleft()
            if not self.grid.has_tile(cfg.id):       # toggling an open channel would close it
                self.on_toggle_camera(cfg, main)
                return
        self._connect_timer.stop()

    def on_toggle_camera(self, cfg: CameraConfig, main: bool = False):
        self._session_hidden.discard(cfg.id)      # asking for this camera explicitly brings it back
        if self.grid.has_tile(cfg.id):
            self.remove_camera_tile(cfg.id)
            return

        if len(self.grid.tiles) >= DEFAULT_MAX_TILES:
            resp = QMessageBox.question(
                self, "יותר מ-64 מצלמות",
                "הגעת לברירת המחדל של 64 מצלמות בו-זמנית. זה ידרוש יותר CPU/רוחב-פס. להמשיך בכל זאת?",
            )
            if resp != QMessageBox.StandardButton.Yes:
                return

        tile = VideoTile(cfg)
        tile.grid_full = self._grid_full
        tile.audio_toggled.connect(self._on_audio_toggled)
        tile.activated.connect(self._on_tile_activated)
        tile.hd_toggled.connect(self._on_hd_toggled)
        tile.ptz_requested.connect(self._open_ptz)
        tile.playback_requested.connect(self._on_tile_playback)
        tile.close_requested.connect(self._close_tile)
        tile.close_all_requested.connect(self._close_all_tiles)
        tile.hide_requested.connect(self._hide_tile)
        tile.hide_dead_requested.connect(self._hide_dead_tiles)
        tile.grid_fullscreen_requested.connect(self._toggle_grid_fullscreen)
        tile.reconnect_requested.connect(self._reconnect_tile)
        tile.edit_connection_requested.connect(self._on_tile_edit_connection)
        self.grid.add_tile(tile)
        self._start_worker(cfg, tile, main)

    def _start_worker(self, cfg: CameraConfig, tile: VideoTile, main: bool = False):
        worker = CameraWorker(cfg)
        if cfg.protocol == "dvrip":                # live events (motion, alarms) of this recorder, one listener each
            self.alarm_hub.ensure(cfg)
        worker.frame_ready.connect(self._on_frame)
        worker.status_changed.connect(self._on_status)
        worker.path_resolved.connect(self._on_path_resolved)
        worker.audio_ready.connect(self._on_audio)
        if main and cfg.protocol == "dvrip":     # open straight on the main stream, no restart later
            worker.force_main = True
            worker._update_width()
            tile.hd_btn.blockSignals(True)
            tile.hd_btn.setChecked(True)
            tile.hd_btn.blockSignals(False)
        self.workers[cfg.id] = worker
        worker.start()

    def remove_camera_tile(self, camera_id: str):
        if self.audio_camera_id == camera_id:
            self.audio_camera_id = None
            self.audio_player.stop()
        worker = self.workers.pop(camera_id, None)
        if worker:
            worker.stop()
            self._stopping = [w for w in self._stopping if w.isRunning()]
            if worker.isRunning():             # keep a reference: dropping a running QThread crashes the program
                self._stopping.append(worker)
        self.grid.remove_tile(camera_id)

    def on_edit_device(self, cfg: CameraConfig, kind: str):
        dlg = AddDeviceDialog(is_nvr=(kind == "nvr"), parent=self, existing_cfg=cfg)
        if not dlg.exec():
            return
        new_cfg = dlg.to_config()   # same id as cfg, just updated fields

        if kind == "nvr":
            for i, n in enumerate(self.nvrs):
                if n.id == new_cfg.id:
                    self.nvrs[i] = new_cfg
                    break
            item = self.nvr_items.get(new_cfg.id)
            if item:
                self.sidebar.refresh_nvr_node(item, new_cfg)
        else:
            for i, s in enumerate(self.singles):
                if s.id == new_cfg.id:
                    self.singles[i] = new_cfg
                    break
            item = self.single_items.get(new_cfg.id)
            if item:
                self.sidebar.refresh_single_node(item, new_cfg)

        # if this camera is currently live in the grid, restart its worker
        # with the new connection details instead of requiring a full app restart
        if new_cfg.id in self.workers:
            self._reconnect_tile(new_cfg.id, cfg=new_cfg)       # same place in the grid, new details
        if kind == "nvr":
            self._propagate_nvr_connection(new_cfg)

        save_all(self.nvrs, self.singles)

    # ---- reconnect / change connection (right-click menu of a tile) ------------
    @staticmethod
    def _detach_worker(worker: CameraWorker):
        """Stop listening to a worker that is being replaced, so its last messages cannot overwrite the new one's."""
        for sig in (worker.frame_ready, worker.status_changed, worker.path_resolved, worker.audio_ready):
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass

    def _reconnect_tile(self, camera_id: str, main: bool | None = None, cfg: CameraConfig | None = None):
        """Drop the connection of one tile and make a new one: same tile, same place in the grid.
        main: None = keep the current stream, True / False = switch to the main / sub stream.
        cfg: new connection details (after the edit dialog)."""
        tile = self.grid.tiles.get(camera_id)
        if tile is None:
            return
        if cfg is not None:
            tile.cfg = cfg
            tile.set_title(cfg.name)
        cfg = tile.cfg
        old = self.workers.pop(camera_id, None)
        was_main = bool(old.force_main) if old is not None else False
        if old is not None:
            self._detach_worker(old)
            old.request_stop()                     # do not wait: the window stays responsive
            self._stopping = [w for w in self._stopping if w.isRunning()]
            self._stopping.append(old)             # kept alive until its thread has really ended
        if self.audio_camera_id == camera_id:
            self.audio_camera_id = None
            self.audio_player.stop()
            tile.set_audio_on(False)
        use_main = was_main if main is None else main
        NO_SUB.discard((cfg.host, cfg.port, cfg.channel))   # forget "this camera has no sub-stream": try again
        tile.begin_reconnect(use_main)
        self._start_worker(cfg, tile, use_main)

    def _on_tile_edit_connection(self, cfg: CameraConfig):
        if cfg.parent_nvr_id:                      # a channel: the connection details belong to its NVR
            owner = next((n for n in self.nvrs if n.id == cfg.parent_nvr_id), None)
            kind = "nvr"
        else:
            owner = next((s for s in self.singles if s.id == cfg.id), None)
            kind = "single"
        if owner is None:
            QMessageBox.information(self, "שינוי חיבור", "המכשיר של המצלמה הזאת לא נמצא ברשימה.")
            return
        self._leave_fullscreen()
        self.on_edit_device(owner, kind)

    def _propagate_nvr_connection(self, nvr: CameraConfig):
        """After the NVR's details were edited: its channels (already listed in the sidebar) must use them
        too, and the ones open in the grid reconnect with the new details."""
        item = self.nvr_items.get(nvr.id)
        if item is None:
            return
        for i in range(item.childCount()):
            ch = item.child(i).data(0, Qt.ItemDataRole.UserRole + 1)
            if not isinstance(ch, CameraConfig):
                continue
            ch.username, ch.password = nvr.username, nvr.password
            if nvr.protocol == "dvrip":            # ONVIF channels keep the addresses the NVR itself reported
                ch.host, ch.port = nvr.host, nvr.port
            if ch.id in self.workers:
                self._reconnect_tile(ch.id)

    def on_remove_device(self, cfg: CameraConfig, kind: str):
        self.remove_camera_tile(cfg.id)
        if kind == "nvr":
            self.nvrs = [n for n in self.nvrs if n.id != cfg.id]
            item = self.nvr_items.pop(cfg.id, None)
        else:
            self.singles = [s for s in self.singles if s.id != cfg.id]
            item = self.single_items.pop(cfg.id, None)
        if item is not None and item.parent() is not None:
            item.parent().removeChild(item)
        save_all(self.nvrs, self.singles)

    # ---------------------------------------------------------------
    def _on_frame(self, camera_id: str, frame):
        tile = self.grid.tiles.get(camera_id)
        if tile:
            tile.update_frame(frame)
        w = self.workers.get(camera_id)
        if w:
            w.pending = False        # GUI is done with this frame -> worker may send the next one

    def _on_tile_activated(self, camera_id: str):
        """Double-click a tile: enlarge it; again: back to the grid. Enlarging does NOT switch to
        the heavy main stream any more -- that is the tile's own 'HD' button."""
        entering = self.grid.focus_id != camera_id
        self.grid.set_focus(camera_id if entering else None)
        for cid, t in self.grid.tiles.items():
            t.fullscreen = entering and cid == camera_id
        if entering:                       # real full screen: hide the sidebar and the title bar
            if not self._grid_full:        # already full screen with the grid? keep the saved state
                self._was_maximized = self.isMaximized()
            self.sidebar.hide()
            self.showFullScreen()
        elif not self._grid_full:          # back to the grid; stay full screen if that is how we came in
            self.sidebar.show()
            self.showMaximized() if self._was_maximized else self.showNormal()
        for cid, w in self.workers.items():
            w.render = (not entering) or cid == camera_id
            if cid == camera_id:
                w.set_focused(entering)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            if self.grid.focus_id:
                self._on_tile_activated(self.grid.focus_id)  # Esc leaves the single-camera view first
                return
            if self._grid_full:
                self._toggle_grid_fullscreen()
                return
        super().keyPressEvent(event)

    def _on_f11(self):
        if self.grid.focus_id:                               # from one enlarged camera: back to the grid
            self._on_tile_activated(self.grid.focus_id)
        else:
            self._toggle_grid_fullscreen()

    def _toggle_grid_fullscreen(self):
        """All cameras on the whole screen (sidebar and title bar hidden). Esc / F11 / the tile
        menu bring everything back."""
        if self.grid.focus_id:
            self._on_tile_activated(self.grid.focus_id)
        if self._grid_full:
            self._grid_full = False
            self.sidebar.show()
            self.showMaximized() if self._was_maximized else self.showNormal()
        else:
            self._was_maximized = self.isMaximized()
            self._grid_full = True
            self.sidebar.hide()
            self.showFullScreen()
        for tile in self.grid.tiles.values():
            tile.grid_full = self._grid_full

    def _leave_fullscreen(self):
        if self.grid.focus_id:
            self._on_tile_activated(self.grid.focus_id)

    def _close_tile(self, camera_id: str):
        self._leave_fullscreen()              # otherwise the window stays full screen with an empty grid
        self.remove_camera_tile(camera_id)

    BROKEN = (CameraStatus.OFFLINE, CameraStatus.DEAD, CameraStatus.AUTH_FAILED, CameraStatus.NO_CAMERA)

    def _hide_tile(self, camera_id: str):
        """Remove a broken camera from the grid. Only for this run: it is not deleted from the saved
        list, so the next start of the program shows every camera again."""
        self._session_hidden.add(camera_id)
        self._close_tile(camera_id)

    def _hide_dead_tiles(self):
        ids = [cid for cid, t in self.grid.tiles.items() if t.status in self.BROKEN]
        for cid in ids:
            self._session_hidden.add(cid)
        self._connect_queue = deque(x for x in self._connect_queue if x[0].id not in self._session_hidden)
        if ids:
            self._leave_fullscreen()
        for cid in ids:
            self.remove_camera_tile(cid)

    def _close_all_tiles(self):
        self._connect_queue.clear()
        self._connect_all_pending.clear()
        self._connect_timer.stop()
        self._leave_fullscreen()
        for w in self.workers.values():        # signal everyone first, then wait: the waits overlap
            w.request_stop()
        for cid in list(self.workers):
            self.remove_camera_tile(cid)

    def _on_tile_playback(self, cfg: CameraConfig):
        self._leave_fullscreen()
        self.on_playback(cfg, "nvr_channel" if cfg.parent_nvr_id else "single")

    def _open_ptz(self, cfg: CameraConfig):
        panel = self._ptz_panels.get(cfg.id)
        if panel is None:
            panel = self._ptz_panels[cfg.id] = PtzPanel(cfg, self)
        panel.show()
        panel.raise_()
        panel.activateWindow()

    def _on_hd_toggled(self, camera_id: str, on: bool):
        w = self.workers.get(camera_id)
        if w and w.cfg.protocol == "dvrip":
            w.set_quality(main=on)

    def _on_renamed(self, cfg: CameraConfig, kind: str):
        if kind == "nvr_channel":
            names.set_name(cfg.parent_nvr_id, cfg.channel, cfg.name)
        else:
            save_all(self.nvrs, self.singles)
        tile = self.grid.tiles.get(cfg.id)
        if tile:
            tile.set_title(cfg.name)

    def on_playback(self, cfg: CameraConfig, kind: str):
        if kind == "nvr_channel":
            nvr = next((n for n in self.nvrs if n.id == cfg.parent_nvr_id), None)
            channel = cfg.channel
        else:
            nvr, channel = cfg, cfg.channel
        if nvr is None or nvr.protocol != "dvrip":
            QMessageBox.information(self, "הקלטות", "צפייה בהקלטות זמינה כרגע רק ל-NVR מסוג XM / Provision.")
            return
        item = self.nvr_items.get(nvr.id)
        channels = []
        if item is not None:
            for i in range(item.childCount()):
                ch = item.child(i).data(0, Qt.ItemDataRole.UserRole + 1)
                if isinstance(ch, CameraConfig):
                    channels.append((ch.channel, ch.name))
        if not channels:
            channels = [(i, f"ערוץ {i + 1}") for i in range(16)]
            channels = [(i, names.get_name(nvr.id, i) or label) for i, label in channels]
        dlg = PlaybackDialog(nvr, channels, channel, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.finished.connect(lambda _r, d=dlg: self.playback_dialogs.remove(d) if d in self.playback_dialogs else None)
        self.playback_dialogs.append(dlg)
        dlg.show()

    def _on_audio_toggled(self, camera_id: str, on: bool):
        if on:
            prev = self.audio_camera_id
            if prev and prev != camera_id:
                if prev in self.workers:
                    self.workers[prev].audio_enabled = False
                if prev in self.grid.tiles:
                    self.grid.tiles[prev].set_audio_on(False)
            self.audio_player.stop()
            self.audio_camera_id = camera_id
            if camera_id in self.workers:
                self.workers[camera_id].audio_enabled = True
        elif self.audio_camera_id == camera_id:
            self.audio_camera_id = None
            if camera_id in self.workers:
                self.workers[camera_id].audio_enabled = False
            self.audio_player.stop()

    def _on_audio(self, camera_id: str, pcm: bytes, rate: int):
        if camera_id == self.audio_camera_id:
            self.audio_player.write(pcm, rate)

    def _on_status(self, camera_id: str, status: CameraStatus):
        tile = self.grid.tiles.get(camera_id)
        if tile:
            tile.set_status(status)

    def _on_path_resolved(self, camera_id: str, path: str):
        """Called on the GUI thread once auto-detection finds a working RTSP
        path -- safe to write to the shared CameraConfig and persist here,
        unlike doing it from inside the worker thread."""
        for cfg in (*self.nvrs, *self.singles):
            if cfg.id == camera_id:
                cfg.rtsp_path = path
                save_all(self.nvrs, self.singles)
                break

    def closeEvent(self, event):
        workers = list(self.workers.values()) + self._stopping
        for worker in workers:                 # signal everyone first, then wait: closing 16 cameras was 16 x 2 s
            worker.request_stop()
        for worker in workers:
            worker.wait(2000)
        self.audio_player.stop()
        for panel in self._ptz_panels.values():
            panel.close()                      # sends "stop" so no camera keeps moving
        self.alarm_hub.stop()
        save_all(self.nvrs, self.singles)
        super().closeEvent(event)


def _log_uncaught(exc_type, exc, tb):
    """An error inside one window must not take the whole program down: Qt aborts on an unhandled exception
    in a slot unless a custom hook is installed. Write it to app.log and carry on."""
    import traceback
    print("[error] uncaught exception:", file=sys.stderr)
    traceback.print_exception(exc_type, exc, tb, file=sys.stderr)


def main():
    if sys.stderr is None:                 # windowed EXE: no console, so write the log file ourselves
        from app.core import applog
        applog.setup()
    sys.excepthook = _log_uncaught
    from app import __version__
    print(f"[app] Universal Cam Viewer {__version__} started (python {sys.version.split()[0]}, "
          f"frozen={getattr(sys, 'frozen', False)})", file=sys.stderr)
    app = QApplication(sys.argv)
    from app.ui.theme import DARK_THEME
    app.setStyleSheet(DARK_THEME)
    win = MainWindow()
    win.show()
    code = app.exec()
    # A thread that is still winding down (an NVR read that has not timed out yet) would make Qt abort with
    # "QThread: Destroyed while thread is still running" while Python tears the objects down: leave directly.
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(code)


if __name__ == "__main__":
    main()
