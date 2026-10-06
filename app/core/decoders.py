"""Video decoder backends. The NVR sends a raw H.264 / H.265 stream; turning it into pictures can be done in
three independent ways, so that when one is blocked or broken on a PC (a locked-down computer room, a DLL that
will not load) another one can be tried:

  pyav    the PyAV package (FFmpeg libraries inside the program) - the original way
  ffmpeg  a separate ffmpeg.exe process: the stream goes into its stdin, finished pictures come out of stdout
  opencv  the FFmpeg that OpenCV carries: the stream is handed over on a local socket (127.0.0.1), nothing
          leaves the PC

The choice lives in settings.json ("decoder": auto | pyav | ffmpeg | opencv; menu "מפענח וידאו" in the program).
"auto" uses the first one that works on this PC. Every backend exposes feed(bytes) -> frames, finish() and close();
frames are either PyAV frames or NpFrame, and to_rgb() accepts both."""
from __future__ import annotations

import base64
import collections
import os
import re
import socket
import subprocess
import sys
import threading
import time

import numpy as np

from app.core import settings

BACKENDS = ("pyav", "ffmpeg", "opencv")
CHOICES = (("auto", "אוטומטי (הראשון שעובד)"),
           ("pyav", "PyAV (כמו עד היום)"),
           ("ffmpeg", "ffmpeg.exe חיצוני"),
           ("opencv", "OpenCV (ה-FFmpeg שבתוכו)"))
LABELS = {"pyav": "PyAV", "ffmpeg": "ffmpeg.exe חיצוני", "opencv": "OpenCV"}

DEFAULT_CAP = 1280          # the external ffmpeg never delivers wider than this (the pipe carries raw RGB); narrower streams stay as they are
QUEUE_MAX = 60              # decoded pictures waiting to be picked up; the oldest are dropped beyond this

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_cv_env_lock = threading.Lock()


# ---------------------------------------------------------------- frames
class NpFrame:
    """A decoded picture as an RGB numpy array (what the ffmpeg / opencv backends deliver)."""

    def __init__(self, rgb: np.ndarray):
        self.rgb = rgb
        self.height, self.width = int(rgb.shape[0]), int(rgb.shape[1])

    def to_ndarray(self, format: str = "rgb24"):
        if format == "bgr24":
            return np.ascontiguousarray(self.rgb[:, :, ::-1])
        return self.rgb


def _resize(arr: np.ndarray, w: int, h: int) -> np.ndarray:
    try:
        import cv2
        return cv2.resize(arr, (w, h), interpolation=cv2.INTER_AREA)
    except ImportError:
        ys = np.arange(h) * arr.shape[0] // h
        xs = np.arange(w) * arr.shape[1] // w
        return arr[ys][:, xs]


def to_rgb(frame, max_width: int | None = None):
    """Scale a decoded picture down to at most max_width and return a writable RGB ndarray.
    Done on the worker thread, off the GUI thread, which is what keeps the UI light."""
    w, h = frame.width, frame.height
    if max_width and w > max_width:
        h = max(2, int(round(h * max_width / w / 2)) * 2)
        w = max_width
    if isinstance(frame, NpFrame):
        arr = frame.rgb
        if w != frame.width:
            arr = _resize(arr, w, h)
        arr = np.ascontiguousarray(arr)
        return arr if arr.flags.writeable else arr.copy()
    return np.ascontiguousarray(frame.reformat(width=w, height=h, format="rgb24").to_ndarray())


# ---------------------------------------------------------------- PyAV
class PyAVBackend:
    name = "pyav"

    def __init__(self, codec: str, max_width=None):
        import av
        self._ctx = av.CodecContext.create(codec, "r")
        try:                                        # use all cores: 2560x1440 HEVC is too heavy for one
            self._ctx.thread_type = "AUTO"
            self._ctx.thread_count = 0
        except Exception:  # noqa: BLE001 - older PyAV: keep the defaults
            pass

    def feed(self, data: bytes):
        for packet in self._ctx.parse(data):
            try:
                for frame in self._ctx.decode(packet):
                    yield frame
            except Exception:  # noqa: BLE001 - a broken frame must not kill the stream
                continue

    def finish(self):
        try:
            for frame in self._ctx.decode(None):
                yield frame
        except Exception:  # noqa: BLE001
            return

    def close(self):
        self._ctx = None


# ---------------------------------------------------------------- external ffmpeg
def find_ffmpeg() -> str | None:
    """The ffmpeg chosen in the menu, else PATH / next to the program / in Downloads (see mp4.find_ffmpeg)."""
    chosen = settings.get("ffmpeg_path")
    if chosen and os.path.isfile(chosen):
        return chosen
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for name in ("ffmpeg.exe", "ffmpeg"):
            if os.path.isfile(os.path.join(base, name)):
                return os.path.join(base, name)
    from app.core import mp4
    return mp4.find_ffmpeg()


_ffmpeg_ver: dict[str, tuple[bool, str]] = {}


def _ffmpeg_check(exe: str) -> str:
    """Run `ffmpeg -version` once. Returns the flag pair that keeps every picture (no dropping / duplicating)."""
    if exe not in _ffmpeg_ver:
        try:
            r = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"ffmpeg לא רץ ({exc})") from exc
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg נכשל: {(r.stderr or r.stdout).strip()[-200:]}")
        m = re.search(r"version n?(\d+)\.(\d+)", r.stdout)
        old = bool(m) and (int(m.group(1)), int(m.group(2))) < (5, 1)
        _ffmpeg_ver[exe] = (old, r.stdout.splitlines()[0] if r.stdout else "")
    return "old" if _ffmpeg_ver[exe][0] else "new"


class FFmpegBackend:
    name = "ffmpeg"

    def __init__(self, codec: str, max_width=None):
        exe = find_ffmpeg()
        if not exe:
            raise RuntimeError("ffmpeg.exe לא נמצא (שים אותו ליד start.pyw או בחר אותו בתפריט)")
        keep = ["-vsync", "0"] if _ffmpeg_check(exe) == "old" else ["-fps_mode", "passthrough"]
        cap = max(DEFAULT_CAP, int(max_width or 0))     # a tile can be enlarged later, so never go below 1280
        cmd = [exe, "-hide_banner", "-loglevel", "info", "-nostdin",
               "-fflags", "discardcorrupt", "-flags", "low_delay",
               "-probesize", "32768", "-analyzeduration", "0", "-threads", "3",
               "-f", "hevc" if codec == "hevc" else "h264", "-i", "pipe:0",
               "-an", "-sn", "-dn", "-vf", f"scale=w='min({cap},iw)':h=-2",
               "-pix_fmt", "rgb24", *keep, "-f", "rawvideo", "pipe:1"]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      creationflags=_NO_WINDOW)
        self._frames: collections.deque = collections.deque(maxlen=QUEUE_MAX)
        self._tail: collections.deque = collections.deque(maxlen=25)
        self._size: tuple[int, int] | None = None
        self._size_known = threading.Event()
        self._failed = False
        self._closed = False
        self._threads = [threading.Thread(target=self._read_err, daemon=True),
                         threading.Thread(target=self._read_out, daemon=True)]
        for t in self._threads:
            t.start()

    # -- helper threads
    def _read_err(self):
        out_section = False
        buf = b""
        stream = self._proc.stderr
        while True:
            chunk = stream.read1(512)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]+", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                self._tail.append(line)
                if line.startswith("Output #0"):
                    out_section = True
                elif out_section and "Video: rawvideo" in line and self._size is None:
                    m = re.search(r"[ ,](\d{2,5})x(\d{2,5})[ ,\[]", line + " ")
                    if m:
                        self._size = (int(m.group(1)), int(m.group(2)))
                        self._size_known.set()
        self._size_known.set()

    def _read_out(self):
        self._size_known.wait()
        if self._size is None:
            return
        w, h = self._size
        n = w * h * 3
        out = self._proc.stdout
        while True:
            buf = bytearray(n)
            got = 0
            view = memoryview(buf)
            while got < n:
                k = out.readinto(view[got:])
                if not k:
                    return
                got += k
            self._frames.append(NpFrame(np.frombuffer(buf, np.uint8).reshape(h, w, 3)))

    # -- backend interface
    def _fail(self, exc):
        if not self._failed:
            self._failed = True
            time.sleep(0.2)
            print(f"[decoder:ffmpeg] ffmpeg stopped ({exc}): {' | '.join(list(self._tail)[-4:])}", file=sys.stderr)

    def _drain(self):
        while True:
            try:
                yield self._frames.popleft()
            except IndexError:
                return

    def feed(self, data: bytes):
        if self._failed or self._closed:
            return
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self._fail(exc)
            return
        yield from self._drain()

    def finish(self):
        try:
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass
        deadline = time.time() + 15
        while time.time() < deadline:
            yield from self._drain()                # pick pictures up while ffmpeg is still finishing the backlog
            if self._proc.poll() is not None and not any(t.is_alive() for t in self._threads):
                break
            time.sleep(0.01)
        yield from self._drain()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self._proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        for s in (self._proc.stdout, self._proc.stderr):
            try:
                s.close()
            except (OSError, ValueError):
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- OpenCV's own FFmpeg
class OpenCVBackend:
    """OpenCV can only open files / URLs, so the stream is served on a local socket: a listening socket on
    127.0.0.1 (port chosen by the system), OpenCV connects to it as if it were a network camera, and we push
    the NVR's bytes into the connection."""
    name = "opencv"

    def __init__(self, codec: str, max_width=None):
        import cv2
        self._cv2 = cv2
        self._frames: collections.deque = collections.deque(maxlen=QUEUE_MAX)
        self._closed = False
        self._failed = False
        self._cap = None
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self._srv.settimeout(6.0)
        port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._reader, args=(f"tcp://127.0.0.1:{port}",), daemon=True)
        self._thread.start()
        try:
            self._conn, _ = self._srv.accept()
        except OSError as exc:
            self.close()
            raise RuntimeError(f"OpenCV לא התחבר לשקע המקומי ({exc})") from exc
        self._conn.settimeout(10.0)

    def _reader(self, url: str):
        cv2 = self._cv2
        # Probe on a small amount of data: the default (5 MB, 5 s) would hold the first picture back for
        # many seconds on a live stream. The RTSP cameras set this same variable themselves before each open,
        # so the previous value is put back afterwards.
        key = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
        try:
            with _cv_env_lock:
                prev = os.environ.get(key)
                os.environ[key] = "probesize;65536|analyzeduration;0"
                try:
                    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                finally:
                    if prev is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = prev
        except Exception as exc:  # noqa: BLE001
            print(f"[decoder:opencv] open failed: {exc!r}", file=sys.stderr)
            return
        self._cap = cap
        if not cap.isOpened():
            if not self._closed:
                print("[decoder:opencv] OpenCV could not open the stream", file=sys.stderr)
            return
        while not self._closed:
            ok, img = cap.read()
            if not ok or img is None:
                break
            self._frames.append(NpFrame(img[:, :, ::-1]))
        cap.release()

    def _drain(self):
        while True:
            try:
                yield self._frames.popleft()
            except IndexError:
                return

    def feed(self, data: bytes):
        if self._failed or self._closed:
            return
        try:
            self._conn.sendall(data)
        except OSError as exc:
            self._failed = True
            print(f"[decoder:opencv] connection to OpenCV ended ({exc})", file=sys.stderr)
            return
        yield from self._drain()

    def finish(self):
        try:
            self._conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        deadline = time.time() + 15
        while time.time() < deadline:
            yield from self._drain()
            if not self._thread.is_alive():
                break
            time.sleep(0.01)
        yield from self._drain()

    def close(self):
        if self._closed:
            return
        self._closed = True
        for s in (getattr(self, "_conn", None), self._srv):
            try:
                if s:
                    s.close()
            except OSError:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


_CLASSES = {"pyav": PyAVBackend, "ffmpeg": FFmpegBackend, "opencv": OpenCVBackend}


# ---------------------------------------------------------------- choosing a backend
_avail: dict[str, str | None] = {}          # name -> None if usable, else the reason it is not


def reset() -> None:
    """Forget what was learned about this PC (after the setting or the ffmpeg path changed)."""
    _avail.clear()
    _ffmpeg_ver.clear()


def configured() -> str:
    choice = (os.environ.get("UCV_DECODER") or settings.get("decoder") or "auto").lower()
    return choice if choice in BACKENDS else "auto"


def unavailable_reason(name: str) -> str | None:
    """None when the backend can be used here, else a short reason. Quick: no stream needed."""
    if name in _avail:
        return _avail[name]
    reason = None
    try:
        if name == "pyav":
            import av                                           # noqa: F401
            av.CodecContext.create("h264", "r")
        elif name == "ffmpeg":
            exe = find_ffmpeg()
            if not exe:
                reason = "ffmpeg.exe לא נמצא"
            else:
                _ffmpeg_check(exe)
        elif name == "opencv":
            import cv2                                          # noqa: F401
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {exc}"
    _avail[name] = reason
    return reason


def candidates() -> list[str]:
    """Backends to try, best first. Raises with the reasons if none is usable."""
    choice = configured()
    names = [choice] if choice != "auto" else list(BACKENDS)
    ok = [n for n in names if unavailable_reason(n) is None]
    if not ok:
        why = "; ".join(f"{LABELS[n]}: {unavailable_reason(n)}" for n in names)
        raise RuntimeError(f"אין מפענח וידאו זמין ({why})")
    return ok


def open_backend(codec: str, names: list[str], max_width=None):
    errors = []
    for n in names:
        try:
            return _CLASSES[n](codec, max_width)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{n}: {exc}")
            print(f"[decoder] {n} could not start: {exc}", file=sys.stderr)
    raise RuntimeError("; ".join(errors))


# ---------------------------------------------------------------- self test
_TEST_CLIP_B64 = (
    "AAAAAWdCwAraEewEQAAAAwBAAAAFA8SJqAAAAAFozgOcgAAAAQYF//9N3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NCBy"
    "MzEwOCAzMWUxOWY5IC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyMyAtIGh0dHA6Ly93d3cudmlk"
    "ZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTAgcmVmPTEgZGVibG9jaz0wOjA6MCBhbmFseXNlPTA6MCBtZT1k"
    "aWEgc3VibWU9MCBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0wIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxs"
    "aXM9MCA4eDhkY3Q9MCBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0wIHRocmVhZHM9"
    "MSBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlf"
    "Y29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTAgd2VpZ2h0cD0wIGtleWludD01IGtleWludF9taW49MSBzY2Vu"
    "ZWN1dD0wIGludHJhX3JlZnJlc2g9MCByYz1jcmYgbWJ0cmVlPTAgY3JmPTQwLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5"
    "IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MACAAAABZYiEOgxgA+jGiOgoe+93XxvhvqfAEoyUwG4cy/v4RPggACITvgktx604"
    "AelNwxGLuoFDUqVQGMcAAsAAXqO//+GQwDQwACYEAyZ3//YP9/f9/jwYcRg4ABYAAQJOIAgACBTEAANFhjAAtNogmKfQYN19+Bby"
    "vwIGTMev/p4Ayq2SOq94qO1oBuHN4RJgACAAEAAiqAQEQX9fwAzDjScdn66D+EsBwATkImQKBvg3tufSABKlz/hwBKlyHAEqXPzU"
    "z2TPeEnAcExipngU/cjdBHkEYlz8ORiXMORiXNoWwAD/JjCHGstbz9AEM1YjWzvnIhEufuBiugTQSsTN4chGuKJdH4EzpmPQOAEC"
    "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"
)


def self_test(timeout: float = 8.0) -> dict[str, tuple[bool, str]]:
    """Decode a tiny built-in H.264 clip (10 pictures, 64x48) with each backend.
    Returns {backend: (worked, text)} - run from the menu to see which one works on this PC."""
    clip = base64.b64decode(_TEST_CLIP_B64)
    result: dict[str, tuple[bool, str]] = {}
    for name in BACKENDS:
        why = unavailable_reason(name)
        if why:
            result[name] = (False, why)
            continue
        be = None
        try:
            be = _CLASSES[name]("h264", None)
            frames = []
            t0 = time.time()
            step = 1500
            for i in range(0, len(clip), step):
                frames += list(be.feed(clip[i:i + step]))
            frames += list(be.finish())
            if not frames:
                result[name] = (False, "לא יצאה אף תמונה")
            else:
                f = frames[0]
                result[name] = (True, f"{len(frames)} תמונות, {f.width}x{f.height}, {time.time() - t0:.1f} שניות")
        except Exception as exc:  # noqa: BLE001
            result[name] = (False, f"{type(exc).__name__}: {exc}")
        finally:
            if be is not None:
                be.close()
    return result
