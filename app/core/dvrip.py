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

Only the standard library is needed here; H.264 decoding is done elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import socket
import string
import struct
import time

from app.core.rtsp_auth import AuthResult

HEADER = struct.Struct("<BB2xII2xHI")   # 20 bytes
assert HEADER.size == 20

MSG_LOGIN = 1000
MSG_KEEPALIVE = 1006
MSG_MONITOR_START = 1410
MSG_MONITOR_DATA = 1412
MSG_MONITOR_CLAIM = 1413

# Return codes that mean "username/password not accepted"
BAD_CREDENTIAL_CODES = {106, 203, 205, 206}

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
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
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
        payload = self._recv_exact(sock, length) if length else b""
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
        if self.sock and time.monotonic() - self._last_keepalive >= max(5, self.alive_interval / 2):
            self._last_keepalive = time.monotonic()
            self._send(self.sock, MSG_KEEPALIVE, {"Name": "KeepAlive", "SessionID": self._sid()})
            self.sock.settimeout(3)
            try:
                self._recv_packet(self.sock)
            except (OSError, DVRIPError):
                pass
            finally:
                self.sock.settimeout(self.timeout)

    def _monitor_params(self, channel: int, stream: str) -> dict:
        return {"Channel": channel, "CombinMode": "NONE", "StreamType": stream, "TransMode": "TCP"}

    def start_monitor(self, channel: int = 0, stream: str = "Main", first_data_timeout: float = 6.0):
        """Open a live-video stream. Tries the documented layout (Claim on the
        control connection, Start on a second connection) and, if the device
        sends no video, the single-connection layout."""
        params = self._monitor_params(channel, stream)
        self._send(self.sock, MSG_MONITOR_CLAIM, {
            "Name": "OPMonitor", "SessionID": self._sid(),
            "OPMonitor": {"Action": "Claim", "Parameter": params}})
        try:
            _m, _s, _q, payload = self._recv_packet(self.sock)
            ret = _parse_json(payload).get("Ret", 100)
            if ret != 100:
                raise DVRIPError(f"ה-NVR דחה בקשת וידאו לערוץ {channel + 1} (קוד {ret})", ret)
        except socket.timeout:
            pass

        start = {"Name": "OPMonitor", "SessionID": self._sid(),
                 "OPMonitor": {"Action": "Start", "Parameter": params}}

        # layout A: separate media connection
        self.media_sock = self._connect()
        self._send(self.media_sock, MSG_MONITOR_START, start)
        if self._wait_for_data(self.media_sock, first_data_timeout):
            return
        self.media_sock.close()

        # layout B: same connection
        self.media_sock = self.sock
        self._send(self.sock, MSG_MONITOR_START, start)
        if self._wait_for_data(self.sock, first_data_timeout):
            return
        raise DVRIPError("ה-NVR אישר את ההתחברות אבל לא שלח וידאו")

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
                if msgid == MSG_MONITOR_DATA:
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
            if msgid == MSG_MONITOR_DATA:
                yield payload


class XMFrameParser:
    """Splits the XM payload byte-stream into H.264 (Annex-B) video frames."""

    _TYPES = (0xFC, 0xFD, 0xFA, 0xF9)

    def __init__(self):
        self.buf = bytearray()

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

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out: list[bytes] = []
        while True:
            i = self._find_start()
            if i == -1:
                del self.buf[:max(0, len(self.buf) - 3)]
                break
            if i == -2:
                break
            if i:
                del self.buf[:i]
            t = self.buf[3]
            if t == 0xFC:
                hdr = 16
                if len(self.buf) < hdr:
                    break
                length = int.from_bytes(self.buf[12:16], "little")
            elif t == 0xFD:
                hdr = 8
                if len(self.buf) < hdr:
                    break
                length = int.from_bytes(self.buf[4:8], "little")
            else:                                   # audio / info frame -> skip
                hdr = 8
                if len(self.buf) < hdr:
                    break
                length = int.from_bytes(self.buf[6:8], "little")
            if length > 8 * 1024 * 1024:            # garbage: resync
                del self.buf[:4]
                continue
            if len(self.buf) < hdr + length:
                break
            body = bytes(self.buf[hdr:hdr + length])
            del self.buf[:hdr + length]
            if t in (0xFC, 0xFD):
                out.append(body)
        return out


class H264Decoder:
    """Thin wrapper around PyAV ('pip install av')."""

    def __init__(self):
        import av  # noqa: WPS433 - optional dependency, imported lazily
        self._ctx = av.CodecContext.create("h264", "r")

    def decode(self, data: bytes):
        """Yield BGR numpy frames."""
        for packet in self._ctx.parse(data):
            try:
                for frame in self._ctx.decode(packet):
                    yield frame.to_ndarray(format="bgr24")
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
