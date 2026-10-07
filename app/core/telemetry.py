"""Opt-in reporting to the developer's management dashboard (a Google Sheet, see tools/dashboard_apps_script.gs).

Nothing is sent until the person explicitly agrees (see app/ui/telemetry_consent.py). What is sent, every time, in
full, is exactly this and nothing else:
  - app version and a per-install id (a random id generated once and stored locally - not the Windows user name
    or any personal identifier)
  - the time of the report and the optional computer name IF the person separately turned that on
  - for every NVR / camera configured in the program: its label, host, port, channel count and protocol

USERNAMES AND PASSWORDS OF CAMERAS ARE NEVER INCLUDED. That line is enforced in code (_camera_row below), not
only by this comment - whoever edits this file must keep it that way: the whole point of asking consent is that
people can trust exactly what leaves their machine, and credentials are what turns a device list into a break-in
list for anyone who later reads the sheet."""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from app import __version__
from app.core import settings

TIMEOUT = 10
_MAX_RETRY_SLEEP = 60

STATUS_APPROVED = "approved"
STATUS_BLOCKED  = "blocked"
STATUS_UNKNOWN  = "unknown"
STATUS_ERROR    = "error"

BLOCK_MSG_DEFAULT = (
    "גישה חסומה\n\n"
    "ההתקנה הזו נחסמה על ידי מנהל המערכת.\n"
    "לפתיחת הגישה פנה למנהל המערכת.")



def install_id() -> str:
    """A random id created once per installation (not tied to the Windows user or the machine's hardware)."""
    value = settings.get("telemetry_install_id")
    if not value:
        value = uuid.uuid4().hex
        settings.put("telemetry_install_id", value)
    return value


def last_status() -> str:
    return str(settings.get("telemetry_last_status") or STATUS_UNKNOWN)


def save_status(status: str, message: str = "") -> None:
    settings.put("telemetry_last_status", status)
    if message:
        settings.put("telemetry_block_message", message)


def block_message() -> str:
    return str(settings.get("telemetry_block_message") or BLOCK_MSG_DEFAULT)


def enabled() -> bool:
    return bool(settings.get("telemetry_enabled", False))


def set_enabled(on: bool) -> None:
    settings.put("telemetry_enabled", bool(on))
    settings.put("telemetry_consent_asked", True)


def consent_asked() -> bool:
    return bool(settings.get("telemetry_consent_asked", False))


def webhook_url() -> str:
    """Where reports are sent. Empty = reporting has no destination configured in this build, so nothing is sent
    even if the person said yes (set by whoever deploys tools/dashboard_apps_script.gs, see its header)."""
    return str(settings.get("telemetry_webhook", "") or DEFAULT_WEBHOOK)


DEFAULT_WEBHOOK = ""   # fill in after deploying the Apps Script, or leave empty and set it via settings.put


def _camera_row(cfg) -> dict:
    """Exactly the fields that are allowed to leave the machine for one configured device. Deliberately built
    field-by-field (never cfg.__dict__ or similar) so a future field added to CameraConfig - including a secret
    one - cannot silently start being reported."""
    return {
        "name": cfg.name,
        "host": cfg.host,
        "port": cfg.port,
        "protocol": cfg.protocol,
        "channel": cfg.channel if cfg.protocol == "dvrip" else None,
        "is_nvr": cfg.parent_nvr_id is None and cfg.protocol in ("dvrip", "onvif"),
    }


def build_payload(nvrs: list, singles: list) -> dict:
    devices = [_camera_row(c) for c in list(nvrs) + list(singles)]
    payload = {
        "install_id": install_id(),
        "version": __version__,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device_count": len(devices),
        "devices": devices,
        "os": sys.platform,
    }
    if settings.get("telemetry_share_computer_name", False):
        import socket
        try:
            payload["computer"] = socket.gethostname()
        except OSError:
            pass
    return payload


@dataclass
class SendResult:
    ok: bool
    detail: str


def send_now(nvrs: list, singles: list, timeout: float = TIMEOUT) -> SendResult:
    """Blocking send; call from a worker thread. Never raises - failure is reported back, never shown as an error
    the person has to deal with (this is a convenience for the developer, not something that should interrupt use)."""
    url = webhook_url()
    if not url:
        return SendResult(False, "לא הוגדר יעד לדיווח בגרסה הזו")
    body = json.dumps(build_payload(nvrs, singles)).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "UniversalCamViewer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4096).decode("utf-8", "ignore")
        try:
            data = json.loads(raw)
            status = str(data.get("status") or STATUS_UNKNOWN)
            msg    = str(data.get("message") or "")
            save_status(status, msg)
        except (ValueError, KeyError):
            status = STATUS_ERROR
        return SendResult(True, f"נשלח (status: {status})")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return SendResult(False, str(exc))


class Reporter:
    """Background sender owned by the main window: one report shortly after start, another whenever the device
    list changes (debounced), a light retry if the network was down. Does nothing while reporting is off."""

    def __init__(self, get_devices):
        self._get_devices = get_devices           # callable -> (nvrs, singles), read fresh at send time
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: SendResult | None = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="telemetry")
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._dirty.set()

    def notify_changed(self):
        self._dirty.set()

    def _run(self):
        self._dirty.wait(5.0)             # first report shortly after startup, not during the busy first moment
        backoff = 5.0
        while not self._stop.is_set():
            self._dirty.clear()
            if enabled():
                nvrs, singles = self._get_devices()
                self.last_result = send_now(nvrs, singles)
                backoff = 5.0 if self.last_result.ok else min(backoff * 2, _MAX_RETRY_SLEEP)
                wait = None if self.last_result.ok else backoff
            else:
                wait = None                # nothing to do until a setting or the device list changes
            if self._stop.is_set():
                return
            self._dirty.wait(wait)
