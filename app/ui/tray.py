"""System-tray (notification area) icon: the program keeps running when the window is minimised.

Left click = show / hide the window. Right click = menu: open, hide, all cameras full screen, event log,
notifications for events while hidden, minimise / close to the tray, start with Windows, updates, exit.
While the window is hidden the video is not drawn (saves CPU), and new events turn the tray icon orange."""
from __future__ import annotations

import time

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from app import __version__
from app.core import autostart, settings
from app.core.config import CameraStatus
from app.ui.logo import make_icon

NOTIFY_GAP = 30.0                          # seconds between two messages about the same channel


class TrayController(QObject):
    def __init__(self, win, hub):
        super().__init__(win)
        self.win = win
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self._last_note: dict[tuple, float] = {}
        self._unseen = 0
        self.tray = QSystemTrayIcon(make_icon(), win)
        self.tray.setToolTip("צופה מצלמות אוניברסלי")
        self._build_menu()
        self.tray.activated.connect(self._on_activated)
        hub.event.connect(self._on_event)
        self._tip_timer = QTimer(self)
        self._tip_timer.setInterval(5000)
        self._tip_timer.timeout.connect(self._update_tooltip)
        if self.available:
            self.tray.show()
            self._tip_timer.start()
            self._update_tooltip()

    # ---- settings (read by the window too) -------------------------------------------------
    @staticmethod
    def minimize_to_tray() -> bool:
        return bool(settings.get("minimize_to_tray", True))

    @staticmethod
    def close_to_tray() -> bool:
        return bool(settings.get("close_to_tray", False))

    def enabled(self) -> bool:
        return self.available and self.tray.isVisible()

    # ---- menu --------------------------------------------------------------------------------
    def _build_menu(self):
        menu = QMenu()
        menu.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        title = QAction(f"צופה מצלמות אוניברסלי  {__version__}", menu)
        title.setEnabled(False)
        menu.addAction(title)
        menu.addSeparator()
        self.open_action = menu.addAction("פתח את התוכנה")
        font = self.open_action.font()
        font.setBold(True)
        self.open_action.setFont(font)
        self.open_action.triggered.connect(self.win.restore_from_tray)
        self.hide_action = menu.addAction("הסתר למגש")
        self.hide_action.triggered.connect(self.win.hide_to_tray)
        menu.addSeparator()
        menu.addAction("כל המצלמות במסך מלא (F11)").triggered.connect(self._grid_fullscreen)
        menu.addAction("יומן אירועים").triggered.connect(self._show_event_log)
        menu.addSeparator()
        self._toggle(menu, "הודעות על אירועים כשהחלון מוסתר", "tray_notify", True)
        self._toggle(menu, "מזעור החלון מעביר אותו למגש", "minimize_to_tray", True)
        self._toggle(menu, "סגירת החלון (X) ממזערת למגש", "close_to_tray", False)
        self.autostart_action = menu.addAction("הפעל עם Windows")
        self.autostart_action.setCheckable(True)
        if autostart.available():
            try:
                self.autostart_action.setChecked(autostart.is_enabled())
                autostart.refresh()
            except OSError:
                pass
            self.autostart_action.toggled.connect(self._set_autostart)
        else:
            self.autostart_action.setEnabled(False)
            self.autostart_action.setToolTip("זמין רק בגרסת ה-EXE ב-Windows")
        menu.addSeparator()
        menu.addAction("בדוק עדכונים").triggered.connect(self._check_updates)
        menu.addSeparator()
        menu.addAction("יציאה").triggered.connect(self.win.quit_app)
        menu.aboutToShow.connect(self._refresh_menu)
        self.menu = menu
        self.tray.setContextMenu(menu)

    @staticmethod
    def _toggle(menu: QMenu, text: str, key: str, default: bool):
        act = menu.addAction(text)
        act.setCheckable(True)
        act.setChecked(bool(settings.get(key, default)))
        act.toggled.connect(lambda on, k=key: settings.put(k, bool(on)))
        return act

    def _refresh_menu(self):
        hidden = not self.win.isVisible()
        self.open_action.setEnabled(True)
        self.hide_action.setEnabled(not hidden)

    def _set_autostart(self, on: bool):
        try:
            autostart.set_enabled(on)
        except OSError as exc:
            self.autostart_action.blockSignals(True)
            self.autostart_action.setChecked(not on)
            self.autostart_action.blockSignals(False)
            self.show_message("הפעלה עם Windows", f"לא הצלחתי לשנות את ההגדרה: {exc}")

    def _grid_fullscreen(self):
        self.win.restore_from_tray()
        self.win._toggle_grid_fullscreen()

    def _show_event_log(self):
        self.win.restore_from_tray()
        self.win.event_dock.show()
        self.win.event_dock.raise_()

    def _check_updates(self):
        self.win.restore_from_tray()
        self.win.updates.check(manual=True)

    # ---- behaviour -----------------------------------------------------------------------------
    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.win.isVisible() and not self.win.isMinimized():
                self.win.hide_to_tray()
            else:
                self.win.restore_from_tray()

    def _update_tooltip(self):
        tiles = getattr(self.win.grid, "tiles", {})
        active = sum(1 for t in tiles.values() if t.status == CameraStatus.ACTIVE)
        text = f"צופה מצלמות אוניברסלי\n{active} מצלמות פעילות מתוך {len(tiles)}"
        if self._unseen:
            text += f"\n{self._unseen} אירועים חדשים"
        self.tray.setToolTip(text)

    def show_message(self, title: str, text: str, ms: int = 5000):
        if self.available:
            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, ms)

    def _on_event(self, ev: dict):
        if self.win.isVisible() or not self.available:
            return
        if ev.get("status") == "Stop":
            return
        self._unseen += 1
        self.tray.setIcon(make_icon("alert"))
        self._update_tooltip()
        if not settings.get("tray_notify", True):
            return
        key = (ev.get("host"), ev.get("channel"), ev.get("event"))
        now = time.monotonic()
        if now - self._last_note.get(key, 0.0) < NOTIFY_GAP:
            return
        self._last_note[key] = now
        self.show_message(ev.get("device") or "אירוע", ev.get("text") or ev.get("type") or "אירוע")

    def on_window_shown(self):
        """The person is looking at the window again: clear the 'new events' marker."""
        if self._unseen:
            self._unseen = 0
            self.tray.setIcon(make_icon())
            self._update_tooltip()
