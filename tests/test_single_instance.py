import unittest

try:
    from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer
    from app.ui import single_instance
    HAVE_QT = True
except ImportError:                                    # the other tests run without PyQt installed
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class SingleInstanceTests(unittest.TestCase):
    NAME = "UniversalCamViewer-test-instance"

    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def _spin(self, ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def test_nobody_listening_means_not_running(self):
        self.assertFalse(single_instance.is_running(name=self.NAME + "-none", timeout_ms=200))

    def test_second_copy_finds_the_first_and_asks_it_to_show(self):
        listener = single_instance.Listener(name=self.NAME)
        self.assertTrue(listener.ok)
        shown = []
        listener.show_requested.connect(lambda: shown.append(1))
        QTimer.singleShot(50, lambda: single_instance.is_running(True, 500, self.NAME))
        self._spin(600)
        self.assertEqual(shown, [1])


if __name__ == "__main__":
    unittest.main()
