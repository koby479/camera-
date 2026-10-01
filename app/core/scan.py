"""
Network scan: given a single IP or a range (e.g. 192.168.1.1-192.168.1.254
or 192.168.1.0/24), probe the ports cameras/NVRs typically use, and for any
host that responds, try a quick ONVIF handshake so the UI can tell the user
"this looks like a real camera/NVR" vs "something is open but not a camera".
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

COMMON_PORTS = {
    554: "RTSP",
    80: "HTTP/ONVIF",
    8000: "ONVIF (alt)",
    2020: "ONVIF (alt)",
    8080: "HTTP (alt)",
    8899: "Proprietary CMS (common on cheap Chinese cameras)",
}


@dataclass
class ScanHit:
    host: str
    open_ports: dict = field(default_factory=dict)   # port -> label
    onvif_ok: bool = False
    onvif_error: str | None = None
    manufacturer: str | None = None


def _parse_targets(spec: str) -> list[str]:
    spec = spec.strip()
    if "/" in spec:
        net = ipaddress.ip_network(spec, strict=False)
        return [str(ip) for ip in net.hosts()]
    if "-" in spec:
        start_s, end_s = spec.split("-", 1)
        start = ipaddress.IPv4Address(start_s.strip())
        # allow "192.168.1.1-254" shorthand
        end_s = end_s.strip()
        if "." not in end_s:
            end = ipaddress.IPv4Address(".".join(start_s.strip().split(".")[:3] + [end_s]))
        else:
            end = ipaddress.IPv4Address(end_s)
        return [str(ipaddress.IPv4Address(i)) for i in range(int(start), int(end) + 1)]
    # single IP
    return [spec]


def _probe_host(host: str, timeout: float = 0.6) -> ScanHit | None:
    open_ports = {}
    for port, label in COMMON_PORTS.items():
        try:
            with socket.create_connection((host, port), timeout=timeout):
                open_ports[port] = label
        except OSError:
            continue
    if not open_ports:
        return None
    return ScanHit(host=host, open_ports=open_ports)


async def _try_onvif(hit: ScanHit, username: str, password: str, timeout: float = 3.0):
    onvif_port = 80 if 80 in hit.open_ports else (8000 if 8000 in hit.open_ports else 2020)
    try:
        from onvif import ONVIFCamera
        from app.core.discovery import wsdl_dir

        cam = ONVIFCamera(hit.host, onvif_port, username, password, wsdl_dir=wsdl_dir(), no_cache=True)
        await asyncio.wait_for(cam.update_xaddrs(), timeout=timeout)
        device = await cam.create_devicemgmt_service()
        info = await asyncio.wait_for(device.GetDeviceInformation(), timeout=timeout)
        hit.onvif_ok = True
        hit.manufacturer = getattr(info, "Manufacturer", None)
    except Exception as exc:  # noqa: BLE001
        hit.onvif_ok = False
        hit.onvif_error = str(exc)


def scan(spec: str, username: str = "", password: str = "",
          progress_cb=None, max_workers: int = 40) -> list[ScanHit]:
    """Blocking call meant to be run in a worker thread (not the GUI thread).
    progress_cb(done, total) is called after each host is probed, if given."""
    targets = _parse_targets(spec)
    hits: list[ScanHit] = []
    total = len(targets)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for hit in pool.map(_probe_host, targets):
            done += 1
            if progress_cb:
                progress_cb(done, total)
            if hit:
                hits.append(hit)

    if username or password:
        async def _run_all():
            await asyncio.gather(*[_try_onvif(h, username, password) for h in hits])

        asyncio.run(_run_all())

    return hits
