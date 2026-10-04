"""Finds out whether the NVR can start playing a recording from the MIDDLE of it. That decides whether a
player can show a recording straight from the network (seek / go back by asking again from another time)
without keeping a copy on disk.

  python tools/diagnose_seek.py 83.229.22.45 --user admin2 --password PASS --channel 0 --date 2026-10-01

Each test asks for a 60-second window and reads at most 40 MB. About 10-15 MB means the NVR honoured the
window; hitting the 40 MB limit means it ignored it and sent the whole file. Send me the whole output."""
import argparse
import copy
import datetime
import sys
import time

sys.path.insert(0, ".")
from app.core import dvrip  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"
CAP = 40 * 1024 * 1024


def request(c, channel, item, mode):
    """Like DVRIPClient.start_download, but with a choice of PlayMode."""
    param = {"FileName": item["name"], "PlayMode": mode, "StreamType": 0,
             "TransMode": "TCP", "Channel": channel, "Value": 0}

    def body(action):
        return {"Name": "OPPlayBack", "SessionID": c._sid(),
                "OPPlayBack": {"Action": action, "StartTime": item["begin"], "EndTime": item["end"],
                               "Parameter": param}}
    c._open_stream(dvrip.MSG_PLAY_CLAIM, body("Claim"), dvrip.MSG_PLAY_START, body("DownloadStart"), 12.0, "הורדה")


def test(a, base, start_offset, mode):
    begin = datetime.datetime.strptime(base["begin"], FMT) + datetime.timedelta(seconds=start_offset)
    item = copy.deepcopy(base)
    item["begin"] = begin.strftime(FMT)
    item["end"] = (begin + datetime.timedelta(seconds=60)).strftime(FMT)
    label = f"{mode:7} from +{start_offset // 60:3d} min ({item['begin'][11:]} - {item['end'][11:]})"
    c = dvrip.DVRIPClient(a.host, a.port, a.user, a.password, timeout=15)
    got, t0 = 0, time.monotonic()
    try:
        c.connect()
        c.login()
        request(c, a.channel, item, mode)
        for chunk in c.read_download_payloads(idle_timeout=2, max_silent=3):
            got += len(chunk)
            if got >= CAP:
                break
        verdict = "limit reached: the NVR sends the whole file" if got >= CAP else "ended by itself"
        print(f"{label}: {got / 1048576:6.1f} MB in {time.monotonic() - t0:4.1f} s - {verdict}")
    except (OSError, dvrip.DVRIPError) as exc:
        print(f"{label}: failed after {got / 1048576:.1f} MB - {type(exc).__name__}: {exc}")
    finally:
        c.close()
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=34567)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="")
    ap.add_argument("--channel", type=int, default=0)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    a = ap.parse_args()

    c = dvrip.DVRIPClient(a.host, a.port, a.user, a.password, timeout=10)
    try:
        c.connect()
        c.login()
        files = c.query_files(a.channel, f"{a.date} 00:00:00", f"{a.date} 23:59:59")
    except (OSError, dvrip.DVRIPError) as exc:
        print("could not list the recordings:", type(exc).__name__, exc)
        return
    finally:
        c.close()
    long_files = [f for f in files if (datetime.datetime.strptime(f["end"], FMT)
                                       - datetime.datetime.strptime(f["begin"], FMT)).total_seconds() >= 1200]
    if not long_files:
        print("no recording of 20 minutes or more on that day/channel; try --date or --channel")
        return
    base = long_files[0]
    total = (datetime.datetime.strptime(base["end"], FMT) - datetime.datetime.strptime(base["begin"], FMT)).total_seconds()
    print(f"recording: {base['begin']} - {base['end']}  ({base['size'] / 1048576:.0f} MB)\n")
    results = {}
    for mode in ("ByName", "ByTime"):
        for offset in (0, int(total // 2) // 60 * 60):
            results[(mode, offset)] = test(a, base, offset, mode)
    print()
    for mode in ("ByName", "ByTime"):
        start, middle = results[(mode, 0)], results[(mode, int(total // 2) // 60 * 60)]
        ok = 0 < start < CAP and 0 < middle < CAP
        print(f"{mode}: {'honours the time window - seeking is possible' if ok else 'does NOT honour it'}")
    print("\nsend me this whole output.")


if __name__ == "__main__":
    main()
