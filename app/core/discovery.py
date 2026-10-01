"""
ONVIF discovery helpers.

Two separate jobs:
1. find_devices_on_network()  -> WS-Discovery broadcast, returns raw
   ONVIF endpoints found on the LAN (used for an "auto scan" button).
2. enumerate_nvr_channels(nvr_cfg) -> logs into a specific NVR / camera
   via ONVIF (GetProfiles) and returns one CameraConfig per channel it
   exposes, each with its own RTSP URL pulled straight from the device.
   This is what powers "click the NVR node in the sidebar -> see its
   cameras".
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from app.core.camera import CameraConfig


def wsdl_dir() -> str | None:
    """When frozen by PyInstaller, the onvif library's bundled WSDL files
    can fail to resolve via their normal relative-path lookup (a known
    issue with onvif-zeep-async under PyInstaller onefile). We ship the
    WSDL folder explicitly via --add-data and point the library straight
    at it. In normal (non-frozen) dev runs, return None and let the
    library use its own bundled default."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidate = os.path.join(base, "onvif", "wsdl")
        if os.path.isdir(candidate):
            return candidate
    return None


@dataclass
class DiscoveredDevice:
    address: str
    xaddrs: list[str]
    scopes: list[str]


def find_devices_on_network(timeout: float = 4.0) -> list[DiscoveredDevice]:
    """Broadcast WS-Discovery and return whatever ONVIF-capable devices answer.
    Safe to call with nothing on the network -- just returns an empty list."""
    from wsdiscovery.discovery import ThreadedWSDiscovery as WSDiscovery

    wsd = WSDiscovery()
    found: list[DiscoveredDevice] = []
    try:
        wsd.start()
        services = wsd.searchServices(timeout=timeout)
        for s in services:
            found.append(
                DiscoveredDevice(
                    address=str(s.getEPR()),
                    xaddrs=list(s.getXAddrs()),
                    scopes=[str(sc) for sc in s.getScopes()],
                )
            )
    finally:
        wsd.stop()
    return found


async def _enumerate_async(host: str, onvif_port: int, username: str, password: str, state: dict):
    from onvif import ONVIFCamera  # onvif-zeep-async

    state["step"] = "שליפת יכולות המכשיר (GetServices/GetCapabilities)"
    cam = ONVIFCamera(host, onvif_port, username, password, wsdl_dir=wsdl_dir(), no_cache=True)
    await asyncio.wait_for(cam.update_xaddrs(), timeout=15)

    state["step"] = "שליפת רשימת הפרופילים (GetProfiles)"
    media = await cam.create_media_service()
    profiles = await asyncio.wait_for(media.GetProfiles(), timeout=15)

    channels = []
    for profile in profiles:
        stream_setup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        state["step"] = f"שליפת כתובת הווידאו של ערוץ {profile.Name or profile.token} (GetStreamUri)"
        uri_resp = await asyncio.wait_for(
            media.GetStreamUri({"StreamSetup": stream_setup, "ProfileToken": profile.token}),
            timeout=15,
        )
        channels.append((profile.Name or profile.token, uri_resp.Uri))
    return channels


def _enumerate_dvrip(nvr_cfg: CameraConfig) -> list[CameraConfig]:
    """XM / Provision CMS protocol: log in (this really verifies the password)
    and create one channel entry per ChannelNum the recorder reports."""
    from app.core.dvrip import DVRIPClient, DVRIPAuthError, DVRIPError

    client = DVRIPClient(nvr_cfg.host, nvr_cfg.port, nvr_cfg.username, nvr_cfg.password)
    try:
        client.connect()
        info = client.login()
    except DVRIPAuthError as exc:
        raise RuntimeError(
            f"שם המשתמש או הסיסמה של ה-NVR ({nvr_cfg.host}:{nvr_cfg.port}) שגויים ({exc.code})."
        ) from exc
    except DVRIPError as exc:
        raise RuntimeError(f"ה-NVR ענה אבל לא כמו מכשיר XM/Provision: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(
            f"אין חיבור ל-{nvr_cfg.host}:{nvr_cfg.port} ({exc}). "
            f"בדוק IP ופורט (ב-Provision CMS3 זה בדרך כלל 34567) ופורט-פורוורד בנתב."
        ) from exc
    finally:
        client.close()

    count = int(info.get("ChannelNum") or 0) or 1
    return [
        CameraConfig(
            name=f"{nvr_cfg.name} / ערוץ {i + 1}",
            host=nvr_cfg.host,
            port=nvr_cfg.port,
            username=nvr_cfg.username,
            password=nvr_cfg.password,
            protocol="dvrip",
            stream="Extra1",          # light sub-stream in the grid; Main on double-click
            channel=i,
            parent_nvr_id=nvr_cfg.id,
        )
        for i in range(count)
    ]


def enumerate_nvr_channels(nvr_cfg: CameraConfig) -> list[CameraConfig]:
    """Query an NVR (or any ONVIF device) for every channel/profile it exposes
    and turn each one into a ready-to-use CameraConfig, parented to the NVR.
    NOTE: this makes blocking network calls (each capped at 8s via
    asyncio.wait_for) - callers on a GUI thread MUST run this in a background
    QThread (see app/ui/nvr_probe_worker.py), never call it directly from a
    Qt slot or the whole window will freeze while it waits."""
    if nvr_cfg.protocol == "dvrip":
        return _enumerate_dvrip(nvr_cfg)

    import socket

    # Step 0: plain TCP check, so "port closed / blocked" is told apart from
    # "port open but ONVIF not answering".
    try:
        socket.create_connection((nvr_cfg.host, nvr_cfg.onvif_port), timeout=6).close()
    except OSError as exc:
        raise RuntimeError(
            f"אין חיבור TCP ל-{nvr_cfg.host}:{nvr_cfg.onvif_port} ({exc}).\n"
            f"הפורט סגור/חסום, או שזה לא פורט ה-ONVIF של ה-NVR. אם ב-NVR מאחורי נתב, "
            f"צריך את הפורט החיצוני (פורט-פורוורד) שמוביל ל-ONVIF של ה-NVR."
        ) from exc

    state = {"step": ""}
    try:
        channels = asyncio.run(
            _enumerate_async(nvr_cfg.host, nvr_cfg.onvif_port, nvr_cfg.username, nvr_cfg.password, state)
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"הפורט {nvr_cfg.host}:{nvr_cfg.onvif_port} פתוח, אבל ה-NVR לא ענה בזמן בשלב: {state['step']}.\n"
            f"אם השלב הוא GetProfiles/GetStreamUri, ה-NVR כנראה מחזיר כתובת פנימית (192.168...) "
            f"שלא נגישה מבחוץ. הרץ tools\\diagnose_nvr.py כדי לראות בדיוק."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a message box
        raise RuntimeError(
            f"נכשל לתקשר עם ה-NVR ({nvr_cfg.host}) דרך ONVIF בשלב: {state['step']}.\n{exc}"
        ) from exc

    from urllib.parse import urlparse

    from app.core.rtsp_auth import AuthResult, check_rtsp_auth

    import ipaddress

    def _is_private(h: str | None) -> bool:
        try:
            return ipaddress.ip_address(h).is_private if h else False
        except ValueError:
            return False

    def _endpoint(uri: str):
        """Behind NAT the NVR advertises its LAN address (192.168.x.x) in the
        stream URI. From outside that is unreachable, so use the address the
        user configured (and the configured RTSP port) instead."""
        parsed = urlparse(uri)
        path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        if parsed.hostname and parsed.hostname != nvr_cfg.host and (
            _is_private(parsed.hostname) and not _is_private(nvr_cfg.host)
        ):
            return nvr_cfg.host, nvr_cfg.port, path
        return parsed.hostname or nvr_cfg.host, parsed.port or 554, path

    endpoints = [(name, *_endpoint(uri)) for name, uri in channels]

    # ONVIF answering is NOT proof the credentials are right (many NVRs serve
    # GetProfiles without auth). Verify with a real RTSP login on the first
    # channel; fail loudly instead of showing a fake "connected" tree.
    if endpoints:
        _, h0, port0, path0 = endpoints[0]
        res = check_rtsp_auth(h0, port0, path0, nvr_cfg.username, nvr_cfg.password)
        if res == AuthResult.BAD_CREDENTIALS:
            raise RuntimeError(
                f"שם המשתמש או הסיסמה של ה-NVR ({nvr_cfg.host}) שגויים. "
                f"ה-NVR מחזיר את רשימת הערוצים גם בלי אימות, אבל ההתחברות לווידאו נדחתה."
            )
        if res == AuthResult.UNREACHABLE:
            raise RuntimeError(
                f"ה-NVR ענה ב-ONVIF, אבל פורט הווידאו (RTSP) {h0}:{port0} לא נגיש.\n"
                f"ערוך את ה-NVR ובדוק את שדה 'פורט RTSP' (הפורט החיצוני שמוביל ל-554 של ה-NVR)."
            )

    result = []
    for index, (name, h, port, path) in enumerate(endpoints):
        result.append(
            CameraConfig(
                name=f"{nvr_cfg.name} / {name}",
                host=h,
                port=port,
                username=nvr_cfg.username,
                password=nvr_cfg.password,
                rtsp_path=path,
                channel=index,
                parent_nvr_id=nvr_cfg.id,
            )
        )
    return result
