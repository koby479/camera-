from __future__ import annotations

import sys

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QMessageBox
)
from PyQt6.QtCore import Qt

from app.core.camera import CameraConfig, CameraWorker, CameraStatus
from app.core.store import load_all, save_all, DEFAULT_MAX_TILES
from app.ui.sidebar import Sidebar
from app.ui.video_grid import VideoGrid
from app.ui.video_tile import VideoTile
from app.ui.add_dialog import AddDeviceDialog
from app.ui.scan_dialog import ScanDialog
from app.ui.nvr_probe_worker import NvrProbeWorker


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

        for nvr in self.nvrs:
            item = self.sidebar.add_nvr_node(nvr)
            self.nvr_items[nvr.id] = item
        for cam in self.singles:
            item = self.sidebar.add_single_node(cam)
            self.single_items[cam.id] = item

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
        if not channels:
            QMessageBox.information(
                self, "אין ערוצים",
                "ה-NVR הגיב אך לא נמצאו ערוצים/פרופילים דרך ONVIF."
            )
            return
        self.sidebar.set_nvr_channels(item, channels)

    def _on_nvr_probe_failed(self, nvr_cfg: CameraConfig, error: str):
        item = self.nvr_items.get(nvr_cfg.id)
        if item is not None:
            item.setText(0, f"🖥 {nvr_cfg.name} ({nvr_cfg.host})")
        QMessageBox.warning(self, "שגיאת חיבור ל-NVR", error)

    def on_toggle_camera(self, cfg: CameraConfig):
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
        self.grid.add_tile(tile)

        worker = CameraWorker(cfg)
        worker.frame_ready.connect(self._on_frame)
        worker.status_changed.connect(self._on_status)
        worker.path_resolved.connect(self._on_path_resolved)
        self.workers[cfg.id] = worker
        worker.start()

    def remove_camera_tile(self, camera_id: str):
        worker = self.workers.pop(camera_id, None)
        if worker:
            worker.stop()
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
            self.remove_camera_tile(new_cfg.id)
            self.on_toggle_camera(new_cfg)

        save_all(self.nvrs, self.singles)

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
        for worker in self.workers.values():
            worker.stop()
        save_all(self.nvrs, self.singles)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    from app.ui.theme import DARK_THEME
    app.setStyleSheet(DARK_THEME)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
