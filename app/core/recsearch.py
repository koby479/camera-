"""Searching an XM/Provision NVR for recordings, with several strategies.

Different firmwares answer the file query differently (some never answer a day-long query,
some want other file-type / event parameters). This module tries a list of strategies and
merges everything that any of them returns. No Qt in here, so it can be unit-tested.
"""
from __future__ import annotations

from typing import NamedTuple

from app.core import dvrip

# Parameter sets for OPFileQuery. "standard" is what most XM firmwares expect; the rest are
# educated guesses tried when the standard one finds nothing.
VARIANTS: dict[str, dict] = {
    "standard": {},
    "type_any": {"Type": "*"},
    "type_h265": {"Type": "h265"},
    "mask_all": {"DriverTypeMask": "0xFFFFFFFF"},
    "stream_int": {"StreamType": 0},
    "event_r": {"Event": "R"},
}
ALT_VARIANTS = [k for k in VARIANTS if k != "standard"]
ALL_VARIANTS = list(VARIANTS)


class Plan(NamedTuple):
    variants: list[str]
    minutes: int              # size of each time window
    timeout: float            # seconds to wait for the NVR per window
    only_if_empty: bool = False   # run only if the plans before it found nothing that day


MODES: list[tuple[str, str]] = [
    ("auto", "אוטומטי (מנסה את כל השיטות)"),
    ("std2h", "שיטה 1: חלונות של שעתיים"),
    ("std30", "שיטה 2: חלונות של חצי שעה"),
    ("day", "שיטה 3: יום שלם בבקשה אחת"),
    ("alt", "שיטה 4: פרמטרים חלופיים (סוג קובץ / אירוע)"),
    ("wide", "חיפוש רחב: כל מה שנמצא, לפי תאריך"),
]


# What worked on a recorder during this session: host -> variant name. The next search tries it first.
LEARNED: dict[str, str] = {}


def smart_plans(host: str | None = None) -> list[Plan]:
    """The one search the program runs. The common case costs a single quick pass (the parameter set that worked
    before on this recorder, else the standard one, in 2-hour windows); every further step runs only if the
    day is still empty: the other parameter sets in 6-hour windows, then one whole-day query."""
    first = LEARNED.get(host or "", "standard")
    others = [v for v in ALL_VARIANTS if v != first]
    return [Plan([first], 120, 10),
            Plan(others, 360, 8, only_if_empty=True),
            Plan(["standard"], 1440, 30, only_if_empty=True)]


def plans_for(mode: str) -> list[Plan]:
    if mode == "std2h":
        return [Plan(["standard"], 120, 10)]
    if mode == "std30":
        return [Plan(["standard"], 30, 10)]
    if mode == "day":
        return [Plan(["standard"], 1440, 40)]
    if mode == "alt":
        return [Plan(ALT_VARIANTS, 60, 8)]
    if mode == "wide":
        return [Plan(ALL_VARIANTS, 120, 8)]
    return smart_plans()


def _reconnect(client) -> bool:
    try:
        client.close()
        client.connect()
        client.login()
        return True
    except (OSError, dvrip.DVRIPError):
        return False


def run_search(client, channel: int, days: list[str], plans: list[Plan], on_files=None, log=None,
               status=None, cancelled=None) -> tuple[list[dict], list[str]]:
    """Search each day ('YYYY-MM-DD') with each plan. Returns (files, failed_windows).

    on_files(new_files) is called as soon as new recordings turn up, log(line) gets one line per
    window (useful to see which strategy worked), status(text) gets short progress text.
    Raises DVRIPError only when the NVR never answered a single query."""
    log = log or (lambda _s: None)
    status = status or (lambda _s: None)
    cancelled = cancelled or (lambda: False)
    found: dict = {}
    failed_windows: list[str] = []
    dead: set = set()
    answered = False
    last_error = ""

    for day in days:
        if cancelled():
            break
        day_files = 0
        for plan in plans:
            if plan.only_if_empty and day_files:
                continue
            for vname in plan.variants:
                if cancelled():
                    break
                key = (vname, plan.minutes)
                if key in dead:
                    continue

                def progress(window, count, secs, err, vname=vname, day=day):
                    res = f"{count} קבצים" if count is not None else f"נכשל ({err})"
                    log(f"{day} {window} [{vname}]: {res} ב-{secs:.1f} שניות")
                    tr = getattr(client, "trace", None)
                    if tr:                       # what the NVR literally answered (first empty/odd replies)
                        if count in (None, 0):
                            log("    תשובת ה-NVR: " + " | ".join(tr[-2:]))
                        del tr[:]
                    status(f"מחפש: {day} {window} (שיטה {vname}), נמצאו עד כה {len(found)}")

                counted = {"new": 0}

                def on_part(fresh, counted=counted):
                    new = []
                    for f in fresh:
                        k = (f["name"], f["begin"])
                        if k not in found:
                            found[k] = f
                            new.append(f)
                    counted["new"] += len(new)
                    if new and on_files:
                        on_files(new)

                try:
                    part, bad = client.query_files_chunked(
                        channel, f"{day} 00:00:00", f"{day} 23:59:59", chunk_minutes=plan.minutes,
                        chunk_timeout=plan.timeout, variant=VARIANTS[vname], progress=progress,
                        cancelled=cancelled, on_part=on_part)
                except dvrip.DVRIPAuthError:
                    raise
                except (OSError, dvrip.DVRIPError) as exc:
                    dead.add(key)
                    last_error = str(exc) or type(exc).__name__
                    log(f"{day} [{vname}]: ה-NVR לא ענה - {last_error}")
                    if not _reconnect(client):
                        raise dvrip.DVRIPError("החיבור ל-NVR אבד באמצע החיפוש")
                    continue

                answered = True
                failed_windows += [f"{day} {w}" for w in bad]
                day_files += len(part)
                host = getattr(client, "host", None)
                if part and host and vname != "standard":
                    LEARNED[host] = vname          # next time this recorder is asked this way first
                log(f"{day} [{vname}] סיכום: {len(part)} קבצים ({counted['new']} חדשים)")

    if not answered and not found and last_error and not cancelled():
        raise dvrip.DVRIPError(f"ה-NVR לא ענה לאף שיטת חיפוש ({last_error})")
    return sorted(found.values(), key=lambda f: f["begin"]), failed_windows
