import json
import tempfile
import unittest
from pathlib import Path

from app.core import scan, store
from app.core.config import CameraConfig


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = (store.APP_DIR, store.CONFIG_FILE)
        store.APP_DIR = Path(self.tmp.name)
        store.CONFIG_FILE = store.APP_DIR / "devices.json"

    def tearDown(self):
        store.APP_DIR, store.CONFIG_FILE = self._old
        self.tmp.cleanup()

    def test_round_trip(self):
        nvr = CameraConfig(name="NVR", host="1.2.3.4", port=34567, protocol="dvrip", password="p@ss:/")
        store.save_all([nvr], [CameraConfig(name="cam")])
        nvrs, singles = store.load_all()
        self.assertEqual(nvrs[0], nvr)
        self.assertEqual(singles[0].name, "cam")

    def test_save_leaves_no_temp_file(self):
        store.save_all([], [])
        self.assertEqual([p.name for p in store.APP_DIR.iterdir()], ["devices.json"])

    def test_broken_file_does_not_stop_startup_and_is_kept(self):
        store.CONFIG_FILE.write_text('{"nvrs": [', encoding="utf-8")
        self.assertEqual(store.load_all(), ([], []))
        self.assertTrue((store.APP_DIR / "devices.json.broken").exists())

    def test_unknown_fields_from_another_version_are_ignored(self):
        store.CONFIG_FILE.write_text(json.dumps({"nvrs": [{"name": "x", "host": "h", "future_field": 1}],
                                                 "singles": ["junk"]}), encoding="utf-8")
        nvrs, singles = store.load_all()
        self.assertEqual((nvrs[0].name, singles), ("x", []))


class ScanTargetTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(scan._parse_targets("10.0.0.5"), ["10.0.0.5"])
        self.assertEqual(scan._parse_targets("10.0.0.1-3"), ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertEqual(len(scan._parse_targets("192.168.1.0/24")), 254)

    def test_huge_range_is_refused(self):
        with self.assertRaises(ValueError):
            scan._parse_targets("10.0.0.0/8")

    def test_garbage_gives_a_readable_error(self):
        with self.assertRaises(ValueError) as cm:
            scan._parse_targets("1.2.3.4-banana")
        self.assertIn("יעד סריקה לא תקין", str(cm.exception))

    def test_xm_port_is_scanned(self):
        self.assertIn(34567, scan.COMMON_PORTS)


if __name__ == "__main__":
    unittest.main()
