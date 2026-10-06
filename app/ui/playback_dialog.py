"""Browse and play the recordings stored on an XM / Provision NVR (DVRIP)."""
from __future__ import annotations

import sys

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QDate, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QDateEdit, QPushButton, QListWidget,
    QListWidgetItem, QLabel, QCheckBox, QWidget, QSpinBox, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QProgressBar, QFileDialog,
)

from app.core import dvrip, recsearch, recfmt, mp4, resumable
from app.core.audio import AudioPlayer
from app.core.camera import CameraConfig
from app.ui.player_window import open_player, open_stream_player


def _explain(step: str, exc: Exception) -> str:
    if isinstance(exc, OSError) and "time" in str(exc).lower():
        return f"{step}: ה-NVR לא ענה בזמן."
    return f"{step}: {exc}"


_BACKGROUND: list = []     # search threads that were cancelled but may still be waiting on the NVR


class _QueryWorker(QThread):
    partial = pyqtSignal(list)         # newly found recordings, while the search is still running
    progress_text = pyqtSignal(str)
    log_line = pyqtSignal(str)
    done = pyqtSignal(list, list)      # files, windows that failed
    failed = pyqtSignal(str)

    def __init__(self, cfg: CameraConfig, channel: int, days: list[str], mode: str):
        super().__init__()
        self.cfg, self.channel, self.days, self.mode = cfg, channel, days, mode
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _log(self, line: str):
        print(f"[playback] ערוץ {self.channel + 1} {line}", file=sys.stderr)
        self.log_line.emit(line)

    def run(self):
        c = dvrip.DVRIPClient(self.cfg.host, self.cfg.port, self.cfg.username, self.cfg.password, timeout=10)
        step = "התחברות ל-NVR"
        try:
            c.connect()
            c.login()
            step = f"חיפוש הקלטות בערוץ {self.channel + 1}"
            files, bad = recsearch.run_search(
                c, self.channel, self.days, recsearch.plans_for(self.mode),
                on_files=self.partial.emit, log=self._log, status=self.progress_text.emit,
                cancelled=lambda: self._cancel)
            self.done.emit(files, bad)
        except (OSError, dvrip.DVRIPError) as exc:
            self._log(f"{step}: {exc!r}")
            self.failed.emit(_explain(step, exc))
        except Exception as exc:               # a bug must show up in the window, not vanish with the thread
            self._log(f"שגיאה פנימית: {exc!r}")
            self.failed.emit(f"שגיאה פנימית: {exc!r}")
        finally:
            c.close()


class _PlayWorker(QThread):
    frame = pyqtSignal(object)
    audio = pyqtSignal(bytes, int)
    message = pyqtSignal(str)

    def __init__(self, cfg: CameraConfig, channel: int, item: dict, max_width: int = 960, stream_type: int = 0):
        super().__init__()
        self.cfg, self.channel, self.item, self.max_width = cfg, channel, item, max_width
        self.stream_type = stream_type
        self.pending = False
        self.audio_enabled = False
        self._running = True

    def stop(self):
        self._running = False
        self.wait(3000)

    def run(self):
        c = dvrip.DVRIPClient(self.cfg.host, self.cfg.port, self.cfg.username, self.cfg.password, timeout=15)
        step = "התחברות ל-NVR"
        try:
            c.connect()
            c.login()
            self.message.emit("פותח הקלטה...")
            step = f"פתיחת הקלטה בערוץ {self.channel + 1}"
            c.start_playback(self.channel, self.item, first_data_timeout=12.0, stream_type=self.stream_type)
            step = "ניגון"
            parser, decoder = dvrip.XMFrameParser(), dvrip.H264Decoder(self.max_width)
            shown = False
            for chunk in c.read_video_payloads():
                if not self._running:
                    return
                c.keepalive_if_due()
                videos = parser.feed(chunk)
                audio, parser.audio = parser.audio, []
                if self.audio_enabled:
                    for _media, rate, payload in audio:
                        self.audio.emit(dvrip.alaw_to_pcm16(payload), rate)
                for v in videos:
                    for f in decoder.decode_raw(v):
                        if not shown:
                            shown = True
                            self.message.emit("מנגן")
                        if self.pending:
                            continue
                        arr = dvrip.H264Decoder.to_rgb(f, self.max_width)
                        h, w, _ = arr.shape
                        img = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
                        self.pending = True
                        self.frame.emit(img)
            self.message.emit("ההקלטה הסתיימה" if shown else "ה-NVR לא שלח תמונה מההקלטה")
        except (OSError, dvrip.DVRIPError) as exc:
            print(f"[playback] {step}: {exc!r}", file=sys.stderr)
            self.message.emit(f"שגיאה - {_explain(step, exc)}")
        finally:
            c.close()


class _DownloadWorker(QThread):
    progress = pyqtSignal(object, object)   # bytes received, expected bytes (0 = unknown); object: files can pass 2 GB
    stage = pyqtSignal(str)
    note = pyqtSignal(str)              # status text only (e.g. waiting for the NVR to come back)
    finished_ok = pyqtSignal(str, str)  # final path, note ("" when everything went as planned)
    failed = pyqtSignal(str)

    def __init__(self, cfg: CameraConfig, channel: int, item: dict, path: str, stream_type: int = 0):
        super().__init__()
        self.cfg, self.channel, self.item, self.path = cfg, channel, item, path   # path: the .mp4 to create
        self.stream_type = stream_type
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _open_stream(self):
        c = dvrip.DVRIPClient(self.cfg.host, self.cfg.port, self.cfg.username, self.cfg.password, timeout=15)
        try:
            c.connect()
            c.login()
            c.start_download(self.channel, self.item, stream_type=self.stream_type)
            return c, c.read_download_payloads()
        except BaseException:
            c.close()
            raise

    def run(self):
        target = Path(self.path)
        raw = target.with_suffix(".h264.part")
        dl = resumable.ResumableDownload(
            self._open_stream, raw,
            identity={"channel": self.channel, "begin": self.item.get("begin"), "end": self.item.get("end"),
                      "stream": self.stream_type},
            expected=int(self.item.get("size") or 0),
            progress=self.progress.emit, status=self.note.emit, cancelled=lambda: self._cancel,
            on_chunk=lambda client: client.keepalive_if_due())
        try:
            res = dl.run()
        except resumable.Cancelled:
            self.failed.emit("ההורדה הופסקה. מה שירד נשמר - בחר שוב את אותה הקלטה ולחץ 'הורד קובץ' כדי להמשיך")
            return
        except resumable.DownloadFailed as exc:
            print(f"[download] {exc}", file=sys.stderr)
            self.failed.emit(f"שגיאה - {exc}")
            return
        except OSError as exc:                       # disk full / cannot write
            print(f"[download] {exc!r}", file=sys.stderr)
            self.failed.emit(f"שגיאה בכתיבת הקובץ: {exc}")
            return
        codec = res.codec
        print(f"[download] embedded headers removed: {res.embedded}, stray bytes skipped: {res.dropped}, "
              f"reconnects: {res.reconnects}", file=sys.stderr)
        try:                                      # keep the numbers next to the video, for diagnosing damage later
            Path(str(target) + ".log.txt").write_text(
                f"video bytes saved: {res.size} (NVR reported size: {dl.expected})\nframes: {res.frames}\n"
                f"reconnects: {res.reconnects}\ncomplete: {res.complete}\n"
                f"embedded headers removed: {res.embedded}\nstray bytes skipped: {res.dropped}\n", encoding="utf-8")
        except OSError:
            pass
        if res.size == 0:
            dl.discard()
            self.failed.emit("ה-NVR לא שלח נתונים")
            return
        extra = ""
        if res.reconnects:
            extra += f"החיבור נפסק {res.reconnects} פעמים וההורדה המשיכה. "
        if not res.complete:
            extra += "ה-NVR סיים את ההורדה לפני הסוף, ייתכן שחלק מההקלטה חסר. "
        self.stage.emit("ממיר ל-MP4...")
        secs = recfmt.duration_seconds(self.item["begin"], self.item["end"])
        try:
            mp4.raw_to_mp4(str(raw), str(target), codec, secs)
        except Exception as exc:                  # noqa: BLE001 - keep the download, whatever went wrong
            print(f"[download] המרה ל-MP4: {exc!r}", file=sys.stderr)
            target.unlink(missing_ok=True)
            kept = target.with_suffix(".h265" if codec == "hevc" else ".h264")
            raw.replace(kept)
            self.finished_ok.emit(str(kept), f"{extra}ההמרה ל-MP4 נכשלה ({exc}). נשמר קובץ וידאו גולמי, נפתח ב-VLC")
            return
        raw.unlink(missing_ok=True)
        self.finished_ok.emit(str(target), extra.strip())


class PlaybackDialog(QDialog):
    def __init__(self, cfg: CameraConfig, channels: list[tuple[int, str]], channel: int, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle(f"הקלטות - {cfg.name}")
        self.resize(1000, 640)
        self._player: _PlayWorker | None = None
        self._query: _QueryWorker | None = None
        self._download: _DownloadWorker | None = None
        self._audio = AudioPlayer()
        self._fullscreen = False
        self._was_maximized = False

        self.channel_combo = QComboBox()
        for idx, label in channels:
            self.channel_combo.addItem(label, idx)
        i = self.channel_combo.findData(channel)
        if i >= 0:
            self.channel_combo.setCurrentIndex(i)
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setMaximumDate(QDate.currentDate())   # a future date can make some NVRs stop answering
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        search_btn = QPushButton("🔍 חפש הקלטות")
        search_btn.clicked.connect(self._search)

        self.mode_combo = QComboBox()
        for key, label in recsearch.MODES:
            self.mode_combo.addItem(label, key)
        self.mode_combo.setToolTip("אם שיטה אחת לא מוצאת הקלטות, נסה שיטה אחרת. 'אוטומטי' מנסה את כולן")
        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 365)
        self.days_spin.setValue(14)
        self.days_spin.setSuffix(" ימים אחורה")
        self.days_spin.setToolTip("בחיפוש רחב: כמה ימים לסרוק, מהתאריך שנבחר אחורה")
        self.days_spin.setEnabled(False)
        self.mode_combo.currentIndexChanged.connect(
            lambda _i: self.days_spin.setEnabled(self.mode_combo.currentData() == "wide"))

        self.top_bar = QWidget()
        top_col = QVBoxLayout(self.top_bar)
        top_col.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(QLabel("ערוץ:"))
        top.addWidget(self.channel_combo, 1)
        top.addWidget(QLabel("תאריך:"))
        top.addWidget(self.date_edit)
        top.addWidget(search_btn)
        top2 = QHBoxLayout()
        top2.addWidget(QLabel("שיטת חיפוש:"))
        top2.addWidget(self.mode_combo, 1)
        top2.addWidget(self.days_spin)
        top_col.addLayout(top)
        top_col.addLayout(top2)
        self._found: dict = {}
        self._log: list[str] = []
        self._rebuild_timer = QTimer(self)             # batches list refreshes while results stream in
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(300)
        self._rebuild_timer.timeout.connect(self._rebuild_list)

        self.files = QTreeWidget()
        self.files.setColumnCount(3)
        self.files.setHeaderLabels(["שעות", "משך", "גודל"])
        self.files.setRootIsDecorated(True)
        self.files.setAlternatingRowColors(True)
        self.files.setUniformRowHeights(True)
        self.files.setMinimumWidth(360)
        self.files.setMaximumWidth(460)
        hdr = self.files.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.files.itemDoubleClicked.connect(
            lambda i, _c: self._play() if isinstance(i.data(0, Qt.ItemDataRole.UserRole), dict) else None)

        self.video = QLabel("בחר ערוץ ותאריך ולחץ 'חפש הקלטות'")
        self.video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video.setMinimumSize(560, 315)
        self.video.setStyleSheet("background:#0d0f13; color:#8a8f9a;")

        play_btn = QPushButton("▶ נגן")
        play_btn.clicked.connect(self._play)
        stop_btn = QPushButton("⏹ עצור")
        stop_btn.clicked.connect(self._on_stop_clicked)
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("איכות מלאה", 0)
        self.quality_combo.addItem("פשוטה (זרם משני)", 1)
        self.quality_combo.setToolTip("'פשוטה' מבקשת מה-NVR את הזרם הקל (רזולוציה נמוכה, קל לניגון). "
                                      "לא כל NVR מקליט אותו - אם לא מגיעה תמונה, חזור לאיכות מלאה")
        self.dl_btn = QPushButton("⬇ הורד קובץ")
        self.dl_btn.setToolTip("שומר את ההקלטה שנבחרה כקובץ MP4 בתיקייה Videos\\CameraRecordings")
        self.dl_btn.clicked.connect(self._download_selected)
        self.play_full_btn = QPushButton("▶ נגן במלא  (קדימה / אחורה / מהירויות)")
        self.play_full_btn.setToolTip("פותח נגן מלא ומתחיל לנגן מיד. ההקלטה יורדת ברקע, אפשר לקפוץ אחורה, לנגן לאחור, "
                                 "להאיץ ולהאט. קדימה אפשר עד המקום שכבר ירד. בסוף אפשר לשמור כ-MP4")
        self.play_full_btn.clicked.connect(self._play_full)
        self.open_btn = QPushButton("📂 פתח קובץ בנגן")
        self.open_btn.setToolTip("פותח בנגן המלא קובץ וידאו שהורדת קודם")
        self.open_btn.clicked.connect(self._open_local)
        self.dl_bar = QProgressBar()
        self.dl_bar.setVisible(False)
        self.dl_bar.setMaximumHeight(14)
        self.sound_box = QCheckBox("🔊 שמע")
        self.sound_box.toggled.connect(self._on_sound)
        self.full_btn = QPushButton("⛶ מסך מלא")
        self.full_btn.setToolTip("אפשר גם לחיצה כפולה על התמונה. Esc חוזר לתצוגה רגילה")
        self.full_btn.clicked.connect(self._toggle_fullscreen)
        self.log_btn = QPushButton("📋 העתק יומן")
        self.log_btn.setToolTip("מעתיק ללוח את פירוט החיפוש האחרון, כדי לשלוח אותו לבדיקה")
        self.log_btn.clicked.connect(self._copy_log)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        controls = QHBoxLayout()
        controls.addWidget(play_btn)
        controls.addWidget(stop_btn)
        controls.addWidget(self.quality_combo)
        controls.addWidget(self.dl_btn)
        controls.addWidget(self.sound_box)
        controls.addWidget(self.full_btn)
        controls.addWidget(self.log_btn)
        controls.addWidget(self.status, 1)

        controls2 = QHBoxLayout()
        controls2.addWidget(self.play_full_btn)
        controls2.addWidget(self.open_btn)
        controls2.addStretch(1)

        right = QVBoxLayout()
        right.addWidget(self.video, 1)
        right.addWidget(self.dl_bar)
        right.addLayout(controls)
        right.addLayout(controls2)
        body = QHBoxLayout()
        body.addWidget(self.files)
        body.addLayout(right, 1)
        root = QVBoxLayout(self)
        root.addWidget(self.top_bar)
        root.addLayout(body, 1)

    # ---- search -------------------------------------------------------
    def _retire_query(self):
        """Let go of the finished search, but never drop a QThread that is still winding down."""
        q, self._query = self._query, None
        _BACKGROUND[:] = [t for t in _BACKGROUND if t.isRunning()]
        if q is not None and q.isRunning():
            _BACKGROUND.append(q)

    def _cancel_query(self):
        q = self._query
        if q is None:
            _BACKGROUND[:] = [t for t in _BACKGROUND if t.isRunning()]
            return
        q.cancel()
        for sig in (q.partial, q.progress_text, q.log_line, q.done, q.failed):
            try:
                sig.disconnect()
            except TypeError:
                pass
        self._retire_query()               # keeps the object alive until it has really finished

    def _search(self):
        self._stop()
        self._cancel_query()
        self._found = {}
        self._log = []
        self.files.clear()
        mode = self.mode_combo.currentData()
        first = self.date_edit.date()
        n = self.days_spin.value() if mode == "wide" else 1
        days = [first.addDays(-i).toString("yyyy-MM-dd") for i in range(n)]
        ch = self.channel_combo.currentData()
        label = self.mode_combo.currentText()
        self._log.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} ערוץ {ch + 1}, שיטה: {label}, ימים: {days[-1]}..{days[0]}")
        self.status.setText("מחפש הקלטות...")
        q = _QueryWorker(self.cfg, ch, days, mode)
        q.partial.connect(self._on_partial)
        q.progress_text.connect(self.status.setText)
        q.log_line.connect(self._log.append)
        q.done.connect(self._on_files)
        q.failed.connect(self._on_query_failed)
        self._query = q
        q.start()

    @staticmethod
    def _key(f: dict) -> tuple:
        return (f["begin"], f["end"], f["size"])

    def _on_partial(self, new: list):
        for f in new:
            self._found[self._key(f)] = f
        if not self._rebuild_timer.isActive():
            self._rebuild_timer.start()

    def _rebuild_list(self):
        cur = self.files.currentItem()
        keep = cur.data(0, Qt.ItemDataRole.UserRole) if cur is not None else None
        keep_key = self._key(keep) if isinstance(keep, dict) else None
        self.files.clear()
        bold = QFont()
        bold.setBold(True)
        for day, day_files in recfmt.group_by_day(self._found.values()):
            total = sum(f["size"] for f in day_files)
            head = QTreeWidgetItem([f"📅 {day}   ({len(day_files)} הקלטות)", "", recfmt.fmt_size(total)])
            head.setFlags(Qt.ItemFlag.ItemIsEnabled)          # a heading: cannot be selected or played
            for col in range(3):
                head.setFont(col, bold)
            self.files.addTopLevelItem(head)
            for f in day_files:
                dur = recfmt.fmt_duration(recfmt.duration_seconds(f["begin"], f["end"]))
                row = QTreeWidgetItem([f"{f['begin'][11:]} – {f['end'][11:]}", dur, recfmt.fmt_size(f["size"])])
                row.setData(0, Qt.ItemDataRole.UserRole, f)
                row.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
                row.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
                head.addChild(row)
                if keep_key is not None and self._key(f) == keep_key:
                    self.files.setCurrentItem(row)
            head.setExpanded(True)

    def _on_files(self, files: list, failed: list):
        for f in files:
            self._found[self._key(f)] = f
        self._rebuild_timer.stop()
        self._rebuild_list()
        self._retire_query()
        if files:
            text = f"נמצאו {len(files)} הקלטות"
        else:
            text = ("לא נמצאו הקלטות. נסה שיטת חיפוש אחרת, או 'חיפוש רחב' לסריקת כמה ימים. "
                    "אחרי חיפוש אפשר ללחוץ 'העתק יומן' ולשלוח לי")
        if failed:
            text += f". ה-NVR לא ענה עבור {len(failed)} חלונות זמן (חלקי, אפשר לחפש שוב)"
        self.status.setText(text)

    def _on_query_failed(self, message: str):
        self._retire_query()
        self._rebuild_timer.stop()
        self._rebuild_list()
        self.status.setText(f"שגיאה: {message}")

    def _on_stop_clicked(self):
        self._stop()
        if self._query is not None:
            self._cancel_query()
            self._rebuild_timer.stop()
            self._rebuild_list()
            self.status.setText(f"החיפוש הופסק. נמצאו {len(self._found)} הקלטות עד כה")

    def _copy_log(self):
        QApplication.clipboard().setText("\n".join(self._log) or "אין יומן: עדיין לא בוצע חיפוש")
        self.status.setText("היומן הועתק ללוח. הדבק אותו בצ'אט")

    # ---- playback -----------------------------------------------------
    def _selected_record(self):
        item = self.files.currentItem()
        rec = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        return rec if isinstance(rec, dict) else None       # None: nothing chosen, or a date heading

    def _download_selected(self):
        if self._download is not None and self._download.isRunning():
            self._download.cancel()
            self.status.setText("מבטל את ההורדה...")
            return
        rec = self._selected_record()
        if rec is None:
            self.status.setText("בחר הקלטה מהרשימה כדי להוריד")
            return
        ch = self.channel_combo.currentData()
        folder = Path.home() / "Videos" / "CameraRecordings"      # saved automatically, no dialog
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            folder = Path.home()
        path = folder / recfmt.safe_filename(ch, rec, "mp4")
        if path.with_suffix(".h264.part.json").exists() and not path.exists():
            pass                                                  # an interrupted download of this recording: continue it
        else:
            n = 2
            while path.exists():                                  # never overwrite an earlier download
                path = folder / recfmt.safe_filename(ch, rec, "mp4").replace(".mp4", f"_{n}.mp4")
                n += 1
        w = _DownloadWorker(self.cfg, ch, rec, str(path), self.quality_combo.currentData())
        w.progress.connect(self._on_dl_progress)
        w.stage.connect(self._on_dl_stage)
        w.note.connect(self.status.setText)
        w.finished_ok.connect(self._on_dl_done)
        w.failed.connect(self._on_dl_failed)
        self._download = w
        self.dl_btn.setText("✖ בטל הורדה")
        self.dl_bar.setRange(0, 0)
        self.dl_bar.setVisible(True)
        self.status.setText("מוריד...")
        w.start()

    def _play_full(self):
        rec = self._selected_record()
        if rec is None:
            self.status.setText("בחר הקלטה מהרשימה כדי לנגן")
            return
        self._stop()                                  # the small preview also uses a connection to the NVR
        try:
            open_stream_player(self.cfg, self.channel_combo.currentData(), rec, self.quality_combo.currentData())
        except Exception as exc:                      # noqa: BLE001 - say so in the window instead of failing silently
            print(f"[player] {exc!r}", file=sys.stderr)
            self.status.setText(f"פתיחת הנגן נכשלה: {exc}")
            return
        self.status.setText("הנגן נפתח. ההקלטה יורדת ברקע ואפשר לצפות כבר עכשיו")

    def _open_in_player(self, path: str):
        try:
            open_player(path)
        except Exception as exc:                  # noqa: BLE001 - say so in the window instead of failing silently
            print(f"[player] {exc!r}", file=sys.stderr)
            self.status.setText(f"פתיחת הנגן נכשלה: {exc}")

    def _open_local(self):
        folder = Path.home() / "Videos" / "CameraRecordings"
        path, _ = QFileDialog.getOpenFileName(
            self, "בחר קובץ וידאו", str(folder if folder.exists() else Path.home()),
            "וידאו (*.mp4 *.avi *.mkv *.mov);;כל הקבצים (*.*)")
        if path:
            self._open_in_player(path)

    def _dl_reset(self):
        self.dl_btn.setText("⬇ הורד קובץ")
        self.dl_bar.setVisible(False)

    def _on_dl_stage(self, text: str):
        self.dl_bar.setRange(0, 0)                  # busy indicator while converting
        self.status.setText(text)

    def _on_dl_progress(self, got: int, expected: int):
        if expected > 0 and got <= expected * 1.3:
            self.dl_bar.setRange(0, 100)
            self.dl_bar.setValue(min(99, int(got * 100 / expected)))
        self.status.setText(f"מוריד... {recfmt.fmt_size(got)}" +
                            (f" מתוך כ-{recfmt.fmt_size(expected)}" if expected else ""))

    def _on_dl_done(self, path: str, note: str):
        self._dl_reset()
        self.status.setText(f"נשמר: {path}" + (f". {note}" if note else ""))
        if path.lower().endswith(".mp4"):
            self.status.setText(f"נשמר: {path}. אפשר לפתוח אותו בנגן עם 'פתח קובץ בנגן'")

    def _on_dl_failed(self, message: str):
        self._dl_reset()
        self.status.setText(message)

    def _play(self):
        rec = self._selected_record()
        if rec is None:
            self.status.setText("בחר הקלטה מהרשימה")
            return
        self._stop()
        self._player = _PlayWorker(self.cfg, self.channel_combo.currentData(), rec, stream_type=self.quality_combo.currentData())
        self._player.audio_enabled = self.sound_box.isChecked()
        self._player.max_width = 1600 if self._fullscreen else 960
        self._player.frame.connect(self._on_frame)
        self._player.audio.connect(self._on_audio)
        self._player.message.connect(self.status.setText)
        self._player.start()

    def _stop(self):
        if self._player is not None:
            self._player.stop()
            self._player = None
        self._audio.stop()

    def _on_frame(self, img):
        pix = QPixmap.fromImage(img).scaled(self.video.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.FastTransformation)
        self.video.setPixmap(pix)
        if self._player is not None:
            self._player.pending = False

    def _on_audio(self, pcm: bytes, rate: int):
        self._audio.write(pcm, rate)

    def _on_sound(self, on: bool):
        if self._player is not None:
            self._player.audio_enabled = on
        if not on:
            self._audio.stop()

    # ---- full screen ----------------------------------------------------
    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        on = self._fullscreen
        self.top_bar.setVisible(not on)
        self.files.setVisible(not on)
        self.full_btn.setText("⛶ חזרה" if on else "⛶ מסך מלא")
        if self._player is not None:
            self._player.max_width = 1600 if on else 960     # sharper picture on a big screen
        if on:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        else:
            self.showMaximized() if self._was_maximized else self.showNormal()

    def mouseDoubleClickEvent(self, event):
        if self.video.geometry().contains(event.position().toPoint()):
            self._toggle_fullscreen()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._fullscreen:
            self._toggle_fullscreen()          # Esc leaves full screen instead of closing the window
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._download is not None and self._download.isRunning():
            self._download.cancel()
            self._download.wait(3000)
        self._stop()
        self._cancel_query()
        super().closeEvent(event)
