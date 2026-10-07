"""Start with Windows (current user only, no admin rights): a value in HKCU\\...\\Run that starts the EXE with
--minimized, so it waits in the tray. Only meaningful for the EXE; from source it reports 'not available'."""
from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "UniversalCamViewer"


def available() -> bool:
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def _command(exe: Path | None = None) -> str:
    return f'"{Path(exe or sys.executable)}" --minimized'


def _winreg():
    import winreg
    return winreg


def is_enabled(reg=None) -> bool:
    reg = reg or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            reg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(on: bool, exe: Path | None = None, reg=None) -> None:
    reg = reg or _winreg()
    with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
        if on:
            reg.SetValueEx(key, VALUE_NAME, 0, reg.REG_SZ, _command(exe))
        else:
            try:
                reg.DeleteValue(key, VALUE_NAME)
            except OSError:
                pass


def refresh(exe: Path | None = None, reg=None) -> None:
    """If it is on, make sure it points at the EXE that is running now (the folder may have changed)."""
    reg = reg or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            current, _kind = reg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return
    if current != _command(exe):
        set_enabled(True, exe, reg)
