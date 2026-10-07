import http.server
import json
import threading
import unittest

from app.core import telemetry
from app.core.config import CameraConfig


class _Handler(http.server.BaseHTTPRequestHandler):
    received = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        _Handler.received.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')


class TelemetryPayloadTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(telemetry.settings._cache or {})
        telemetry.settings._cache = {}

    def tearDown(self):
        telemetry.settings._cache = self._saved

    def _nvr(self):
        return CameraConfig(name="max kosher", host="83.229.22.45", port=34567, username="admin2",
                            password="superSecret123", protocol="dvrip", channel=0)

    def test_never_includes_username_or_password(self):
        payload = telemetry.build_payload([self._nvr()], [])
        dumped = json.dumps(payload)
        self.assertNotIn("superSecret123", dumped)
        self.assertNotIn("admin2", dumped)
        for d in payload["devices"]:
            self.assertEqual(set(d.keys()), {"name", "host", "port", "protocol", "channel", "is_nvr"})

    def test_includes_the_allowed_fields(self):
        payload = telemetry.build_payload([self._nvr()], [])
        d = payload["devices"][0]
        self.assertEqual((d["name"], d["host"], d["port"], d["protocol"], d["channel"]),
                         ("max kosher", "83.229.22.45", 34567, "dvrip", 0))
        self.assertIn("install_id", payload)
        self.assertIn("version", payload)
        self.assertEqual(payload["device_count"], 1)

    def test_computer_name_only_when_separately_enabled(self):
        payload = telemetry.build_payload([], [])
        self.assertNotIn("computer", payload)
        telemetry.settings.put("telemetry_share_computer_name", True)
        payload = telemetry.build_payload([], [])
        self.assertIn("computer", payload)

    def test_install_id_is_stable_and_not_the_windows_user(self):
        import getpass
        first = telemetry.install_id()
        second = telemetry.install_id()
        self.assertEqual(first, second)
        self.assertNotEqual(first, getpass.getuser())

    def test_enabled_flag_round_trip(self):
        self.assertFalse(telemetry.enabled())
        self.assertFalse(telemetry.consent_asked())
        telemetry.set_enabled(True)
        self.assertTrue(telemetry.enabled())
        self.assertTrue(telemetry.consent_asked())
        telemetry.set_enabled(False)
        self.assertFalse(telemetry.enabled())
        self.assertTrue(telemetry.consent_asked())

    def test_no_webhook_configured_sends_nothing(self):
        telemetry.settings.put("telemetry_webhook", "")
        result = telemetry.send_now([self._nvr()], [])
        self.assertFalse(result.ok)


class TelemetrySendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Handler.received.clear()
        self._saved = dict(telemetry.settings._cache or {})
        telemetry.settings._cache = {}
        telemetry.settings.put("telemetry_webhook", self.url)

    def tearDown(self):
        telemetry.settings._cache = self._saved

    def test_send_now_posts_json_and_the_server_sees_no_secret(self):
        nvr = CameraConfig(name="n", host="1.2.3.4", port=34567, username="u", password="p", protocol="dvrip")
        result = telemetry.send_now([nvr], [])
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(len(_Handler.received), 1)
        dumped = json.dumps(_Handler.received[0])
        self.assertNotIn('"u"', dumped)
        self.assertNotIn('"p"', dumped)

    def test_reporter_only_sends_while_enabled(self):
        telemetry.set_enabled(False)
        reporter = telemetry.Reporter(lambda: ([], []))
        reporter.start()
        reporter.notify_changed()
        import time
        time.sleep(0.3)
        reporter.stop()
        self.assertEqual(len(_Handler.received), 0)

    def test_reporter_sends_once_enabled(self):
        telemetry.set_enabled(True)
        reporter = telemetry.Reporter(lambda: ([], []))
        reporter.start()
        reporter.notify_changed()          # otherwise the first report only fires after the 5s startup delay
        import time
        time.sleep(0.5)
        reporter.stop()
        self.assertGreaterEqual(len(_Handler.received), 1)


if __name__ == "__main__":
    unittest.main()
