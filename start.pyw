"""Double-click to start Universal Cam Viewer (no console window, no commands).
Output / errors go to  %LOCALAPPDATA%\\UniversalCamViewer\\app.log  (handy when something misbehaves)."""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

from app.core import applog

applog.setup()                             # pythonw has no console: keep prints instead of losing them

try:
    from app.main import main
    main()
except SystemExit:
    raise
except Exception:                          # noqa: BLE001
    traceback.print_exc()
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, traceback.format_exc()[-800:], "Universal Cam Viewer", 0x10)
    except Exception:                      # noqa: BLE001
        pass
