"""Menu "עזרה" + the dialogs of the self-update (see app/core/updater.py for how it works).

Three ways to get a new version:
  1. automatic: a quiet check a few seconds after the start (at most every 6 hours), then a question;
  2. "check for updates now" in the menu;
  3. no internet needed: "update from file..." or a file called UniversalCamViewer.new.exe put next to the program."""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QDesktopServices
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox, QProgressDialog

from app import BUILD, VERSION, __version__
from app.core import settings, updater

CHECK_EVERY = 6 * 3600
_ALIVE: list = []                      # a running QThread must never lose its last reference


def _keep(thread: QThread):
    _ALIVE.append(thread)
    thread.finished.connect(lambda t=thread: _ALIVE.remove(t) if t in _ALIVE else None)


class _CheckWorker(QThread):
    done = pyqtSignal(int, str)        # latest build, its notes
    failed = pyqtSignal(str)

    def run(self):
        try:
            build = updater.latest_build()
            notes = updater.release_notes(build) if build > BUILD else ""
            self.done.emit(build, notes)
        except updater.UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:       # noqa: BLE001 - shown to the person, never a silent dead thread
            self.failed.emit(f"שגיאה: {exc!r}")


class _DownloadWorker(QThread):
    progress = pyqtSignal(int, int)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, build: int, dest: Path):
        super().__init__()
        self.build, self.dest = build, dest
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            updater.download(self.build, self.dest, progress=self.progress.emit, cancelled=lambda: self._cancel)
            self.done.emit(str(self.dest))
        except updater.UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:       # noqa: BLE001
            self.failed.emit(f"שגיאה: {exc!r}")


class _InfoWorker(QThread):
    done = pyqtSignal(object)          # (version, build) or None

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def run(self):
        self.done.emit(updater.candidate_info(self.path))


class UpdateController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._busy = False
        self._progress: QProgressDialog | None = None
        self._download: _DownloadWorker | None = None

    # ---- menu --------------------------------------------------------------------------
    def build_menu(self, menubar):
        menu = menubar.addMenu("עזרה")
        label = QAction(f"גרסה {__version__}", menu)
        label.setEnabled(False)
        menu.addAction(label)
        menu.addSeparator()
        check = QAction("בדוק עדכונים עכשיו", menu)
        check.triggered.connect(lambda: self.check(manual=True))
        menu.addAction(check)
        from_file = QAction("עדכון מקובץ... (בלי אינטרנט)", menu)
        from_file.triggered.connect(self.update_from_file)
        menu.addAction(from_file)
        self.rollback_action = QAction("חזור לגרסה הקודמת", menu)
        self.rollback_action.triggered.connect(self.rollback)
        menu.addAction(self.rollback_action)
        auto = QAction("בדוק עדכונים אוטומטית", menu, checkable=True)
        auto.setChecked(bool(settings.get("update_auto", True)))
        auto.toggled.connect(lambda on: settings.put("update_auto", bool(on)))
        menu.addAction(auto)
        menu.addSeparator()
        about = QAction("אודות", menu)
        about.triggered.connect(self.about)
        menu.addAction(about)
        menu.aboutToShow.connect(self._refresh_menu)
        self._refresh_menu()

    def _refresh_menu(self):
        self.rollback_action.setEnabled(updater.is_frozen() and updater.rollback_available())

    # ---- startup ------------------------------------------------------------------------
    def start(self):
        """Called once after the window is shown."""
        QTimer.singleShot(2500, self._look_for_dropin)
        if settings.get("update_auto", True) and updater.is_frozen():
            QTimer.singleShot(8000, lambda: self.check(manual=False))

    def _look_for_dropin(self):
        if not updater.is_frozen():
            return
        path = updater.dropin_path()
        if path.is_file():
            self._offer_file(path, in_place=True)

    # ---- 1+2: from the internet ----------------------------------------------------------------
    def check(self, manual: bool):
        if self._busy:
            return
        if not manual and time.time() - float(settings.get("update_last_check", 0) or 0) < CHECK_EVERY:
            return
        self._busy = True
        w = _CheckWorker()
        _keep(w)
        w.done.connect(lambda b, n, m=manual: self._on_checked(b, n, m))
        w.failed.connect(lambda e, m=manual: self._on_check_failed(e, m))
        w.start()

    def _on_check_failed(self, error: str, manual: bool):
        self._busy = False
        if manual:
            QMessageBox.warning(
                self.win, "בדיקת עדכונים",
                f"{error}.\n\nאפשר לעדכן בלי אינטרנט: בתפריט עזרה ← 'עדכון מקובץ...', או להניח ליד התוכנה "
                f"קובץ בשם {updater.DROPIN_NAME} ולהפעיל מחדש.")

    def _on_checked(self, build: int, notes: str, manual: bool):
        self._busy = False
        settings.put("update_last_check", time.time())
        if build <= BUILD:
            if manual:
                QMessageBox.information(self.win, "בדיקת עדכונים",
                                        f"התוכנה מעודכנת.\nגרסה {__version__} (האחרונה באתר: build {build}).")
            return
        if not manual and settings.get("update_skip") == build:
            return
        if not updater.is_frozen():
            QMessageBox.information(
                self.win, "גרסה חדשה",
                f"יש גרסה חדשה (build {build}). התוכנה רצה כרגע מקוד המקור ולכן לא מתעדכנת לבד.\n"
                "הורד את הגרסה האחרונה מהאתר.")
            QDesktopServices.openUrl(QUrl(updater.REPO_URL + "/releases/latest"))
            return
        box = QMessageBox(self.win)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("גרסה חדשה")
        box.setText(f"יש גרסה חדשה: build {build}  (אצלך: {__version__})")
        if notes:
            box.setInformativeText("מה חדש:\n" + notes)
        now = box.addButton("עדכן עכשיו", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("אחר כך", QMessageBox.ButtonRole.RejectRole)
        skip = box.addButton("דלג על גרסה זו", QMessageBox.ButtonRole.DestructiveRole)
        box.exec()
        if box.clickedButton() is now:
            self._start_download(build)
        elif box.clickedButton() is skip:
            settings.put("update_skip", build)

    def _start_download(self, build: int):
        exe = updater.exe_path()
        if not updater.can_write(exe.parent):
            QMessageBox.warning(self.win, "עדכון",
                                f"אין הרשאת כתיבה בתיקייה:\n{exe.parent}\n\n"
                                "העבר את התוכנה לתיקייה רגילה (למשל שולחן העבודה), או הרץ אותה כמנהל.")
            return
        dest = exe.with_name(exe.name + ".update.tmp")
        dlg = QProgressDialog("מוריד עדכון...", "ביטול", 0, 100, self.win)
        dlg.setWindowTitle("עדכון התוכנה")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        w = _DownloadWorker(build, dest)
        _keep(w)
        self._progress, self._download = dlg, w
        dlg.canceled.connect(w.cancel)
        w.progress.connect(self._on_progress)
        w.done.connect(lambda p: self._on_downloaded(Path(p)))
        w.failed.connect(self._on_download_failed)
        dlg.show()
        w.start()

    def _on_progress(self, done: int, total: int):
        if self._progress is None:
            return
        if total > 0:
            self._progress.setMaximum(total // 1024)
            self._progress.setValue(done // 1024)
            self._progress.setLabelText(f"מוריד עדכון... {done / 1048576:.0f} / {total / 1048576:.0f} MB")
        else:
            self._progress.setMaximum(0)

    def _close_progress(self):
        if self._progress is not None:
            self._progress.close()
            self._progress = None

    def _on_download_failed(self, error: str):
        self._close_progress()
        if "בוטל" not in error:
            QMessageBox.warning(self.win, "עדכון", error)

    def _on_downloaded(self, path: Path):
        self._close_progress()
        self._apply_and_restart(path)

    # ---- 3: without internet ---------------------------------------------------------------------
    def update_from_file(self):
        if not updater.is_frozen():
            QMessageBox.information(self.win, "עדכון מקובץ",
                                    "התוכנה רצה מקוד המקור. עדכון מקובץ עובד רק בגרסת ה-EXE.")
            return
        name, _ = QFileDialog.getOpenFileName(self.win, "בחר קובץ עדכון (UniversalCamViewer.exe)", "",
                                              "תוכנה (*.exe);;כל הקבצים (*)")
        if name:
            self._offer_file(Path(name), in_place=False)

    def _offer_file(self, path: Path, in_place: bool):
        if self._busy:
            return
        try:
            updater.verify_exe(path)
        except updater.UpdateError as exc:
            QMessageBox.warning(self.win, "עדכון מקובץ", f"{path.name}: {exc}")
            return
        self._busy = True
        dlg = QProgressDialog("בודק את הקובץ... (עד חצי דקה)", None, 0, 0, self.win)
        dlg.setWindowTitle("עדכון מקובץ")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.show()
        w = _InfoWorker(path)
        _keep(w)
        w.done.connect(lambda info, p=path, i=in_place, d=dlg: self._on_file_info(p, i, info, d))
        w.start()

    def _on_file_info(self, path: Path, in_place: bool, info, dlg: QProgressDialog):
        dlg.close()
        self._busy = False
        if info is None:
            detail = "לא ניתן לזהות את הגרסה של הקובץ (ייתכן שזו גרסה ישנה)."
        else:
            version, build = info
            relation = ("חדשה יותר" if build > BUILD else "זהה" if build == BUILD else "ישנה יותר")
            detail = f"הקובץ: גרסה {version}, build {build}  ({relation} מהנוכחית)."
        ask = QMessageBox(self.win)
        ask.setIcon(QMessageBox.Icon.Question)
        ask.setWindowTitle("עדכון מקובץ")
        ask.setText(f"{detail}\nהגרסה הנוכחית: {__version__}\n\nלהחליף ולהפעיל מחדש?")
        yes = ask.addButton("עדכן", QMessageBox.ButtonRole.AcceptRole)
        ask.addButton("ביטול", QMessageBox.ButtonRole.RejectRole)
        ask.exec()
        if ask.clickedButton() is not yes:
            return
        exe = updater.exe_path()
        if not updater.can_write(exe.parent):
            QMessageBox.warning(self.win, "עדכון", f"אין הרשאת כתיבה בתיקייה:\n{exe.parent}")
            return
        staged = exe.with_name(exe.name + ".update.tmp")
        try:
            if in_place:
                path.replace(staged)               # same folder: a rename, instant
            else:
                shutil.copy2(path, staged)         # from a USB drive etc.
        except OSError as exc:
            QMessageBox.warning(self.win, "עדכון", f"העתקת הקובץ נכשלה: {exc}")
            return
        self._apply_and_restart(staged)

    # ---- rollback / about ----------------------------------------------------------------------------
    def rollback(self):
        if QMessageBox.question(self.win, "גרסה קודמת",
                                "לחזור לגרסה הקודמת של התוכנה ולהפעיל מחדש?") != QMessageBox.StandardButton.Yes:
            return
        try:
            updater.rollback()
        except updater.UpdateError as exc:
            QMessageBox.warning(self.win, "גרסה קודמת", str(exc))
            return
        self._restart()

    def about(self):
        QMessageBox.information(
            self.win, "אודות",
            f"צופה מצלמות אוניברסלי\n\nגרסה {__version__}\nמספר גרסה: {VERSION}   |   build: {BUILD}\n"
            f"Python {sys.version.split()[0]}   |   {'EXE' if updater.is_frozen() else 'מקוד המקור'}\n"
            f"מקור עדכונים: {updater.REPO_URL}")

    # ---- the swap ----------------------------------------------------------------------------------
    def _apply_and_restart(self, staged: Path):
        try:
            updater.apply(staged)
        except updater.UpdateError as exc:
            try:
                staged.unlink()
            except OSError:
                pass
            QMessageBox.warning(self.win, "עדכון", str(exc))
            return
        self._restart()

    def _restart(self):
        QMessageBox.information(self.win, "עדכון", "העדכון הותקן. התוכנה נסגרת ותיפתח שוב בעוד כמה שניות.")
        updater.relaunch_later()
        self.win.close()
        QApplication.quit()
