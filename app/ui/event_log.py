"""Event list at the bottom of the main window (like the log in the manufacturer's CMS): live motion / alarm /
disk events pushed by the recorders, plus an optional load of the recorder's stored log."""
from __future__ import annotations

import threading
from datetime import datetime

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QDockWidget, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from app.core import alarms

MAX_ROWS = 500
COLUMNS = ("סוג", "זמן", "התקן", "ערוץ", "משתמש", "תיאור")


class AlarmHub(QObject):
    """One watcher per recorder; its events arrive here on the GUI thread."""
    event = pyqtSignal(dict)
    state = pyqtSignal(str)
    history_done = pyqtSignal(list, str)
    history_failed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watchers: dict[tuple, alarms.AlarmWatcher] = {}
        self._cfgs: dict[tuple, object] = {}

    def ensure(self, cfg, label: str | None = None):
        key = (cfg.host, cfg.port, cfg.username)
        if key in self._watchers:
            return
        self._cfgs[key] = cfg
        self._watchers[key] = alarms.AlarmWatcher(
            cfg.host, cfg.port, cfg.username, cfg.password, label or cfg.host,
            on_event=self.event.emit, on_state=self.state.emit)

    def recorders(self) -> list:
        return list(self._cfgs.values())

    def load_history(self, begin: str, end: str):
        cfgs = self.recorders()

        def work():
            rows, raw = [], ""
            try:
                for cfg in cfgs:
                    r, t = alarms.query_history(cfg.host, cfg.port, cfg.username, cfg.password, begin, end)
                    rows += r
                    raw = raw or t
                self.history_done.emit(rows, raw)
            except Exception as exc:  # noqa: BLE001 - shown in the window
                self.history_failed.emit(str(exc))

        threading.Thread(target=work, daemon=True, name="log-history").start()

    def stop(self):
        for w in self._watchers.values():
            w.stop()


class EventLogDock(QDockWidget):
    def __init__(self, hub: AlarmHub, parent=None):
        super().__init__("יומן אירועים", parent)
        self.hub = hub
        self.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.state = QLabel("")
        self.state.setStyleSheet("color:#8a8f9a;")
        hist = QPushButton("טען יומן מה-NVR (היום)")
        hist.setToolTip("שואל את ההקלטה את היומן השמור שלה. הפורמט תלוי בדגם, לכן זה ניסיוני")
        hist.clicked.connect(self._history)
        clear = QPushButton("נקה")
        clear.clicked.connect(lambda: self.table.setRowCount(0))
        top = QHBoxLayout()
        top.addWidget(self.state, 1)
        top.addWidget(hist)
        top.addWidget(clear)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.addLayout(top)
        lay.addWidget(self.table)
        self.setWidget(body)
        hub.event.connect(self.add_event)
        hub.state.connect(self.state.setText)
        hub.history_done.connect(self._history_done)
        hub.history_failed.connect(lambda e: self.state.setText(f"טעינת היומן נכשלה: {e}"))

    def _row(self, row: int, ev: dict):
        ch = ev.get("channel")
        values = (ev.get("type", ""), ev.get("time", ""), ev.get("device", ""),
                  "" if ch is None else str(int(ch) + 1), ev.get("user", ""), ev.get("text", ""))
        for col, v in enumerate(values):
            self.table.setItem(row, col, QTableWidgetItem(v))

    def add_event(self, ev: dict):
        self.table.insertRow(0)                    # newest on top
        self._row(0, ev)
        while self.table.rowCount() > MAX_ROWS:
            self.table.removeRow(self.table.rowCount() - 1)

    def _history(self):
        if not self.hub.recorders():
            self.state.setText("אין NVR מחובר. חבר מצלמה קודם")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        self.state.setText("טוען יומן...")
        self.hub.load_history(f"{today} 00:00:00", f"{today} 23:59:59")

    def _history_done(self, rows: list, raw: str):
        if not rows:
            self.state.setText("ה-NVR לא החזיר רשומות. תשובה גולמית: " + (raw or "אין תשובה"))
            return
        self.table.setRowCount(0)
        for ev in rows[:MAX_ROWS]:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self._row(r, ev)
        self.state.setText(f"נטענו {min(len(rows), MAX_ROWS)} רשומות מהיומן של ה-NVR")
