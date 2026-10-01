"""Checks recordings on an XM/Provision NVR: lists today's files for a channel and
tries to play the first one for a few seconds.

  python tools/diagnose_playback.py 83.229.22.45 --user admin2 --password PASS --channel 0
"""
import argparse
import datetime
import sys
import time

sys.path.insert(0, ".")
from app.core import dvrip, recsearch  # noqa: E402


def scan_all(a):
    """One line per channel: how many recordings, how long the NVR took, or what failed."""
    probe = dvrip.DVRIPClient(a.host, a.port, a.user, a.password, timeout=8)
    try:
        probe.connect()
        n = int(probe.login().get("ChannelNum", 16) or 16)
    except (OSError, dvrip.DVRIPError) as exc:
        print("התחברות נכשלה:", type(exc).__name__, exc)
        return
    finally:
        probe.close()
    print(f"תאריך {a.date}, {n} ערוצים, חיפוש אוטומטי בכל השיטות")
    for ch in range(n):
        c = dvrip.DVRIPClient(a.host, a.port, a.user, a.password, timeout=10)
        t0 = time.monotonic()
        try:
            c.connect()
            c.login()
            lines = []
            files, bad = recsearch.run_search(c, ch, [a.date], recsearch.plans_for("auto"), log=lines.append)
            print(f"ערוץ {ch + 1}: {len(files)} הקלטות, {len(bad)} חלונות נכשלו ({time.monotonic() - t0:.1f} שניות)")
            for l in lines:
                if "סיכום" in l or "לא ענה" in l:
                    print("    ", l)
        except (OSError, dvrip.DVRIPError) as exc:
            print(f"ערוץ {ch + 1}: נכשל אחרי {time.monotonic() - t0:.1f} שניות - {type(exc).__name__}: {exc}")
        finally:
            c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=34567)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="")
    ap.add_argument("--channel", type=int, default=0)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--all", action="store_true", help="query the recordings of every channel and report each one")
    a = ap.parse_args()

    if a.all:
        scan_all(a)
        return

    c = dvrip.DVRIPClient(a.host, a.port, a.user, a.password, timeout=8)
    try:
        c.connect()
        c.login()
        print(f"ערוץ {a.channel + 1}, תאריך {a.date}")
        files = c.query_files(a.channel, f"{a.date} 00:00:00", f"{a.date} 23:59:59")
        print(f"נמצאו {len(files)} הקלטות")
        for f in files[:8]:
            print("  ", f["begin"], "->", f["end"], f["size"], "bytes", f["name"])
        if not files:
            print("לא נמצאו הקלטות. נסה תאריך אחר (--date 2026-09-29) או ערוץ אחר (--channel 1)")
            return
    except (OSError, dvrip.DVRIPError) as exc:
        print("שאילתת ההקלטות נכשלה:", type(exc).__name__, exc)
        return
    finally:
        c.close()

    c = dvrip.DVRIPClient(a.host, a.port, a.user, a.password, timeout=8)
    try:
        c.connect()
        c.login()
        c.start_playback(a.channel, files[0])
        parser, video, audio = dvrip.XMFrameParser(), 0, 0
        end = time.monotonic() + 5
        for chunk in c.read_video_payloads(idle_timeout=1):
            if chunk:
                video += len(parser.feed(chunk))
                audio += len(parser.audio)
                parser.audio = []
            if time.monotonic() > end:
                break
        print(f"ניגון: {video} פריימי וידאו, {audio} פריימי שמע ב-5 שניות. סוגי הודעות: {sorted(c.media_msgids)}")
    except (OSError, dvrip.DVRIPError) as exc:
        print("הניגון נכשל:", type(exc).__name__, exc)
    finally:
        c.close()
    print("\nסיום. שלח לי את כל הפלט הזה.")


if __name__ == "__main__":
    main()
