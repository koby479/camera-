"""Checks whether the NVR can start playing a recording from the MIDDLE of it, and how exactly.

  python tools\\diagnose_seek.py

With no arguments it uses the NVR saved in the program and finds a long recording from today by itself.
For each test it asks the NVR to start 'N minutes into the recording', reads only until the first key frame
arrives, and prints the real time written in that frame. A difference of a few seconds from what was asked means
the NVR honours the start time (the player then jumps anywhere without downloading what is before it).
Send me the whole output if the player cannot jump."""
import argparse
import copy
import datetime
import sys
import time

sys.path.insert(0, ".")
from app.core import dvrip, frameindex  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"


def request(c, channel, item, mode):
    """Like DVRIPClient.start_download, with a choice of PlayMode."""
    c.start_download(channel, item, mode=mode)


def first_stamp(a, base, offset, mode):
    begin = datetime.datetime.strptime(base["begin"], FMT) + datetime.timedelta(seconds=offset)
    item = copy.deepcopy(base)
    item["begin"] = begin.strftime(FMT)
    label = f"{mode:7} from +{offset // 60:3d} min ({item['begin'][11:]})"
    c = dvrip.DVRIPClient(a.host, a.port, a.user, a.password, timeout=15)
    parser, got, t0 = dvrip.XMFrameParser(), 0, time.monotonic()
    try:
        c.connect()
        c.login()
        request(c, a.channel, item, mode)
        for chunk in c.read_download_payloads(idle_timeout=3, max_silent=3):
            got += len(chunk)
            parser.feed(chunk)
            stamps = [s for s in parser.stamps if s]
            if stamps:
                seconds = frameindex.stamp_to_seconds(stamps[0])
                if seconds is None:
                    print(f"{label}: a key frame arrived but its time is not a valid date (raw {stamps[0]:#x})")
                    return None
                asked = frameindex.wall_seconds(item["begin"])
                diff = seconds - asked
                real = datetime.datetime.utcfromtimestamp(seconds).strftime("%H:%M:%S")
                print(f"{label}: first key frame is at {real}  ({diff:+d} s from what was asked) "
                      f"after {got / 1024:.0f} KB, {time.monotonic() - t0:.1f} s")
                return diff
            if got > 4 * 1024 * 1024:
                print(f"{label}: 4 MB arrived and no key frame carried a time")
                return None
        print(f"{label}: the stream ended after {got / 1024:.0f} KB without a key frame")
    except (OSError, dvrip.DVRIPError) as exc:
        print(f"{label}: failed - {type(exc).__name__}: {exc}")
    finally:
        c.close()
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?", help="leave out to use the NVR saved in the program")
    ap.add_argument("--port", type=int, default=34567)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="")
    ap.add_argument("--channel", type=int, default=None, help="leave out to search the channels")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    a = ap.parse_args()
    if not a.host:
        from app.core import store
        nvrs, singles = store.load_all()
        saved = [d for d in nvrs + singles if d.protocol == "dvrip"]
        if not saved:
            print("no XM/DVRIP NVR is saved in the program; give the address: diagnose_seek.py HOST --user U --password P")
            return
        d = saved[0]
        a.host, a.port, a.user, a.password = d.host, d.port, d.username, d.password
        print(f"using the saved NVR {d.host}:{d.port} (user {d.username})")

    def is_long(f):
        return (datetime.datetime.strptime(f["end"], FMT) - datetime.datetime.strptime(f["begin"], FMT)).total_seconds() >= 1200

    base = None
    for ch in ([a.channel] if a.channel is not None else range(8)):
        c = dvrip.DVRIPClient(a.host, a.port, a.user, a.password, timeout=10)
        try:
            c.connect()
            c.login()
            files = [f for f in c.query_files(ch, f"{a.date} 00:00:00", f"{a.date} 23:59:59") if is_long(f)]
        except (OSError, dvrip.DVRIPError) as exc:
            print(f"channel {ch + 1}: {type(exc).__name__}: {exc}")
            files = []
        finally:
            c.close()
        if files:
            a.channel, base = ch, files[0]
            break
    if base is None:
        print("no recording of 20 minutes or more found; try --date YYYY-MM-DD (a day with recordings)")
        return
    total = (datetime.datetime.strptime(base["end"], FMT) - datetime.datetime.strptime(base["begin"], FMT)).total_seconds()
    print(f"channel {a.channel + 1}, recording {base['begin']} - {base['end']}\n")
    results = {}
    for mode in ("ByName", "ByTime"):
        for offset in (0, int(total // 2) // 60 * 60):
            results[(mode, offset)] = first_stamp(a, base, offset, mode)
    middle = int(total // 2) // 60 * 60
    print()
    for mode in ("ByName", "ByTime"):
        diff = results[(mode, middle)]
        ok = diff is not None and abs(diff) <= 25
        print(f"{mode}: {'honours the start time - the player can jump anywhere' if ok else 'does NOT honour it'}")
    print("\nsend me this whole output if the player cannot jump.")


if __name__ == "__main__":
    main()
