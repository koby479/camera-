"""
Different camera brands expose their main RTSP stream at different paths.
If the user doesn't know theirs, we try the common ones in order until one
actually returns valid video frames. This list is compiled from ONVIF/RTSP
open-source tooling (getOnvifInfo, onvif-finder, libonvif) and vendor docs.
"""

COMMON_RTSP_PATHS = [
    "/Streaming/Channels/101",       # Hikvision + many Hikvision-OEM (incl. many "Annke"-style) cameras
    "/Streaming/Channels/1",
    "/h264/ch1/main/av_stream",      # Some Hikvision OEM variants
    "/cam/realmonitor?channel=1&subtype=0",  # Dahua + Dahua-OEM
    "/live/ch0",                     # Generic / some cheap Chinese OEM DVRs
    "/live/ch00_0",                  # TVT / some OEM chipsets
    "/onvif1",                       # Generic ONVIF profile name
    "/11",                           # Some ultra-cheap OEM chipsets use numeric paths
    "/",                             # last resort: bare root (a few devices serve directly here)
]
