"""Wrap a raw H.264 / H.265 elementary stream (what the NVR sends) into an MP4 file.

No re-encoding: the packets are copied as they are, so it is fast and lossless. The raw stream
has no timestamps, so they are rebuilt from the recording's real length: frames / seconds.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


def frame_rate(frames: int, seconds: float | None, default: int = 25) -> Fraction:
    """Average frame rate as an exact fraction, clamped to something sane (1..60 fps)."""
    if frames <= 0 or not seconds or seconds <= 0:
        return Fraction(default)
    fps = Fraction(frames, 1) / Fraction(seconds).limit_denominator(1000)
    fps = fps.limit_denominator(1000)
    if fps < 1 or fps > 60:
        return Fraction(default)
    return fps


def find_ffmpeg() -> str | None:
    """ffmpeg on PATH, or an ffmpeg.exe lying next to the app / in the recordings folder."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for d in (Path(sys.executable).parent, Path.cwd(), Path(__file__).resolve().parents[2],
              Path.home() / "Videos" / "CameraRecordings", Path.home() / "Downloads"):
        if (d / name).is_file():
            return str(d / name)
    return None


def count_frames(raw_path: str, codec: str | None) -> int:
    """Pictures in a raw stream, counted straight from the NAL headers (no decoder needed)."""
    import mmap
    n = 0
    with open(raw_path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        size = len(m)
        i = m.find(b"\x00\x00\x01")
        while i != -1 and i + 5 < size:
            b0 = m[i + 3]
            if codec == "hevc":
                if ((b0 >> 1) & 0x3F) < 32 and m[i + 5] & 0x80:      # first_slice_segment_in_pic_flag
                    n += 1
            elif (b0 & 0x1F) in (1, 5) and m[i + 4] & 0x80:           # first_mb_in_slice == 0
                n += 1
            i = m.find(b"\x00\x00\x01", i + 3)
    return n


def _ffmpeg_remux(raw_path: str, mp4_path: str, codec: str | None, fps: Fraction) -> None:
    exe = find_ffmpeg()
    if not exe:
        raise RuntimeError("ffmpeg לא נמצא")
    cmd = [exe, "-y", "-v", "error", "-r", str(float(fps)), "-i", raw_path, "-c", "copy"]
    if codec == "hevc":
        cmd += ["-tag:v", "hvc1"]                    # the tag Windows / QuickTime players expect
    cmd += ["-movflags", "+faststart", mp4_path]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg נכשל: {r.stderr.strip()[-300:]}")


def raw_to_mp4(raw_path: str, mp4_path: str, codec: str | None, seconds: float | None) -> int:
    """Returns the number of video frames written. Raises on any failure (caller keeps the raw file).
    PyAV first; if it refuses (its API differs between versions), a real ffmpeg does the same copy."""
    try:
        return _pyav_remux(raw_path, mp4_path, codec, seconds)
    except Exception as first:  # noqa: BLE001
        Path(mp4_path).unlink(missing_ok=True)
        try:
            frames = count_frames(raw_path, codec)
            _ffmpeg_remux(raw_path, mp4_path, codec, frame_rate(frames, seconds))
            return frames
        except Exception as second:  # noqa: BLE001
            Path(mp4_path).unlink(missing_ok=True)
            raise RuntimeError(f"{first!r} | {second}") from second


def _pyav_remux(raw_path: str, mp4_path: str, codec: str | None, seconds: float | None) -> int:
    import av  # optional dependency, imported lazily like the decoder

    fmt = "hevc" if codec == "hevc" else "h264"

    with av.open(raw_path, format=fmt) as probe:
        vs = probe.streams.video[0]
        frames = sum(1 for p in probe.demux(vs) if p.size)
    if frames == 0:
        raise ValueError("no video frames in the downloaded file")
    fps = frame_rate(frames, seconds)
    base = Fraction(1, 90000)                        # integer ticks: newer PyAV/FFmpeg reject odd fractional time bases
    step = int(90000 / fps)                          # ticks per frame (duration)

    written = 0
    with av.open(raw_path, format=fmt) as src, av.open(mp4_path, "w", format="mp4") as dst:
        in_stream = src.streams.video[0]
        make = getattr(dst, "add_stream_from_template", None)
        out_stream = make(in_stream) if make else dst.add_stream(template=in_stream)
        for pkt in src.demux(in_stream):
            if not pkt.size:
                continue                              # flush marker
            pkt.stream = out_stream
            pkt.time_base = base
            pkt.pts = pkt.dts = int(written * 90000 / fps)
            pkt.duration = step
            dst.mux(pkt)
            written += 1
    return written
