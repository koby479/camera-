import unittest
from pathlib import Path

from app.core import autostart


class FakeReg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    class _Key:
        def __init__(self, reg):
            self.reg = reg

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path, reserved=0, access=0):
        assert root == "HKCU" and path == autostart.RUN_KEY
        return self._Key(self)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 1

    def SetValueEx(self, key, name, _r, _t, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class AutostartTests(unittest.TestCase):
    EXE = Path(r"C:\Users\me\Desktop\UniversalCamViewer.exe")

    def test_enable_writes_a_quoted_command_that_starts_minimised(self):
        reg = FakeReg()
        self.assertFalse(autostart.is_enabled(reg))
        autostart.set_enabled(True, self.EXE, reg)
        self.assertTrue(autostart.is_enabled(reg))
        self.assertEqual(reg.values[autostart.VALUE_NAME], f'"{self.EXE}" --minimized')

    def test_disable_removes_it_and_is_safe_when_it_was_never_set(self):
        reg = FakeReg()
        autostart.set_enabled(False, self.EXE, reg)
        autostart.set_enabled(True, self.EXE, reg)
        autostart.set_enabled(False, self.EXE, reg)
        self.assertFalse(autostart.is_enabled(reg))

    def test_refresh_follows_a_moved_program_but_does_not_turn_it_on(self):
        reg = FakeReg()
        autostart.refresh(self.EXE, reg)
        self.assertFalse(autostart.is_enabled(reg))
        autostart.set_enabled(True, Path(r"D:\old\UniversalCamViewer.exe"), reg)
        autostart.refresh(self.EXE, reg)
        self.assertEqual(reg.values[autostart.VALUE_NAME], f'"{self.EXE}" --minimized')


if __name__ == "__main__":
    unittest.main()
