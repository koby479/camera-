from __future__ import annotations

import math

from PyQt6.QtWidgets import QWidget, QGridLayout, QScrollArea, QVBoxLayout

from app.ui.video_tile import VideoTile


class VideoGrid(QScrollArea):
    """Auto-arranging grid. No hard limit on tile count -- it just keeps
    adding rows/columns as cameras are toggled on. 64 is only the default
    *window* size the user asked for, not an enforced ceiling."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setSpacing(2)
        self.setWidget(self._container)
        self.tiles: dict[str, VideoTile] = {}

    def _columns_for(self, count: int) -> int:
        if count <= 1:
            return 1
        return math.ceil(math.sqrt(count))

    def _relayout(self):
        while self._grid.count():
            self._grid.takeAt(0)
        cols = self._columns_for(len(self.tiles))
        for i, tile in enumerate(self.tiles.values()):
            r, c = divmod(i, cols)
            self._grid.addWidget(tile, r, c)

    def add_tile(self, tile: VideoTile):
        self.tiles[tile.cfg.id] = tile
        self._relayout()

    def remove_tile(self, camera_id: str):
        tile = self.tiles.pop(camera_id, None)
        if tile:
            tile.setParent(None)
            tile.deleteLater()
        self._relayout()

    def has_tile(self, camera_id: str) -> bool:
        return camera_id in self.tiles
