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
from dataclasses import dataclass

from app.core.camera import CameraConfig


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

    cam = ONVIFCamera(host, onvif_port, username, password, no_cache=True)
    await cam.update_xaddrs()

    media = await cam.create_media_service()
    profiles = await media.GetProfiles()

    channels = []
    for profile in profiles:
        stream_setup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        uri_resp = await media.GetStreamUri(
            {"StreamSetup": stream_setup, "ProfileToken": profile.token}
        )
        channels.append((profile.Name or profile.token, uri_resp.Uri))
    return channels


def enumerate_nvr_channels(nvr_cfg: CameraConfig) -> list[CameraConfig]:
    """Query an NVR (or any ONVIF device) for every channel/profile it exposes
    and turn each one into a ready-to-use CameraConfig, parented to the NVR."""
    try:
        channels = asyncio.run(
            _enumerate_async(nvr_cfg.host, nvr_cfg.onvif_port, nvr_cfg.username, nvr_cfg.password)
        )
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
