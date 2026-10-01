"""Double-click to start Universal Cam Viewer (no console window, no commands).
Output / errors go to  %LOCALAPPDATA%\\UniversalCamViewer\\app.log  (handy when something misbehaves)."""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

log_dir = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "UniversalCamViewer")
try:
    os.makedirs(log_dir, exist_ok=True)
    log = open(os.path.join(log_dir, "app.log"), "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log          # pythonw has no console: keep prints instead of losing them
except OSError:
    pass

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
