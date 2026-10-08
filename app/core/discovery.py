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


def resolve_single_camera_via_onvif(host: str, onvif_port: int, username: str, password: str,
                                     timeout: float = 6.0) -> tuple[str, int, str] | None:
    """Quick, single-profile ONVIF probe for one standalone camera (not an NVR): ask the device
    itself for its first video profile's RTSP stream URI. This is the standards-based way to find
    a camera's real stream address - unlike a fixed list of per-brand guessed paths, it works for
    any camera that speaks ONVIF at all, regardless of brand or model. Returns (host, port, path)
    for building the RTSP URL, correcting for the common case of a NAT-ed device that advertises
    its own LAN address. Returns None (never raises) if the device doesn't answer ONVIF in time,
    has no profiles, or anything else goes wrong - callers are expected to fall back to
    path-guessing in that case."""
    import asyncio
    import ipaddress
    from urllib.parse import urlparse

    async def _one():
        from onvif import ONVIFCamera

        cam = ONVIFCamera(host, onvif_port, username, password, wsdl_dir=wsdl_dir(), no_cache=True)
        await asyncio.wait_for(cam.update_xaddrs(), timeout=timeout)
        media = await cam.create_media_service()
        profiles = await asyncio.wait_for(media.GetProfiles(), timeout=timeout)
        if not profiles:
            return None
        stream_setup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
        uri_resp = await asyncio.wait_for(
            media.GetStreamUri({"StreamSetup": stream_setup, "ProfileToken": profiles[0].token}),
            timeout=timeout,
        )
        return uri_resp.Uri

    try:
        uri = asyncio.run(_one())
    except Exception:          # noqa: BLE001 - this is a best-effort probe, never surfaced as an error
        return None
    if not uri:
        return None

    parsed = urlparse(uri)
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    resolved_host = parsed.hostname or host

    def _is_private(h: str) -> bool:
        try:
            return ipaddress.ip_address(h).is_private
        except ValueError:
            return False

    if resolved_host != host and _is_private(resolved_host) and not _is_private(host):
        # Behind NAT the camera advertised its own LAN address; keep what the user configured.
        return host, parsed.port or 554, path
    return resolved_host, parsed.port or 554, path


def _enumerate_hikvision(nvr_cfg: CameraConfig) -> list[CameraConfig]:
    """Hikvision (and Hikvision-OEM, e.g. many 'Annke'/'Hiwatch'-style devices) ISAPI: a plain
    HTTP + XML management API, documented at https://tpp.hikvision.com/Wiki/ISAPI/, authenticated
    with HTTP Digest. Reads the device's real channel list from /ISAPI/Streaming/channels and
    builds one CameraConfig per main-stream channel, each a plain RTSP camera at
    /Streaming/Channels/<id> - the exact path ISAPI's own docs give for playback, and the same one
    this program's brand-path list already tries first for Hikvision gear."""
    import urllib.error
    import urllib.request
    import xml.etree.ElementTree as ET

    port = nvr_cfg.onvif_port or 80
    base = f"http://{nvr_cfg.host}:{port}"
    pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pwd_mgr.add_password(None, base, nvr_cfg.username, nvr_cfg.password)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pwd_mgr))

    try:
        with opener.open(f"{base}/ISAPI/Streaming/channels", timeout=8) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError(
                f"שם המשתמש או הסיסמה של ה-NVR ({nvr_cfg.host}:{port}) שגויים.") from exc
        raise RuntimeError(f"ה-NVR ענה אבל לא כמו מכשיר Hikvision/ISAPI (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(
            f"אין חיבור ל-{nvr_cfg.host}:{port} ({exc}). "
            f"בדוק IP ופורט (ISAPI לרוב 80, לפעמים 8000) ופורט-פורוורד בנתב.") from exc

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RuntimeError(f"ה-NVR ענה אבל לא בפורמט ISAPI XML צפוי: {exc}") from exc

    ns = {"h": "http://www.hikvision.com/ver20/XMLSchema"}
    channels: list[tuple[str, str]] = []
    for ch in root.findall("h:StreamingChannel", ns) or root.findall("StreamingChannel"):
        cid = (ch.findtext("h:id", namespaces=ns) or ch.findtext("id") or "").strip()
        if not cid or not cid.endswith("01"):   # keep main streams (xx01) only; skip sub-streams (xx02...)
            continue
        name = (ch.findtext("h:channelName", namespaces=ns) or ch.findtext("channelName") or "").strip()
        channels.append((cid, name))

    if not channels:
        raise RuntimeError("ה-NVR ענה ב-ISAPI, אבל לא דיווח אף ערוץ (Streaming/channels חזר ריק).")

    return [
        CameraConfig(
            name=f"{nvr_cfg.name} / {name or f'ערוץ {i + 1}'}",
            host=nvr_cfg.host,
            port=nvr_cfg.port,
            username=nvr_cfg.username,
            password=nvr_cfg.password,
            rtsp_path=f"/Streaming/Channels/{cid}",
            channel=i,
            parent_nvr_id=nvr_cfg.id,
        )
        for i, (cid, name) in enumerate(channels)
    ]


def _enumerate_dahua_rtsp(nvr_cfg: CameraConfig) -> list[CameraConfig]:
    """Dahua/Amcrest-family NVRs: their own configuration/channel-list CGI API is no longer
    published - Dahua now requires signing an NDA to get the current version, and public forum
    copies are old enough to be unreliable (some report Dahua blocking cgi-bin access entirely on
    newer firmware). Implementing against that would be guessing at an API we cannot verify.

    What IS still public and stable across virtually all Dahua/Amcrest/OEM devices is the RTSP
    URL pattern itself: rtsp://user:pass@host:port/cam/realmonitor?channel=N&subtype=0 (confirmed
    across multiple independent, current sources). So instead of asking the device for its
    channel list, this builds that URL for channel=1..nvr_cfg.channel (the count the person typed
    in the dialog) and verifies each one with a real RTSP login (check_rtsp_auth, same check used
    everywhere else here) - only channels that actually answer with the right credentials are
    reported, and a channel number with no camera on it is just skipped, not an error."""
    from app.core.rtsp_auth import AuthResult, check_rtsp_auth

    count = nvr_cfg.channel or 1
    result = []
    for n in range(1, count + 1):
        path = f"/cam/realmonitor?channel={n}&subtype=0"
        res = check_rtsp_auth(nvr_cfg.host, nvr_cfg.port, path, nvr_cfg.username, nvr_cfg.password)
        if res == AuthResult.BAD_CREDENTIALS:
            raise RuntimeError(f"שם המשתמש או הסיסמה של ה-NVR ({nvr_cfg.host}:{nvr_cfg.port}) שגויים.")
        if res == AuthResult.UNREACHABLE:
            if n == 1:
                raise RuntimeError(
                    f"אין חיבור RTSP ל-{nvr_cfg.host}:{nvr_cfg.port}. בדוק IP ופורט (לרוב 554).")
            continue   # device stopped answering partway through - treat as "no more channels"
        if res == AuthResult.ERROR:
            continue   # this channel number doesn't exist on the device (e.g. 404) - just skip it
        result.append(CameraConfig(
            name=f"{nvr_cfg.name} / ערוץ {n}",
            host=nvr_cfg.host,
            port=nvr_cfg.port,
            username=nvr_cfg.username,
            password=nvr_cfg.password,
            rtsp_path=path,
            channel=n - 1,
            parent_nvr_id=nvr_cfg.id,
        ))

    if not result:
        raise RuntimeError(
            f"אף ערוץ מתוך {count} לא ענה בהצלחה. בדוק את מספר הערוצים שהזנת, ושה-NVR אכן תומך "
            f"בנתיב הסטנדרטי cam/realmonitor (רוב ה-Dahua/Amcrest תומכים - אם לא, נסה ONVIF).")
    return result


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
    if nvr_cfg.protocol == "hikvision":
        return _enumerate_hikvision(nvr_cfg)
    if nvr_cfg.protocol == "dahua_rtsp":
        return _enumerate_dahua_rtsp(nvr_cfg)

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
