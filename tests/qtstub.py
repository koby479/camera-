"""Just enough of PyQt6.QtCore / QtGui for the player-engine tests on a machine without PyQt6.
install() returns a function that removes the stubs again, so other tests are not affected."""
import sys
import threading
import types


class _Sig:
    def emit(self, *a):
        pass

    def connect(self, *a, **k):
        pass


def _pyqtSignal(*a):
    return _Sig


class QThread:
    def __init__(self, parent=None):
        for k, v in list(type(self).__dict__.items()):
            if v is _Sig:
                setattr(self, k, _Sig())
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self.run, daemon=True)
        self._t.start()

    def wait(self, ms=0):
        if self._t:
            self._t.join(ms / 1000)

    def isRunning(self):
        return bool(self._t and self._t.is_alive())


class QImage:
    class Format:
        Format_RGB888 = 1

    def __init__(self, *a):
        pass

    def copy(self):
        return self


def install():
    names = ["PyQt6", "PyQt6.QtCore", "PyQt6.QtGui"]
    saved = {n: sys.modules.get(n) for n in names}
    core = types.ModuleType("PyQt6.QtCore")
    core.QThread, core.pyqtSignal = QThread, _pyqtSignal
    gui = types.ModuleType("PyQt6.QtGui")
    gui.QImage = QImage
    sys.modules.update({"PyQt6": types.ModuleType("PyQt6"), "PyQt6.QtCore": core, "PyQt6.QtGui": gui})

    def undo():
        for n, mod in saved.items():
            if mod is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = mod
        for n in ("app.core.playerengine",):
            sys.modules.pop(n, None)
    return undo
