"""Live events from a DVRIP recorder (motion, video loss, disk problems...) - what the "log" list at the bottom of
the manufacturer's CMS shows. After login the recorder is asked for alarms (message 1500) and pushes an
AlarmInfo packet (1504) for each event. One watcher thread per recorder; it reconnects by itself.

Also: query_history() asks the recorder for its stored log (OPLogQuery, message 1442). The reply format differs
between firmwares, so the parser is tolerant and the raw text is returned too."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from datetime import datetime

from app.core import dvrip

MSG_ALARM_SUBSCRIBE = 1500
MSG_ALARM_INFO = 1504
MSG_LOG_QUERY = 1442

EVENT_TEXT = {
    "VideoMotion": "תנועה", "VideoLoss": "אובדן וידאו", "VideoBlind": "הסתרת מצלמה",
    "HumanDetect": "זיהוי אדם", "AlarmLocal": "אזעקה", "AlarmNet": "אזעקת רשת",
    "StorageNotExist": "אין כונן", "StorageFailure": "תקלת כונן", "StorageLowSpace": "כונן כמעט מלא",
    "Motion": "תנועה", "NetAbort": "ניתוק רשת", "NetAbortExtra": "ניתוק רשת", "IPConflict": "התנגשות IP",
    "LoginFailed": "כניסה כושלת", "VideoAnalyze": "ניתוח וידאו", "PerimeterDetection": "חציית גבול",
}


def event_text(name: str) -> str:
    """Hebrew name of an event. Some firmwares send 'appEventHumanDetectAlarm' for what others call 'HumanDetect'."""
    if name in EVENT_TEXT:
        return EVENT_TEXT[name]
    core = name
    if core.startswith("appEvent"):
        core = core[len("appEvent"):]
    if core.endswith("Alarm") and len(core) > len("Alarm"):
        core = core[:-len("Alarm")]
    return EVENT_TEXT.get(core, name or "אירוע")


class AlarmWatcher:
    def __init__(self, host: str, port: int, username: str, password: str, label: str, on_event, on_state=None):
        self.host, self.port, self.username, self.password = host, port, username, password
        self.label = label
        self.on_event = on_event            # callable(dict) from the watcher thread
        self.on_state = on_state            # callable(str) - connection state text
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"alarms-{host}")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _state(self, text: str):
        if self.on_state:
            try:
                self.on_state(f"{self.label}: {text}")
            except Exception:  # noqa: BLE001
                pass

    def _run(self):
        delay = 3.0
        while not self._stop.is_set():
            c = dvrip.DVRIPClient(self.host, self.port, self.username, self.password, timeout=4.0)
            try:
                c.connect()
                c.login()
                c._send(c.sock, MSG_ALARM_SUBSCRIBE, {"Name": "", "SessionID": c._sid()})
                self._state("מאזין לאירועים")
                delay = 3.0
                self._listen(c)
            except dvrip.DVRIPAuthError as exc:
                self._state(str(exc))
                delay = 60.0
            except (OSError, dvrip.DVRIPError) as exc:
                print(f"[alarms] {self.host}: {exc!r}", file=sys.stderr)
                self._state("אין חיבור, מנסה שוב")
                delay = min(delay * 2, 30.0)
            finally:
                c.close()
            self._stop.wait(delay)

    def _listen(self, c: dvrip.DVRIPClient):
        while not self._stop.is_set():
            try:
                mid, _s, _q, payload = c._recv_packet(c.sock)
            except socket.timeout:
                c.keepalive_if_due()
                continue
            c.keepalive_if_due()
            if mid != MSG_ALARM_INFO:
                continue
            data = dvrip._parse_json(payload)
            info = data.get("AlarmInfo") or {}
            if not isinstance(info, dict):
                continue
            event = str(info.get("Event", ""))
            status = str(info.get("Status", ""))
            stamp = str(info.get("StartTime") or "") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                channel = int(info.get("Channel", 0))
            except (TypeError, ValueError):
                channel = 0
            self.on_event({"time": stamp, "event": event, "type": event_text(event), "status": status,
                           "channel": channel, "host": self.host, "device": self.label, "user": "",
                           "text": f"ערוץ {channel + 1} {event_text(event)}" + (" (הסתיים)" if status == "Stop" else "")})


def query_history(host: str, port: int, username: str, password: str, begin: str, end: str,
                  log_type: str = "LogAll") -> tuple[list[dict], str]:
    """Stored log of the recorder between begin and end ('YYYY-MM-DD HH:MM:SS'). Returns (rows, raw_text)."""
    c = dvrip.DVRIPClient(host, port, username, password, timeout=8.0)
    rows: list[dict] = []
    raw = ""
    try:
        c.connect()
        c.login()
        pos = 0
        for _page in range(30):
            c._send(c.sock, MSG_LOG_QUERY, {"Name": "OPLogQuery", "SessionID": c._sid(),
                                            "OPLogQuery": {"BeginTime": begin, "EndTime": end,
                                                           "LogPosition": pos, "Type": log_type}})
            data: dict = {}
            try:
                for _try in range(5):
                    _m, _s, _q, payload = c._recv_packet(c.sock)
                    data = dvrip._parse_json(payload)
                    if "OPLogQuery" in data or data.get("Name") == "OPLogQuery":
                        break
            except socket.timeout:
                break
            if not raw:
                raw = json.dumps(data, ensure_ascii=False)[:400]
            items = data.get("OPLogQuery")
            if not isinstance(items, list) or not items:
                break
            for it in items:
                if not isinstance(it, dict):
                    continue
                kind = str(it.get("Type", ""))
                text = str(it.get("Data", "") or it.get("Content", "") or "")
                rows.append({"time": str(it.get("Time", "")), "event": kind, "type": event_text(kind),
                             "status": "", "channel": None, "host": host, "device": host,
                             "user": str(it.get("User", "")), "text": text})
            if len(items) < 8:
                break
            pos += len(items)
    finally:
        c.close()
    rows.sort(key=lambda r: r["time"], reverse=True)
    return rows, raw
