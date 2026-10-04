"""Checks that the player can open, jump around in and decode a video file (no window involved).

  python tools/diagnose_player.py "C:\\Users\\you\\Videos\\CameraRecordings\\channel3_2026-10-01_14-03-22.mp4"

Send the printed lines if the player misbehaves on a file."""
import sys
import time

sys.path.insert(0, ".")
from app.core.videosource import VideoSource  # noqa: E402


def main(path: str) -> int:
    try:
        src = VideoSource(path)
    except Exception as exc:                          # noqa: BLE001
        print("opening failed:", type(exc).__name__, exc)
        return 1
    print(f"opened: {src.width}x{src.height}, {src.fps:.2f} fps, duration {src.duration:.1f} s")
    if src.duration <= 0:
        print("no duration in the file: the seek bar cannot work")
    for frac in (0.0, 0.25, 0.5, 0.75, 0.95):
        target = src.duration * frac
        t0 = time.monotonic()
        times = []
        try:
            for ft, _frame in src.frames_from(target):
                times.append(ft)
                if len(times) >= 5:
                    break
        except Exception as exc:                      # noqa: BLE001
            print(f"seek to {target:7.1f} s FAILED: {type(exc).__name__} {exc}")
            continue
        took = time.monotonic() - t0
        first = f"{times[0]:.2f}" if times else "none"
        print(f"seek to {target:7.1f} s: first frame at {first} s, {len(times)} frames in {took:.2f} s")
    src.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
