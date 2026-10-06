"""
Core camera model + a background thread that pulls frames from an
RTSP stream and reports connection status (ACTIVE / DEAD / OFFLINE).

ACTIVE  - stream is connecting and frames are arriving normally
DEAD    - we can open a connection / the device answers, but no valid
          frames come through (common with cheap Chinese cameras whose
          proprietary CMS blocks the plain RTSP port, or a corrupted stream)
OFFLINE - no response from the device at all (wrong IP/port, powered off,
          network unreachable)
"""
from __future__ import annotations

import socket
import sys
import time

import cv2
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

from app.core.config import CameraConfig, CameraStatus  # noqa: F401  (re-exported: other modules import them from here)
from app.core.streamstats import StreamStats


# (host, port, channel) of channels whose NVR has no usable sub-stream. Remembered for the
# whole session so re-opening a tile does not waste ~12 s probing the sub-stream again.
NO_SUB: set = set()

MAX_AUTH_FAILURES = 3   # wrong password: stop after this many tries (NVRs lock the user after a few bad logins)


class CameraWorker(QThread):
    """One thread per camera tile. Emits frames + status changes."""

    frame_ready = pyqtSignal(str, object)          # camera_id, numpy frame (BGR)
    status_changed = pyqtSignal(str, object)        # camera_id, CameraStatus
    path_resolved = pyqtSignal(str, str)            # camera_id, working rtsp_path (only on 'auto')
    audio_ready = pyqtSignal(str, bytes, int)       # camera_id, PCM16 mono, sample rate (dvrip only)

    def __init__(self, cfg: CameraConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._running = True
        self._last_status: CameraStatus | None = None
        self._resolved_path: str | None = None   # local, thread-owned -- never mutate self.cfg directly
        self.audio_enabled = False               # flipped from the GUI thread (plain bool, safe)
        self.pending = False        # True while the GUI still owns the last frame -> newer ones are dropped
        self.render = True          # False while the tile is hidden (focus mode): skip the costly conversion
        self.max_width = 640        # frames are scaled down to this width in the worker thread
        self.force_main = False     # True = "HD" button: use the full-quality main stream (heavy!)
        self.focused = False        # True = tile is enlarged
        self._restart = False
        self._no_sub = False        # the NVR has no usable sub-stream -> always use Main

    def _update_width(self):
        self.max_width = 1600 if self.force_main else (1280 if self.focused else 640)

    def set_focused(self, focused: bool):
        self.focused = focused
        self._update_width()

    def set_quality(self, main: bool):
        """Switch between the light sub-stream and the (possibly 4K) main stream."""
        self.force_main = main
        self._update_width()
        self._restart = True

    def request_stop(self):
        """Ask the thread to end without waiting, so many workers can be stopped in parallel."""
        self._running = False

    def stop(self):
        self.request_stop()
        self.wait(2000)

    def _wait(self, seconds: float):
        """Sleep that ends at once when the worker is stopped."""
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(0.1)

    def _emit_status(self, status: CameraStatus):
        if status != self._last_status:
            self._last_status = status
            self.status_changed.emit(self.cfg.id, status)

    def _tcp_reachable(self, timeout=2.0) -> bool:
        try:
            with socket.create_connection((self.cfg.host, self.cfg.port), timeout=timeout):
                return True
        except OSError:
            return False

    def _try_open(self, rtsp_url: str, timeout_frames: int = 15):
        """Try to open a specific RTSP URL and confirm we actually get frames
        (not just that ffmpeg accepted the connection)."""
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            return None
        for _ in range(timeout_frames):
            ok, frame = cap.read()
            if ok and frame is not None:
                return cap
            time.sleep(0.1)
        cap.release()
        return None

    def _build_url(self, path: str) -> str:
        return self.cfg.rtsp_url_for(path)

    def _open_stream(self):
        """If the user gave a concrete path, use it. If they left it as
        'auto' (or blank), try the known common paths per brand until one
        actually yields a frame. IMPORTANT: this never writes to self.cfg
        directly (that object is shared with the GUI thread, e.g. for
        saving to disk) -- the resolved path is tracked locally and reported
        back via the path_resolved signal so the GUI thread can persist it."""
        if self._resolved_path:
            return self._try_open(self._build_url(self._resolved_path))

        if self.cfg.rtsp_path and self.cfg.rtsp_path.strip("/").lower() != "auto":
            return self._try_open(self._build_url(self.cfg.rtsp_path))

        from app.core.rtsp_paths import COMMON_RTSP_PATHS

        for path in COMMON_RTSP_PATHS:
            cap = self._try_open(self._build_url(path))
            if cap:
                self._resolved_path = path
                self.path_resolved.emit(self.cfg.id, path)
                return cap
        return None

    def _run_dvrip(self):
        """XM / Provision CMS protocol (port 34567). Same status semantics as RTSP."""
        import sys
        from app.core import dvrip

        try:
            dvrip.H264Decoder()          # fail early if no video decoder works on this PC
        except Exception as exc:  # noqa: BLE001
            print(f"[dvrip] video decoder unavailable: {exc}", file=sys.stderr)
            self._emit_status(CameraStatus.DEAD)
            return

        delay = 3.0                       # grows while the channel keeps failing (empty channels)

        wait = self._wait
        auth_failures = 0

        while self._running:
            self._restart = False
            client = dvrip.DVRIPClient(self.cfg.host, self.cfg.port, self.cfg.username, self.cfg.password)
            try:
                client.connect()
                client.login()
            except dvrip.DVRIPAuthError:
                self._emit_status(CameraStatus.AUTH_FAILED)
                client.close()
                auth_failures += 1
                if auth_failures >= MAX_AUTH_FAILURES:
                    print(f"[dvrip] {self.cfg.host}: wrong user/password {auth_failures} times - not retrying "
                          f"(edit the device or reopen the camera to try again)", file=sys.stderr)
                    return
                wait(delay)
                continue
            except OSError:
                self._emit_status(CameraStatus.OFFLINE)
                client.close()
                wait(delay)
                continue
            except dvrip.DVRIPError as exc:
                print(f"[dvrip] login failed: {exc}", file=sys.stderr)
                self._emit_status(CameraStatus.DEAD)
                client.close()
                wait(delay)
                continue

            auth_failures = 0
            key = (self.cfg.host, self.cfg.port, self.cfg.channel)
            stream = "Main" if (self.force_main or self._no_sub or key in NO_SUB) else self.cfg.stream
            got_video = False
            try:
                client.start_monitor(self.cfg.channel, stream)
                parser, decoder = dvrip.XMFrameParser(), dvrip.H264Decoder(self.max_width)
                logged_audio = False
                video_seen, decoded_any = 0, False
                stats = StreamStats()
                for chunk in client.read_video_payloads():
                    if not self._running or self._restart:
                        break
                    client.keepalive_if_due()
                    if chunk:
                        stats.chunk()
                    line = stats.report()
                    if line and (stats.notable or stats.reports == 1):
                        print(f"[stats] ch{self.cfg.channel + 1} {stream}: {line}", file=sys.stderr)
                    video_frames = parser.feed(chunk)
                    audio, parser.audio = parser.audio, []
                    for media, rate, payload in audio:
                        if not logged_audio:
                            logged_audio = True
                            print(f"[dvrip] audio stream: media=0x{media:02X} rate={rate} Hz", file=sys.stderr)
                        if self.audio_enabled:
                            self.audio_ready.emit(self.cfg.id, dvrip.alaw_to_pcm16(payload), rate)
                    for video in video_frames:
                        video_seen += 1
                        for frame in decoder.decode_raw(video):
                            if not decoded_any:
                                decoded_any = got_video = True
                                delay = 3.0
                                print(f"[dvrip] ch{self.cfg.channel + 1} {stream}: video OK ({decoder.codec} via {decoder.backend}, "
                                      f"{frame.width}x{frame.height})", file=sys.stderr)
                            self._emit_status(CameraStatus.ACTIVE)
                            stats.frame()
                            # The GUI is still busy with the previous frame (or the tile is
                            # hidden): drop this one *before* the expensive scaling/conversion.
                            if self.pending or not self.render:
                                if self.pending:
                                    stats.dropped()
                                continue
                            arr = dvrip.H264Decoder.to_rgb(frame, self.max_width)
                            h, w, _ = arr.shape
                            img = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
                            self.pending = True
                            self.frame_ready.emit(self.cfg.id, img)
                    if not decoded_any and video_seen == 160:
                        print(f"[dvrip] {video_seen} video frames but none decoded (codec={decoder.codec})",
                              file=sys.stderr)
                        self._emit_status(CameraStatus.DEAD)
                        break
            except (OSError, dvrip.DVRIPError) as exc:
                print(f"[dvrip] ch{self.cfg.channel + 1} {stream}: stream ended: {exc}", file=sys.stderr)
                self._emit_status(CameraStatus.DEAD)
            finally:
                client.close()

            if self._restart:
                continue                      # quality switch requested -> reconnect right away
            if stream != "Main" and not got_video:
                print(f"[dvrip] ch{self.cfg.channel + 1}: no sub-stream video, falling back to Main",
                      file=sys.stderr)
                self._no_sub = True
                NO_SUB.add(key)
                continue
            delay = 3.0 if got_video else min(delay * 2, 30.0)
            wait(delay)

    def run(self):
        if self.cfg.protocol == "dvrip":
            return self._run_dvrip()
        retry_delay = 3
        auth_failures = 0
        while self._running:
            if not self._tcp_reachable():
                self._emit_status(CameraStatus.OFFLINE)
                self._wait(retry_delay)
                continue

            from app.core.rtsp_auth import AuthResult, check_rtsp_auth
            probe_path = self._resolved_path or (
                self.cfg.rtsp_path if self.cfg.rtsp_path.strip("/").lower() not in ("", "auto") else "/"
            )
            if check_rtsp_auth(self.cfg.host, self.cfg.port, probe_path,
                               self.cfg.username, self.cfg.password) == AuthResult.BAD_CREDENTIALS:
                self._emit_status(CameraStatus.AUTH_FAILED)
                auth_failures += 1
                if auth_failures >= MAX_AUTH_FAILURES:
                    print(f"[rtsp] {self.cfg.host}: wrong user/password {auth_failures} times - not retrying",
                          file=sys.stderr)
                    return
                self._wait(retry_delay)
                continue
            auth_failures = 0

            options = (
                "rtsp_transport;tcp" if self.cfg.transport == "tcp" else "rtsp_transport;udp"
            )
            import os
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = options

            cap = self._open_stream()
            if not cap:
                self._emit_status(CameraStatus.DEAD)
                self._wait(retry_delay)
                continue

            consecutive_failures = 0
            while self._running:
                ok, frame = cap.read()
                if not ok or frame is None:
                    consecutive_failures += 1
                    if consecutive_failures > 10:
                        self._emit_status(CameraStatus.DEAD)
                        break
                    time.sleep(0.2)
                    continue

                consecutive_failures = 0
                self._emit_status(CameraStatus.ACTIVE)
                if not self.pending and self.render:
                    self.pending = True
                    self.frame_ready.emit(self.cfg.id, frame)

            cap.release()
            self._wait(retry_delay)
