from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem, QPushButton,
    QHBoxLayout, QMenu, QInputDialog, QMessageBox
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
    edit_device_requested = pyqtSignal(object, str)    # CameraConfig, kind
    renamed = pyqtSignal(object, str)                  # CameraConfig (already renamed), kind
    playback_requested = pyqtSignal(object, str)       # CameraConfig, kind
    connect_all_requested = pyqtSignal(object, bool)   # NVR CameraConfig, use main stream
    disconnect_all_requested = pyqtSignal()
    grid_fullscreen_requested = pyqtSignal()

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

        playback_btn = QPushButton("📼 צפייה בהקלטות (לאחור)")
        playback_btn.setToolTip("בחר ערוץ ברשימה ולחץ. אפשר גם קליק ימני על מצלמה")
        playback_btn.clicked.connect(self._on_playback_clicked)

        full_btn = QPushButton("⛶ כל המצלמות במסך מלא (F11)")
        full_btn.setToolTip("מסתיר את הרשימה ומציג את כל המצלמות על כל המסך. Esc או F11 חוזרים")
        full_btn.clicked.connect(self.grid_fullscreen_requested.emit)

        layout = QVBoxLayout(self)
        layout.addLayout(btn_row)
        layout.addWidget(scan_btn)
        layout.addWidget(playback_btn)
        layout.addWidget(full_btn)
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

    def reset_nvr_children(self, nvr_item: QTreeWidgetItem):
        """Drop any previously-loaded channel list (e.g. after editing the
        NVR's credentials/IP, the old channels are stale) back to the
        'click to load' placeholder."""
        nvr_item.takeChildren()
        placeholder = QTreeWidgetItem(["לחץ כדי לטעון ערוצים..."])
        nvr_item.addChild(placeholder)
        nvr_item.setExpanded(False)

    def refresh_nvr_node(self, nvr_item: QTreeWidgetItem, cfg: CameraConfig):
        nvr_item.setData(0, ROLE_CFG, cfg)
        nvr_item.setText(0, f"🖥 {cfg.name} ({cfg.host})")
        self.reset_nvr_children(nvr_item)

    def refresh_single_node(self, item: QTreeWidgetItem, cfg: CameraConfig):
        item.setData(0, ROLE_CFG, cfg)
        item.setText(0, f"📷 {cfg.name} ({cfg.host})")

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

    def _on_playback_clicked(self):
        item = self.tree.currentItem()
        kind = item.data(0, ROLE_KIND) if item is not None else None
        if kind not in ("nvr", "nvr_channel", "single"):
            QMessageBox.information(self, "צפייה בהקלטות", "בחר קודם ערוץ (או NVR) מהרשימה, ואז לחץ שוב.")
            return
        self.playback_requested.emit(item.data(0, ROLE_CFG), kind)

    def _item_text(self, cfg: CameraConfig, kind: str) -> str:
        if kind == "nvr":
            return f"🖥 {cfg.name} ({cfg.host})"
        if kind == "single":
            return f"📷 {cfg.name} ({cfg.host})"
        return f"📷 {cfg.name}"

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        kind = item.data(0, ROLE_KIND)
        cfg = item.data(0, ROLE_CFG)
        if kind not in ("nvr", "nvr_channel", "single"):
            return
        menu = QMenu(self)
        rename_action = menu.addAction("🏷 שנה שם")
        play_action = None
        if cfg.protocol == "dvrip" or kind == "nvr_channel":
            play_action = menu.addAction("📼 צפייה בהקלטות")
        all_sub = all_main = all_off = None
        if kind == "nvr":
            menu.addSeparator()
            all_sub = menu.addAction("🔗 התחבר לכל הערוצים (זרם משני, קל)")
            all_main = menu.addAction("🔗 התחבר לכל הערוצים (זרם ראשי, כבד)")
            all_off = menu.addAction("⛔ התנתק מכל המקורות")
        edit_action = remove_action = None
        if kind in ("nvr", "single"):
            menu.addSeparator()
            edit_action = menu.addAction("✏ עריכת פרטי התחברות")
            remove_action = menu.addAction("🗑 הסר")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == rename_action:
            new, ok = QInputDialog.getText(self, "שינוי שם", "שם חדש:", text=cfg.name)
            new = new.strip()
            if ok and new:
                cfg.name = new
                item.setText(0, self._item_text(cfg, kind))
                self.renamed.emit(cfg, kind)
        elif all_sub is not None and chosen == all_sub:
            self.connect_all_requested.emit(cfg, False)
        elif all_main is not None and chosen == all_main:
            self.connect_all_requested.emit(cfg, True)
        elif all_off is not None and chosen == all_off:
            self.disconnect_all_requested.emit()
        elif chosen == play_action:
            self.playback_requested.emit(cfg, kind)
        elif chosen == remove_action:
            self.remove_device_requested.emit(cfg, kind)
        elif chosen == edit_action:
            self.edit_device_requested.emit(cfg, kind)
