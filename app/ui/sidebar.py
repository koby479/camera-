from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton,
    QHBoxLayout, QMenu
)

from app.core.camera import CameraConfig

ROLE_KIND = Qt.ItemDataRole.UserRole          # "nvr" | "nvr_channel" | "single"
ROLE_CFG = Qt.ItemDataRole.UserRole + 1        # CameraConfig


class Sidebar(QWidget):
    add_nvr_requested = pyqtSignal()
    add_single_requested = pyqtSignal()
    scan_network_requested = pyqtSignal()
    expand_nvr_requested = pyqtSignal(object)      # CameraConfig of the NVR
    camera_toggle_requested = pyqtSignal(object)    # CameraConfig, add/remove from grid
    remove_device_requested = pyqtSignal(object, str)  # CameraConfig, kind

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)

        self.nvr_root = QTreeWidgetItem(["NVR / רשמים"])
        self.nvr_root.setFlags(self.nvr_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.single_root = QTreeWidgetItem(["מצלמות בודדות"])
        self.single_root.setFlags(self.single_root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.tree.addTopLevelItem(self.nvr_root)
        self.tree.addTopLevelItem(self.single_root)
        self.tree.expandAll()

        btn_row = QHBoxLayout()
        add_nvr_btn = QPushButton("+ הוסף NVR")
        add_nvr_btn.clicked.connect(self.add_nvr_requested.emit)
        add_single_btn = QPushButton("+ הוסף מצלמה")
        add_single_btn.clicked.connect(self.add_single_requested.emit)
        btn_row.addWidget(add_nvr_btn)
        btn_row.addWidget(add_single_btn)

        scan_btn = QPushButton("🔍 סרוק רשת")
        scan_btn.clicked.connect(self.scan_network_requested.emit)

        layout = QVBoxLayout(self)
        layout.addLayout(btn_row)
        layout.addWidget(scan_btn)
        layout.addWidget(self.tree)

    # ---- population -------------------------------------------------
    def add_nvr_node(self, cfg: CameraConfig) -> QTreeWidgetItem:
        item = QTreeWidgetItem([f"🖥 {cfg.name} ({cfg.host})"])
        item.setData(0, ROLE_KIND, "nvr")
        item.setData(0, ROLE_CFG, cfg)
        # placeholder child so the arrow to expand is shown before we've probed it
        placeholder = QTreeWidgetItem(["לחץ כדי לטעון ערוצים..."])
        item.addChild(placeholder)
        self.nvr_root.addChild(item)
        return item

    def set_nvr_channels(self, nvr_item: QTreeWidgetItem, channels: list[CameraConfig]):
        nvr_item.takeChildren()
        for ch in channels:
            child = QTreeWidgetItem([f"📷 {ch.name}"])
            child.setData(0, ROLE_KIND, "nvr_channel")
            child.setData(0, ROLE_CFG, ch)
            nvr_item.addChild(child)
        nvr_item.setExpanded(True)

    def add_single_node(self, cfg: CameraConfig) -> QTreeWidgetItem:
        item = QTreeWidgetItem([f"📷 {cfg.name} ({cfg.host})"])
        item.setData(0, ROLE_KIND, "single")
        item.setData(0, ROLE_CFG, cfg)
        self.single_root.addChild(item)
        return item

    # ---- interaction --------------------------------------------------
    def _on_double_click(self, item: QTreeWidgetItem, _col: int):
        kind = item.data(0, ROLE_KIND)
        cfg = item.data(0, ROLE_CFG)
        if kind == "nvr":
            self.expand_nvr_requested.emit(cfg)
        elif kind in ("nvr_channel", "single"):
            self.camera_toggle_requested.emit(cfg)

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        kind = item.data(0, ROLE_KIND)
        cfg = item.data(0, ROLE_CFG)
        if kind not in ("nvr", "single"):
            return
        menu = QMenu(self)
        remove_action = menu.addAction("הסר")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen == remove_action:
            self.remove_device_requested.emit(cfg, kind)
