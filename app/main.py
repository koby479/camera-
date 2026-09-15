from __future__ import annotations

import sys

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QMessageBox
)
from PyQt6.QtCore import Qt

from app.core.camera import CameraConfig, CameraWorker, CameraStatus
from app.core.store import load_all, save_all, DEFAULT_MAX_TILES
from app.core import discovery
from app.ui.sidebar import Sidebar
from app.ui.video_grid import VideoGrid
from app.ui.video_tile import VideoTile
from app.ui.add_dialog import AddDeviceDialog


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("צופה מצלמות אוניברסלי")
        self.resize(1400, 900)

        self.nvrs, self.singles = load_all()
        self.workers: dict[str, CameraWorker] = {}
        self.nvr_items = {}   # nvr id -> tree item, for populating channels later

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
        self.sidebar.expand_nvr_requested.connect(self.on_expand_nvr)
        self.sidebar.camera_toggle_requested.connect(self.on_toggle_camera)
        self.sidebar.remove_device_requested.connect(self.on_remove_device)

        for nvr in self.nvrs:
            item = self.sidebar.add_nvr_node(nvr)
            self.nvr_items[nvr.id] = item
        for cam in self.singles:
            self.sidebar.add_single_node(cam)

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
            self.sidebar.add_single_node(cfg)
            save_all(self.nvrs, self.singles)

    def on_expand_nvr(self, nvr_cfg: CameraConfig):
        item = self.nvr_items.get(nvr_cfg.id)
        if item is None:
            return
        try:
            channels = discovery.enumerate_nvr_channels(nvr_cfg)
        except RuntimeError as exc:
            QMessageBox.warning(self, "שגיאת חיבור ל-NVR", str(exc))
            return
        if not channels:
            QMessageBox.information(
                self, "אין ערוצים",
                "ה-NVR הגיב אך לא נמצאו ערוצים/פרופילים דרך ONVIF."
            )
            return
        self.sidebar.set_nvr_channels(item, channels)

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
        self.workers[cfg.id] = worker
        worker.start()

    def remove_camera_tile(self, camera_id: str):
        worker = self.workers.pop(camera_id, None)
        if worker:
            worker.stop()
        self.grid.remove_tile(camera_id)

    def on_remove_device(self, cfg: CameraConfig, kind: str):
        self.remove_camera_tile(cfg.id)
        if kind == "nvr":
            self.nvrs = [n for n in self.nvrs if n.id != cfg.id]
        else:
            self.singles = [s for s in self.singles if s.id != cfg.id]
        save_all(self.nvrs, self.singles)
        QMessageBox.information(self, "הוסר", "יש להפעיל מחדש את התוכנה כדי לרענן את העץ.")

    # ---------------------------------------------------------------
    def _on_frame(self, camera_id: str, frame):
        tile = self.grid.tiles.get(camera_id)
        if tile:
            tile.update_frame(frame)

    def _on_status(self, camera_id: str, status: CameraStatus):
        tile = self.grid.tiles.get(camera_id)
        if tile:
            tile.set_status(status)

    def closeEvent(self, event):
        for worker in self.workers.values():
            worker.stop()
        save_all(self.nvrs, self.singles)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
