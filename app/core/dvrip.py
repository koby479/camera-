"""
Client for the "DVRIP / Sofia / NetSurveillance" protocol spoken by Xiongmai (XM)
based DVR/NVR/XVR boxes (many cheap Chinese recorders, and Provision-ISR units
that are rebadged XM firmware), normally on TCP port 34567. This is what CMS
software such as Provision CMS3 / XMeye uses -- it is NOT ONVIF and NOT RTSP.

Implemented from the publicly known protocol description:
  * 20-byte header:  0xFF 0x00 <2 pad> <session u32> <seq u32> <2 pad> <msgid u16> <len u32>  (little endian)
  * body: JSON terminated by 0x0A 0x00
  * login = msg 1000 with the "Sofia hash" of the password; reply msg 1001
  * live video = OPMonitor Claim (msg 1413) on the control connection, then
    OPMonitor Start (msg 1410) -> device streams msg 1412 packets whose payload
    is a stream of XM frames (00 00 01 FC = I-frame, 00 00 01 FD = P-frame,
    00 00 01 FA = audio) carrying H.264 in Annex-B form.

Audio frames (G.711 A-law) are collected in XMFrameParser.audio; H.264 decoding is done elsewhere.
"""
from __future__ import annotations
from datetime import datetime, timedelta

import hashlib
import json
import select
import socket
import string
import struct
import time

import numpy as np

from app.core.rtsp_auth import AuthResult

HEADER = struct.Struct("<BB2xII2xHI")   # 20 bytes
assert HEADER.size == 20

MSG_LOGIN = 1000
MSG_KEEPALIVE = 1006
MSG_MONITOR_START = 1410
MSG_MONITOR_DATA = 1412
MSG_MONITOR_CLAIM = 1413
MSG_PLAY_START = 1420
MSG_PLAY_DATA = 1422
MSG_PLAY_EOF = 1423
MSG_PLAY_CLAIM = 1424
MSG_DOWNLOAD_DATA = 1426
MSG_FILE_QUERY = 1440
MEDIA_MSGS = {MSG_MONITOR_DATA, MSG_PLAY_DATA, MSG_DOWNLOAD_DATA}

# Return codes that mean "username/password not accepted"
BAD_CREDENTIAL_CODES = {106, 203, 205, 206}

# audio frame header: 00 00 01 FA <media u8> <rate-code u8> <len u16>
AUDIO_RATES = {1: 4000, 2: 8000, 3: 11025, 4: 16000, 5: 20000, 6: 22050,
               7: 32000, 8: 44100, 9: 48000}


def alaw_to_pcm16(data: bytes) -> bytes:
    """G.711 A-law -> signed 16-bit little-endian PCM (mono)."""
    a = np.frombuffer(data, dtype=np.uint8) ^ 0x55
    seg = ((a & 0x70) >> 4).astype(np.int32)
    t = ((a & 0x0F).astype(np.int32) << 4)
    t = np.where(seg == 0, t + 8, (t + 0x108) << np.maximum(seg - 1, 0))
    pcm = np.where(a & 0x80, t, -t).astype(np.int16)
    return pcm.tobytes()


_CHARS = string.digits + string.ascii_uppercase + string.ascii_lowercase


def sofia_hash(password: str) -> str:
    """XM's 8-character password hash (md5 folded byte-pairs -> alphabet)."""
    md5 = hashlib.md5(password.encode("utf-8")).digest()
    return "".join(_CHARS[(a + b) % len(_CHARS)] for a, b in zip(md5[::2], md5[1::2]))


class DVRIPError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class DVRIPAuthError(DVRIPError):
    """Device answered but rejected the username/password."""


def _parse_json(payload: bytes) -> dict:
    text = payload.rstrip(b"\x00\n\r ").decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return {}


class DVRIPClient:
    def __init__(self, host: str, port: int = 34567, username: str = "",
                 password: str = "", timeout: float = 6.0):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.media_sock: socket.socket | None = None
        self.session = 0
        self.seq = 0
        self.alive_interval = 20
        self.info: dict = {}
        self.media_msgids: set[int] = set()     # message ids seen while streaming (diagnostics)
        self.replied_ok = False                 # the NVR has answered at least one file query
        self.needs_reconnect = False            # a query got no answer: a late reply may still arrive
        self.trace: list[str] = []              # raw-ish description of file-query replies (diagnostics)
        self._pending: list[bytes] = []
        self._last_keepalive = time.monotonic()

    # ---- low level -------------------------------------------------------
    def _connect(self) -> socket.socket:
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        return s

    def _send(self, sock: socket.socket, msgid: int, obj: dict):
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\x0a\x00"
        self.seq += 1
        sock.sendall(HEADER.pack(0xFF, 0, self.session, self.seq, msgid, len(payload)) + payload)

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int, mid_packet: bool = False, patience: float = 60.0) -> bytes:
        """Read exactly n bytes. A timeout with nothing read yet is re-raised (the caller may poll
        again), but once some bytes of a packet were read (or mid_packet is set) we keep waiting:
        dropping a half-read packet would silently cut bytes out of the video stream."""
        buf = bytearray()
        deadline = time.monotonic() + patience
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except socket.timeout:
                if (buf or mid_packet) and time.monotonic() < deadline:
                    continue
                raise
            if not chunk:
                raise ConnectionError("connection closed by device")
            buf += chunk
        return bytes(buf)

    def _recv_packet(self, sock: socket.socket):
        head = self._recv_exact(sock, HEADER.size)
        magic, _ver, session, seq, msgid, length = HEADER.unpack(head)
        if magic != 0xFF:
            raise DVRIPError(f"not a DVRIP device (bad header byte 0x{magic:02X})")
        if length > 16 * 1024 * 1024:
            raise DVRIPError("absurd packet length - protocol mismatch")
        payload = self._recv_exact(sock, length, mid_packet=True) if length else b""
        return msgid, session, seq, payload

    def _sid(self) -> str:
        return "0x%08X" % self.session

    # ---- public API ------------------------------------------------------
    def connect(self):
        self.sock = self._connect()

    def close(self):
        for s in (self.media_sock, self.sock):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self.sock = self.media_sock = None

    def login(self) -> dict:
        """Returns the device's login reply (has ChannelNum, DeviceType...).
        Raises DVRIPAuthError for a wrong user/password, DVRIPError otherwise."""
        if self.sock is None:
            self.connect()
        self._send(self.sock, MSG_LOGIN, {
            "EncryptType": "MD5",
            "LoginType": "DVRIP-Web",
            "PassWord": sofia_hash(self.password),
            "UserName": self.username,
        })
        _msgid, _sess, _seq, payload = self._recv_packet(self.sock)
        info = _parse_json(payload)
        ret = info.get("Ret")
        if ret in (100, 515):
            self.session = int(str(info.get("SessionID", "0x0")), 16)
            self.alive_interval = int(info.get("AliveInterval", 20) or 20)
            self.info = info
            return info
        if ret in BAD_CREDENTIAL_CODES:
            raise DVRIPAuthError(f"שם משתמש/סיסמה שגויים (קוד {ret})", ret)
        raise DVRIPError(f"ההתחברות נדחתה, קוד {ret}: {info}", ret)

    def keepalive_if_due(self):
        """Send a keepalive WITHOUT waiting for its reply: this is called from the video loop, so
        any wait here is a visible freeze. When video flows on this same socket, the reply simply
        arrives in-band and read_video_payloads() ignores it (it is not a media packet). When video
        has its own socket, the reply is picked up from the control socket without blocking."""
        now = time.monotonic()
        if not self.sock or now - self._last_keepalive < max(5, self.alive_interval / 2):
            return
        self._last_keepalive = now
        self._send(self.sock, MSG_KEEPALIVE, {"Name": "KeepAlive", "SessionID": self._sid()})
        if self.media_sock is not None and self.media_sock is not self.sock:
            self._drain_control()

    def _drain_control(self):
        """Throw away whatever the device already sent on the control socket (keepalive replies)
        so its buffer never fills up. Never waits for data that has not arrived yet."""
        sock = self.sock
        try:
            while select.select([sock], [], [], 0)[0]:
                sock.settimeout(0.5)               # a reply that has started is tiny: it completes at once
                self._recv_packet(sock)
        except (OSError, DVRIPError):
            pass
        finally:
            try:
                sock.settimeout(self.timeout)
            except OSError:
                pass

    def _monitor_params(self, channel: int, stream: str) -> dict:
        return {"Channel": channel, "CombinMode": "NONE", "StreamType": stream, "TransMode": "TCP"}

    def _open_stream(self, claim_msg, claim, start_msg, start, first_data_timeout, what):
        """Claim on the control connection, Start on a second connection and, if the
        device sends nothing, Start again on the control connection."""
        self._send(self.sock, claim_msg, claim)
        try:
            _m, _s, _q, payload = self._recv_packet(self.sock)
            ret = _parse_json(payload).get("Ret", 100)
            if ret != 100:
                raise DVRIPError(f"ה-NVR דחה בקשת {what} (קוד {ret})", ret)
        except socket.timeout:
            pass

        # layout A: separate media connection (some NVRs stop answering extra connections
        # when many streams are already open - then fall through to layout B)
        try:
            self.media_sock = self._connect()
            self._send(self.media_sock, start_msg, start)
            if self._wait_for_data(self.media_sock, first_data_timeout):
                return
        except OSError:
            pass
        if self.media_sock is not None and self.media_sock is not self.sock:
            try:
                self.media_sock.close()
            except OSError:
                pass

        # layout B: same connection
        self.media_sock = self.sock
        self._send(self.sock, start_msg, start)
        if self._wait_for_data(self.sock, first_data_timeout):
            return
        raise DVRIPError(f"ה-NVR אישר את ההתחברות אבל לא שלח {what}")

    def start_monitor(self, channel: int = 0, stream: str = "Main", first_data_timeout: float = 6.0):
        """Open a live-video stream."""
        params = self._monitor_params(channel, stream)
        claim = {"Name": "OPMonitor", "SessionID": self._sid(),
                 "OPMonitor": {"Action": "Claim", "Parameter": params}}
        start = {"Name": "OPMonitor", "SessionID": self._sid(),
                 "OPMonitor": {"Action": "Start", "Parameter": params}}
        self._open_stream(MSG_MONITOR_CLAIM, claim, MSG_MONITOR_START, start,
                          first_data_timeout, "וידאו")

    # ---- recordings ------------------------------------------------------
    def query_files(self, channel: int, begin: str, end: str, file_type: str = "h264",
                    variant: dict | None = None) -> list[dict]:
        """List recorded files. begin/end are 'YYYY-MM-DD HH:MM:SS'. Returns dicts with
        name, begin, end, size (bytes). The device returns at most ~64 per reply, so
        keep asking from the end of the last file."""
        out: list[dict] = []
        seen = set()
        cur = begin
        page_size = 64                       # the device sends at most 64 files per reply
        for page in range(40):
            q = {"BeginTime": cur, "Channel": channel,
                 "DriverTypeMask": "0x0000FFFF", "EndTime": end,
                 "Event": "*", "StreamType": "0x00000000", "Type": file_type}
            if variant:                      # alternative parameter sets some firmwares want
                q.update(variant)
            self._send(self.sock, MSG_FILE_QUERY, {
                "Name": "OPFileQuery", "SessionID": self._sid(), "OPFileQuery": q})
            data: dict = {}
            try:
                for _try in range(6):
                    _m, _s, _q, payload = self._recv_packet(self.sock)
                    data = _parse_json(payload)
                    if "OPFileQuery" in data or data.get("Name") == "OPFileQuery":
                        break
            except socket.timeout:
                # Some firmwares simply do not answer when there is nothing (more) to list.
                # Keep what was already read instead of throwing it away.
                self.needs_reconnect = True
                if out:
                    self.trace.append(f"page {page + 1}: no answer, keeping {len(out)} files")
                    break
                if self.replied_ok:
                    self.trace.append("no answer at all (this NVR answered other windows -> treated as empty)")
                    break
                raise
            self.replied_ok = True
            ret = data.get("Ret", 100)
            items = data.get("OPFileQuery") or []
            if not isinstance(items, list):
                items = []
            desc = f"Ret={ret} keys={sorted(data)[:8]} items={len(items)}"
            if not items:                    # an empty answer: keep the raw text, it shows what the NVR meant
                desc += f" raw={json.dumps(data, ensure_ascii=False)[:300]}"
            self.trace.append(desc)
            del self.trace[:-20]
            if ret != 100:
                raise DVRIPError(f"שאילתת ההקלטות נדחתה (קוד {ret})", ret)
            new = 0
            for it in items:
                key = (it.get("FileName"), it.get("BeginTime"))
                if key in seen:
                    continue
                seen.add(key)
                new += 1
                size = it.get("FileLength", 0)
                try:
                    size = int(size, 16) if isinstance(size, str) else int(size)
                except ValueError:
                    size = 0
                size *= 1024                 # FileLength is in kilobytes on these devices
                out.append({"name": it.get("FileName", ""), "begin": it.get("BeginTime", ""),
                            "end": it.get("EndTime", ""), "size": size})
            if not items or not new or len(items) < page_size:
                break                        # a short page is the last page: do not ask again
            cur = items[-1].get("EndTime", end)
        out.sort(key=lambda f: f["begin"])
        return out

    def query_files_chunked(self, channel: int, begin: str, end: str, chunk_hours: int = 2,
                            chunk_timeout: float = 10.0, progress=None, chunk_minutes: int | None = None,
                            variant: dict | None = None, cancelled=None,
                            on_part=None) -> tuple[list[dict], list[str]]:
        """Like query_files, but asks for short time windows one at a time. A busy channel can
        make the NVR sit on one huge day-long query until it times out; small windows answer
        quickly, and a window that fails does not throw away the ones that worked.

        Returns (files, failed_windows). progress(window_label, count_or_None, seconds, error)
        is called after each window. Raises only when nothing at all could be read."""
        fmt = "%Y-%m-%d %H:%M:%S"
        start, stop = datetime.strptime(begin, fmt), datetime.strptime(end, fmt)
        files: list[dict] = []
        seen: set = set()
        failed: list[str] = []
        consecutive = 0
        last_error = ""
        self.timeout = chunk_timeout
        if self.sock:
            self.sock.settimeout(chunk_timeout)
        step = timedelta(minutes=chunk_minutes) if chunk_minutes else timedelta(hours=chunk_hours)
        cur = start
        while cur < stop:
            if cancelled and cancelled():
                break
            if cur > datetime.now() + timedelta(hours=1):
                break                               # windows in the future cannot hold recordings
            if self.sock:                           # a NVR that already answered fast is not left waiting long
                self.sock.settimeout(min(chunk_timeout, 3.0) if (self.replied_ok and step <= timedelta(hours=2))
                                     else chunk_timeout)
            nxt = min(cur + step, stop)
            label = f"{cur:%H:%M}-{nxt:%H:%M}"
            t0 = time.monotonic()
            try:
                part = self.query_files(channel, cur.strftime(fmt), nxt.strftime(fmt), variant=variant)
                consecutive = 0
                if self.needs_reconnect:            # tolerated silence: reset the connection for the next window
                    self.needs_reconnect = False
                    try:
                        self.close()
                        self.connect()
                        self.sock.settimeout(chunk_timeout)
                        self.login()
                    except (OSError, DVRIPError):
                        break
                fresh = []
                for f in part:
                    key = (f["name"], f["begin"])
                    if key not in seen:
                        seen.add(key)
                        files.append(f)
                        fresh.append(f)
                if fresh and on_part:
                    on_part(fresh)          # show results while the search is still running
                if progress:
                    progress(label, len(part), time.monotonic() - t0, "")
            except DVRIPAuthError:
                raise
            except (OSError, DVRIPError) as exc:
                consecutive += 1
                last_error = str(exc) or type(exc).__name__
                failed.append(label)
                if progress:
                    progress(label, None, time.monotonic() - t0, last_error)
                if consecutive >= 3:
                    break                       # the NVR is not answering this channel at all
                try:                            # a late reply would confuse the next window
                    self.close()
                    self.connect()
                    self.sock.settimeout(chunk_timeout)
                    self.login()
                except (OSError, DVRIPError):
                    break
            cur = nxt
        if not files and failed and not (cancelled and cancelled()):
            raise DVRIPError(f"ה-NVR לא ענה לשאילתת ההקלטות ({last_error})")
        files.sort(key=lambda f: f["begin"])
        return files, failed

    def start_playback(self, channel: int, item: dict, first_data_timeout: float = 8.0, stream_type: int = 0):
        """Play one recorded file (item from query_files). Read the result with
        read_video_payloads(); the generator ends when the device signals EOF."""
        param = {"FileName": item["name"], "PlayMode": "ByName", "StreamType": stream_type,
                 "TransMode": "TCP", "Channel": channel, "Value": 0}
        def body(action):
            return {"Name": "OPPlayBack", "SessionID": self._sid(),
                    "OPPlayBack": {"Action": action, "StartTime": item["begin"],
                                   "EndTime": item["end"], "Parameter": param}}
        self._open_stream(MSG_PLAY_CLAIM, body("Claim"), MSG_PLAY_START, body("Start"),
                          first_data_timeout, "הקלטה")

    def start_download(self, channel: int, item: dict, first_data_timeout: float = 12.0, stream_type: int = 0):
        """Ask the NVR to send one recorded file as a download (msg 1426 packets)."""
        param = {"FileName": item["name"], "PlayMode": "ByName", "StreamType": stream_type,
                 "TransMode": "TCP", "Channel": channel, "Value": 0}
        def body(action):
            return {"Name": "OPPlayBack", "SessionID": self._sid(),
                    "OPPlayBack": {"Action": action, "StartTime": item["begin"],
                                   "EndTime": item["end"], "Parameter": param}}
        self._open_stream(MSG_PLAY_CLAIM, body("Claim"), MSG_PLAY_START, body("DownloadStart"),
                          first_data_timeout, "הורדה")

    def read_download_payloads(self, idle_timeout: float = 3.0, max_silent: int = 10):
        """Like read_video_payloads, but an empty data packet (or EOF) ends the transfer."""
        for p in self._pending:
            if p:
                yield p
        self._pending = []
        sock = self.media_sock
        sock.settimeout(idle_timeout)
        silent = 0
        while True:
            try:
                msgid, _s, _q, payload = self._recv_packet(sock)
            except socket.timeout:
                silent += 1
                if silent >= max_silent:
                    raise DVRIPError("ה-NVR הפסיק לשלוח את הקובץ")
                yield b""                  # lets the caller check its cancel flag
                continue
            silent = 0
            if msgid in MEDIA_MSGS:
                if not payload:
                    return
                yield payload
            elif msgid == MSG_PLAY_EOF:
                return

    def _wait_for_data(self, sock: socket.socket, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        self._pending: list[bytes] = []
        sock.settimeout(1.0)
        try:
            while time.monotonic() < deadline:
                try:
                    msgid, _s, _q, payload = self._recv_packet(sock)
                except socket.timeout:
                    continue
                self.media_msgids.add(msgid)
                if msgid in MEDIA_MSGS:
                    self._pending.append(payload)
                    return True
                ret = _parse_json(payload).get("Ret")
                if ret not in (None, 100):
                    raise DVRIPError(f"שגיאה בהפעלת וידאו (קוד {ret})", ret)
            return False
        finally:
            sock.settimeout(self.timeout)

    def read_video_payloads(self, idle_timeout: float = 2.0):
        """Generator of raw XM stream payload chunks (msg 1412). Ends on
        connection loss; raises socket.timeout-derived DVRIPError if the
        device goes silent for longer than idle_timeout * 5."""
        for p in self._pending:
            yield p
        self._pending = []
        sock = self.media_sock
        sock.settimeout(idle_timeout)
        silent = 0
        while True:
            try:
                msgid, _s, _q, payload = self._recv_packet(sock)
            except socket.timeout:
                silent += 1
                if silent >= 5:
                    raise DVRIPError("ה-NVR הפסיק לשלוח וידאו")
                yield b""          # let the caller check its stop flag
                continue
            silent = 0
            self.media_msgids.add(msgid)
            if msgid in MEDIA_MSGS:
                yield payload
            elif msgid == MSG_PLAY_EOF:
                return


class XMFrameParser:
    """Splits the XM payload byte-stream into H.264 (Annex-B) video frames."""

    _TYPES = (0xFC, 0xFD, 0xFA, 0xF9)

    def __init__(self):
        self.buf = bytearray()
        # audio frames seen since the caller last drained this list:
        # (media_type, sample_rate_hz, raw_payload)
        self.audio: list[tuple[int, int, bytes]] = []
        self.fps_hint: int | None = None     # frame rate written in the last key-frame header (1..60)
        self.embedded = 0     # frame headers found inside another frame's data (and removed)
        self.dropped = 0      # bytes skipped because they belonged to no frame (diagnostics)

    def _find_start(self) -> int:
        b = self.buf
        i = b.find(b"\x00\x00\x01")
        while i != -1:
            if i + 3 < len(b):
                if b[i + 3] in self._TYPES:
                    return i
                i = b.find(b"\x00\x00\x01", i + 1)
            else:
                return -2       # need more bytes to decide
        return -1

    @staticmethod
    def _header(b, i: int):
        """(header_len, payload_len) of the XM frame header at b[i:], or None if too short."""
        t = b[i + 3]
        if t == 0xFC:
            if len(b) < i + 16:
                return None
            return 16, int.from_bytes(b[i + 12:i + 16], "little")
        if t == 0xFD:
            if len(b) < i + 8:
                return None
            return 8, int.from_bytes(b[i + 4:i + 8], "little")
        if len(b) < i + 8:                          # audio (0xFA) / info (0xF9)
            return None
        return 8, int.from_bytes(b[i + 6:i + 8], "little")

    def _split_embedded(self, body: bytes) -> list[bytes]:
        """The NVR sometimes declares a frame length that also covers the frames that follow it, so
        their XM headers end up inside the video data. HEVC/H.264 payloads can never contain
        00 00 01 FC/FD/FA/F9 (0xFD.. is an invalid NAL header, and 00 00 01 is escaped), so a
        header that fits exactly inside the body is a real frame: cut it out."""
        out: list[bytes] = []
        start = pos = 0
        while True:
            j = body.find(b"\x00\x00\x01", pos)
            if j == -1 or j + 4 > len(body):
                break
            if body[j + 3] not in self._TYPES:
                pos = j + 1
                continue
            h = self._header(body, j)
            if h is None or j + h[0] + h[1] > len(body):
                pos = j + 1
                continue
            hdr, length = h
            if j > start:
                out.append(body[start:j])
            inner = body[j + hdr:j + hdr + length]
            t = body[j + 3]
            self.embedded += 1
            if t in (0xFC, 0xFD):
                out.extend(self._split_embedded(inner))
            elif t == 0xFA and inner:
                self.audio.append((body[j + 4], AUDIO_RATES.get(body[j + 5], 8000), inner))
            start = pos = j + hdr + length
        if start < len(body):
            out.append(body[start:])
        return out

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out: list[bytes] = []
        while True:
            i = self._find_start()
            if i == -1:
                self.dropped += max(0, len(self.buf) - 3)
                del self.buf[:max(0, len(self.buf) - 3)]
                break
            if i == -2:
                break
            if i:
                self.dropped += i               # bytes between frames that belong to no frame
                del self.buf[:i]
            t = self.buf[3]
            h = self._header(self.buf, 0)
            if h is None:
                break
            hdr, length = h
            if length > 8 * 1024 * 1024:            # garbage: resync
                self.dropped += 4
                del self.buf[:4]
                continue
            if len(self.buf) < hdr + length:
                break
            body = bytes(self.buf[hdr:hdr + length])
            media, rate_code = self.buf[4], self.buf[5]
            if t == 0xFC and 1 <= media <= 60:
                self.fps_hint = media
            del self.buf[:hdr + length]
            if t in (0xFC, 0xFD):
                out.extend(self._split_embedded(body))
            elif t == 0xFA and body:
                self.audio.append((media, AUDIO_RATES.get(rate_code, 8000), body))
        return out


def sniff_codec(data: bytes) -> str | None:
    """'h264' / 'hevc' from the first NAL unit that carries parameter sets or a
    key frame, or None if it can't be told yet (e.g. a P-frame)."""
    i = data.find(b"\x00\x00\x01")
    while i != -1 and i + 4 < len(data):
        b, b2 = data[i + 3], data[i + 4]
        if b in (0x40, 0x42, 0x44, 0x26, 0x28) and b2 == 0x01:     # HEVC VPS/SPS/PPS/IDR
            return "hevc"
        if b & 0x80 == 0 and (b & 0x1F) in (7, 8, 5):               # H.264 SPS/PPS/IDR
            return "h264"
        i = data.find(b"\x00\x00\x01", i + 3)
    return None


class H264Decoder:
    """Video decoder on top of PyAV ('pip install av'). Despite the name it picks
    H.264 or H.265 (HEVC) automatically from the first key frame, because many
    XM/Provision recorders are set to H.265."""

    def __init__(self):
        import av  # noqa: WPS433 - optional dependency, imported lazily
        self._av = av
        self._ctx = None
        self.codec: str | None = None

    def decode(self, data: bytes):
        """Yield BGR numpy frames (full size)."""
        for frame in self.decode_raw(data):
            yield frame.to_ndarray(format="bgr24")

    @staticmethod
    def to_rgb(frame, max_width: int | None = None):
        """Scale an av frame down to at most max_width and return an RGB ndarray.
        Scaling here, off the GUI thread, is what keeps the UI light."""
        w, h = frame.width, frame.height
        if max_width and w > max_width:
            h = max(2, int(round(h * max_width / w / 2)) * 2)
            w = max_width
        return np.ascontiguousarray(frame.reformat(width=w, height=h, format="rgb24").to_ndarray())

    def decode_raw(self, data: bytes):
        """Yield decoded av.VideoFrame objects (no colour conversion yet)."""
        if self._ctx is None:
            codec = sniff_codec(data)
            if codec is None:
                return                      # wait for a key frame
            self.codec = codec
            self._ctx = self._av.CodecContext.create(codec, "r")
            try:                                    # use all cores: 2560x1440 HEVC is too heavy for one
                self._ctx.thread_type = "AUTO"
                self._ctx.thread_count = 0
            except Exception:  # noqa: BLE001 - older PyAV: keep the defaults
                pass
        for packet in self._ctx.parse(data):
            try:
                for frame in self._ctx.decode(packet):
                    yield frame
            except Exception:  # noqa: BLE001 - a broken frame must not kill the stream
                continue


def check_login(host: str, port: int, username: str, password: str, timeout: float = 6.0):
    """Returns (AuthResult, info_or_error_text)."""
    c = DVRIPClient(host, port, username, password, timeout)
    try:
        c.connect()
        info = c.login()
        return AuthResult.OK, info
    except DVRIPAuthError as exc:
        return AuthResult.BAD_CREDENTIALS, str(exc)
    except DVRIPError as exc:
        return AuthResult.ERROR, str(exc)
    except OSError as exc:
        return AuthResult.UNREACHABLE, str(exc)
    finally:
        c.close()
