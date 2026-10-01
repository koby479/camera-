import socket
import unittest

from app.core import dvrip, recsearch


class _FakeSock:
    def settimeout(self, _t):
        pass


class Nvr(dvrip.DVRIPClient):
    """No network. `behaviour` maps a variant key (its Type override, or 'standard') to
    'files' (returns recordings), 'empty' (answers with nothing) or 'silent' (times out).
    `days_with_files` limits which days have recordings."""

    def __init__(self, behaviour, days_with_files=None):
        super().__init__("x", 1, "u", "p")
        self.behaviour = behaviour
        self.days_with_files = days_with_files
        self.calls = []
        self.sock = _FakeSock()

    def connect(self):
        self.sock = _FakeSock()

    def login(self):
        return {}

    def close(self):
        self.sock = self.media_sock = None

    def query_files(self, channel, begin, end, file_type="h264", variant=None):
        vkey = (variant or {}).get("Type") or (variant or {}).get("DriverTypeMask") \
            or (variant or {}).get("Event") or ("int" if variant and "StreamType" in variant else "standard")
        self.calls.append((begin[:10], vkey))
        mode = self.behaviour.get(vkey, "empty")
        if mode == "silent":
            raise socket.timeout("timed out")
        if mode == "empty":
            return []
        if self.days_with_files is not None and begin[:10] not in self.days_with_files:
            return []
        return [{"name": f"{begin}", "begin": begin, "end": end, "size": 1}]


def search(nvr, mode, days=("2026-08-30",), **kw):
    return recsearch.run_search(nvr, 0, list(days), recsearch.plans_for(mode), **kw)


class RecSearchTests(unittest.TestCase):
    def test_standard_variant_finds_files(self):
        files, bad = search(Nvr({"standard": "files"}), "std2h")
        self.assertEqual(len(files), 12)
        self.assertEqual(bad, [])

    def test_auto_finds_files_only_an_alternative_variant_returns(self):
        nvr = Nvr({"standard": "empty", "*": "files"})       # standard answers, but with nothing
        files, _ = search(nvr, "auto")
        self.assertGreater(len(files), 0)

    def test_auto_survives_a_silent_standard_variant(self):
        nvr = Nvr({"standard": "silent", "*": "files"})
        files, _ = search(nvr, "auto")
        self.assertGreater(len(files), 0)
        standard_calls = [c for c in nvr.calls if c[1] == "standard"]
        self.assertLessEqual(len(standard_calls), 4)         # gave up quickly, did not grind all day

    def test_auto_falls_back_to_the_big_query_when_windows_find_nothing(self):
        nvr = Nvr({})                                        # everything answers empty
        files, _ = search(nvr, "auto")
        self.assertEqual(files, [])
        day_calls = [c for c in nvr.calls if c[1] == "standard"]
        self.assertEqual(len(day_calls), 12 + 1)             # 12 two-hour windows + one whole-day query

    def test_nothing_answers_at_all_raises(self):
        nvr = Nvr({k: "silent" for k in ("standard", "*", "h265", "0xFFFFFFFF", "int", "R")})
        with self.assertRaises(dvrip.DVRIPError):
            search(nvr, "auto")

    def test_all_empty_is_not_an_error(self):
        files, bad = search(Nvr({}), "wide")
        self.assertEqual((files, bad), ([], []))

    def test_wide_search_spans_days_and_skips_dead_variants(self):
        days = ["2026-08-30", "2026-08-29", "2026-08-28"]
        nvr = Nvr({"standard": "files", "*": "silent"}, days_with_files={"2026-08-29"})
        files, _ = search(nvr, "wide", days=days)
        self.assertEqual({f["begin"][:10] for f in files}, {"2026-08-29"})
        silent_days = {d for d, v in nvr.calls if v == "*"}
        self.assertEqual(silent_days, {"2026-08-30"})        # tried once, then skipped on later days

    def test_partial_results_reported_as_they_arrive(self):
        batches = []
        search(Nvr({"standard": "files"}), "std2h", on_files=batches.append)
        self.assertEqual(sum(len(b) for b in batches), 12)
        self.assertGreater(len(batches), 1)

    def test_no_duplicates_across_variants(self):
        nvr = Nvr({"standard": "files", "*": "files", "h265": "files"})
        files, _ = search(nvr, "wide")
        self.assertEqual(len(files), len({(f["name"], f["begin"]) for f in files}))

    def test_cancel_stops_early(self):
        nvr = Nvr({"standard": "files"})
        flag = {"stop": False}
        seen = []
        def on_files(new):
            seen.extend(new)
            flag["stop"] = True
        files, _ = search(nvr, "std2h", on_files=on_files, cancelled=lambda: flag["stop"])
        self.assertEqual(len(files), 1)

    def test_log_lines_name_the_variant(self):
        lines = []
        search(Nvr({"standard": "files"}), "std2h", log=lines.append)
        self.assertTrue(any("[standard]" in l for l in lines))

    def test_modes_all_have_plans(self):
        for key, _label in recsearch.MODES:
            self.assertTrue(recsearch.plans_for(key))


if __name__ == "__main__":
    unittest.main()
