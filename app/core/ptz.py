"""PTZ (pan / tilt / zoom / focus / iris / presets) over the DVRIP protocol (XM / Provision, port 34567).

The commands go over their OWN connection to the recorder (the video connections stay untouched). One controller
per recorder; a worker thread owns the socket, so the window never waits for the network. The connection is
opened on the first command and closed again after a while without use (the NVR has a limited number of sessions).

Message OPPTZControl (1400): a movement starts with Preset -1 and ends with the same command with Preset 65535.
Whether a camera really moves depends on the camera: the recorder answers Ret=100 even for some fixed cameras,
and a refusal comes back as another code - that text is handed to on_result so the window can show it."""
from __future__ import annotations

import queue
import select
import socket
import sys
import threading
import time

from app.core import dvrip

MSG_PTZ = 1400
IDLE_CLOSE = 45.0          # seconds without a command before the connection is closed

DIRECTIONS = {
    "up": "DirectionUp", "down": "DirectionDown", "left": "DirectionLeft", "right": "DirectionRight",
    "up_left": "DirectionLeftUp", "up_right": "DirectionRightUp",
    "down_left": "DirectionLeftDown", "down_right": "DirectionRightDown",
}
LENS = {"zoom_in": "ZoomTile", "zoom_out": "ZoomWide", "focus_far": "FocusFar", "focus_near": "FocusNear",
        "iris_open": "IrisLarge", "iris_close": "IrisSmall"}

_OK_CODES = (100, 101, 102, 515)
_RET_TEXT = {203: "שם משתמש/סיסמה שגויים", 205: "שם משתמש/סיסמה שגויים", 607: "אין הרשאה לשליטה במצלמה"}


STOP_MODES = {
    "std": "סטנדרטית",          # same command with Preset 65535 (what most recorders expect)
    "aux_off": "עם AUX כבוי",   # as above, and the AUX flag switched off
    "step0": "מהירות 0",        # a new "move" with speed 0
}


def _params(channel: int, step: int, preset: int, cmd: str, aux: str = "On") -> dict:
    return {"AUX": {"Number": 0, "Status": aux}, "Channel": channel, "MenuOpts": "Enter",
            "POINT": {"bottom": 0, "left": 0, "right": 0, "top": 0}, "Pattern": "SetBegin",
            "Preset": preset, "Step": step, "Tour": 1 if "Tour" in cmd else 0}


class PtzController:
    _pool: dict[tuple, "PtzController"] = {}
    _pool_lock = threading.Lock()

    @classmethod
    def for_cfg(cls, cfg) -> "PtzController":
        key = (cfg.host, cfg.port, cfg.username)
        with cls._pool_lock:
            ctl = cls._pool.get(key)
            if ctl is None:
                ctl = cls._pool[key] = cls(cfg.host, cfg.port, cfg.username, cfg.password)
            return ctl

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host, self.port, self.username, self.password = host, port, username, password
        self.on_result = None                      # callable(ok: bool, text: str), called from the worker thread
        self._q: queue.Queue = queue.Queue()
        self._client: dvrip.DVRIPClient | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._want_ok = False
        self.stop_mode = "std"

    # ---- public (non-blocking) --------------------------------------------------
    def send(self, channel: int, command: str, step: int = 5, preset: int = -1, aux: str = "On"):
        self._q.put((channel, command, int(step), int(preset), aux))
        self._ensure_thread()

    def move(self, channel: int, name: str, speed: int = 5):
        """Start moving / zooming / focusing; call stop() with the same name when the button is released."""
        cmd = DIRECTIONS.get(name) or LENS.get(name)
        if cmd:
            self.send(channel, cmd, speed, -1)

    def stop(self, channel: int, name: str, speed: int = 5):
        """End a movement. A stop that the recorder or camera misses leaves the camera running to the end of its
        range, so it is sent three times (now, +0.15 s, +0.4 s); repeating a stop is harmless."""
        cmd = DIRECTIONS.get(name) or LENS.get(name)
        if not cmd:
            return
        self._send_stop(channel, cmd, speed)
        for delay in (0.15, 0.4):
            t = threading.Timer(delay, self._send_stop, (channel, cmd, speed))
            t.daemon = True
            t.start()

    def _send_stop(self, channel: int, cmd: str, speed: int):
        mode = self.stop_mode
        if mode == "step0":
            self.send(channel, cmd, 0, -1)
        else:
            self.send(channel, cmd, speed, 65535, aux="Off" if mode == "aux_off" else "On")

    def warm_up(self):
        """Open the connection now, so the first click is not delayed by connecting (a start and a stop that wait
        together reach the recorder at the same instant and the stop can be lost)."""
        self._q.put(None)
        self._ensure_thread()

    def goto_preset(self, channel: int, number: int):
        self.send(channel, "GotoPreset", 5, number)

    def set_preset(self, channel: int, number: int):
        self.send(channel, "SetPreset", 5, number)

    # ---- worker --------------------------------------------------------------------
    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name="ptz")
                self._thread.start()

    def _report(self, ok: bool, text: str):
        cb = self.on_result
        if cb is not None:
            try:
                cb(ok, text)
            except Exception:  # noqa: BLE001
                pass

    def _connect(self):
        c = dvrip.DVRIPClient(self.host, self.port, self.username, self.password, timeout=6.0)
        try:
            c.connect()
            c.login()
        except BaseException:
            c.close()
            raise
        self._client = c

    def _drop(self):
        c, self._client = self._client, None
        if c is not None:
            c.close()

    def _run(self):
        """Commands are sent at once and never wait for the recorder's answer (a slow answer used to delay the
        "stop" and the camera kept moving). Answers are collected between commands."""
        last_used = time.monotonic()
        while True:
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                try:
                    if self._client is not None:
                        self._poll_replies()
                        self._client.keepalive_if_due()
                except (OSError, dvrip.DVRIPError):
                    self._drop()
                if time.monotonic() - last_used > IDLE_CLOSE:
                    self._drop()
                    return
                continue
            last_used = time.monotonic()
            try:
                if self._client is None:
                    self._connect()
                if item is not None:
                    self._deliver(*item)
            except dvrip.DVRIPAuthError as exc:
                self._drop()
                self._report(False, str(exc))
            except (OSError, dvrip.DVRIPError) as exc:
                self._drop()
                print(f"[ptz] {self.host}: {exc!r}", file=sys.stderr)
                self._report(False, f"אין חיבור ל-NVR ({exc})")

    def _deliver(self, channel: int, cmd: str, step: int, preset: int, aux: str = "On"):
        c = self._client
        print(f"[ptz] -> {cmd} step={step} preset={preset} aux={aux}", file=sys.stderr)
        # a command that is not a plain movement (preset, tour) gets its answer reported as success too
        self._want_ok = preset not in (-1, 65535)
        c._send(c.sock, MSG_PTZ, {"Name": "OPPTZControl", "SessionID": c._sid(),
                                  "OPPTZControl": {"Command": cmd, "Parameter": _params(channel, step, preset, cmd, aux)}})
        if self._q.empty():
            self._poll_replies(0.05)

    def _poll_replies(self, first_wait: float = 0.0):
        """Read the answers that already arrived (never blocks for long)."""
        c = self._client
        wait = first_wait
        for _ in range(8):
            if not select.select([c.sock], [], [], wait)[0]:
                return
            wait = 0.0
            c.sock.settimeout(1.0)
            try:
                _mid, _s, _q, payload = c._recv_packet(c.sock)
            finally:
                try:
                    c.sock.settimeout(c.timeout)
                except OSError:
                    pass
            data = dvrip._parse_json(payload)
            if data.get("Name") == "OPPTZControl" or "OPPTZControl" in data:
                self._handle_ret(data.get("Ret"))

    def _handle_ret(self, ret):
        print(f"[ptz] <- Ret={ret}", file=sys.stderr)
        ok = ret in _OK_CODES or ret is None
        if ok:
            if self._want_ok:
                self._report(True, "הפקודה נשלחה")
        else:
            self._report(False, _RET_TEXT.get(ret) or f"ה-NVR דחה את הפקודה (קוד {ret}) - ייתכן שהמצלמה לא תומכת בכך")
