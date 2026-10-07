import unittest

from app.core import alarms


class EventTextTests(unittest.TestCase):
    def test_known_names(self):
        self.assertEqual(alarms.event_text("VideoMotion"), "תנועה")
        self.assertEqual(alarms.event_text("HumanDetect"), "זיהוי אדם")

    def test_firmware_variant_with_prefix_and_suffix(self):
        self.assertEqual(alarms.event_text("appEventHumanDetectAlarm"), "זיהוי אדם")

    def test_unknown_name_is_shown_as_is_and_empty_is_generic(self):
        self.assertEqual(alarms.event_text("SomethingNew"), "SomethingNew")
        self.assertEqual(alarms.event_text(""), "אירוע")


if __name__ == "__main__":
    unittest.main()
