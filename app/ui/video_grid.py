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
        self.focus_id: str | None = None      # when set, only this tile is shown (enlarged)

    def _columns_for(self, count: int) -> int:
        if count <= 1:
            return 1
        return math.ceil(math.sqrt(count))

    def set_focus(self, camera_id: str | None):
        self.focus_id = camera_id
        self._relayout()

    def _relayout(self):
        while self._grid.count():
            self._grid.takeAt(0)
        if self.focus_id in self.tiles:
            shown = [self.tiles[self.focus_id]]
        else:
            self.focus_id = None
            shown = list(self.tiles.values())
        for t in self.tiles.values():
            t.setVisible(any(t is x for x in shown))
        cols = self._columns_for(len(shown))
        rows = max(1, math.ceil(len(shown) / cols))
        # forget the stretch / minimum sizes of the previous arrangement, otherwise rows and
        # columns left over from the old layout keep pulling space away from the new ones
        for i in range(max(self._grid.rowCount(), rows) + 1):
            self._grid.setRowStretch(i, 0)
            self._grid.setRowMinimumHeight(i, 0)
        for i in range(max(self._grid.columnCount(), cols) + 1):
            self._grid.setColumnStretch(i, 0)
            self._grid.setColumnMinimumWidth(i, 0)
        for i, tile in enumerate(shown):
            r, c = divmod(i, cols)
            self._grid.addWidget(tile, r, c)
        for r in range(rows):                  # equal share for every row and column
            self._grid.setRowStretch(r, 1)
        for c in range(cols):
            self._grid.setColumnStretch(c, 1)

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
