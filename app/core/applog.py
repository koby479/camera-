"""Log file (stdout/stderr -> %LOCALAPPDATA%\\UniversalCamViewer\\app.log). Needed because a windowed
program has no console: without this every message, including the stream statistics, is lost.
The previous run's log is kept as app.prev.log, so a problem can still be looked at after a restart.
Standard library only (start.pyw imports this before anything else)."""
from __future__ import annotations

import faulthandler
import os
import sys
from pathlib import Path


def log_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "UniversalCamViewer"


def setup() -> Path | None:
    d = log_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        path = d / "app.log"
        try:
            if path.exists():
                os.replace(path, d / "app.prev.log")
        except OSError:
            pass
        f = open(path, "w", encoding="utf-8", buffering=1)
    except OSError:
        return None
    sys.stdout = sys.stderr = f
    try:
        faulthandler.enable(f)            # a hard crash (native code) leaves a traceback in the log
    except (RuntimeError, ValueError, OSError):
        pass
    return path
