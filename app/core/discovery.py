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


async def _enumerate_async(host: str, onvif_port: int, username: str, password: str):
    from onvif import ONVIFCamera  # onvif-zeep-async

    cam = ONVIFCamera(host, onvif_port, username, password, wsdl_dir=wsdl_dir(), no_cache=True)
    await asyncio.wait_for(cam.update_xaddrs(), timeout=8)

    media = await cam.create_media_service()
    profiles = await asyncio.wait_for(media.GetProfiles(), timeout=8)

    channels = []
    for profile in profiles:
        stream_setup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        uri_resp = await asyncio.wait_for(
            media.GetStreamUri({"StreamSetup": stream_setup, "ProfileToken": profile.token}),
            timeout=8,
        )
        channels.append((profile.Name or profile.token, uri_resp.Uri))
    return channels


def enumerate_nvr_channels(nvr_cfg: CameraConfig) -> list[CameraConfig]:
    """Query an NVR (or any ONVIF device) for every channel/profile it exposes
    and turn each one into a ready-to-use CameraConfig, parented to the NVR.
    NOTE: this makes blocking network calls (each capped at 8s via
    asyncio.wait_for) - callers on a GUI thread MUST run this in a background
    QThread (see app/ui/nvr_probe_worker.py), never call it directly from a
    Qt slot or the whole window will freeze while it waits."""
    try:
        channels = asyncio.run(
            _enumerate_async(nvr_cfg.host, nvr_cfg.onvif_port, nvr_cfg.username, nvr_cfg.password)
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"ה-NVR ({nvr_cfg.host}) לא הגיב תוך 8 שניות. בדוק שה-IP/פורט ONVIF נכונים "
            f"ושאין חומת אש שחוסמת."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a message box
        raise RuntimeError(f"נכשל לתקשר עם ה-NVR ({nvr_cfg.host}) דרך ONVIF: {exc}") from exc

    from urllib.parse import urlparse

    result = []
    for name, uri in channels:
        parsed = urlparse(uri)
        result.append(
            CameraConfig(
                name=f"{nvr_cfg.name} / {name}",
                host=parsed.hostname or nvr_cfg.host,
                port=parsed.port or 554,
                username=nvr_cfg.username,
                password=nvr_cfg.password,
                rtsp_path=parsed.path or "/",
                parent_nvr_id=nvr_cfg.id,
            )
        )
    return result
