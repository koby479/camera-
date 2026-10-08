"""
Different camera brands expose their main RTSP stream at different paths.

This list is the LAST RESORT: app/core/camera.py tries ONVIF first (see
_try_onvif / discovery.resolve_single_camera_via_onvif), which asks the
camera itself for its real stream address and works for any brand that
speaks the ONVIF standard - which is most modern IP cameras. Only a camera
that answers to neither its configured ONVIF port nor any of these paths
genuinely falls outside what this program can reach without that vendor's
own closed SDK (common for some cloud-only consumer cameras).

This list is compiled from ONVIF/RTSP open-source tooling (getOnvifInfo,
onvif-finder, libonvif), ispyconnect's camera list, and vendor docs.
"""

COMMON_RTSP_PATHS = [
    "/Streaming/Channels/101",       # Hikvision + many Hikvision-OEM (incl. many "Annke"-style) cameras
    "/Streaming/Channels/1",
    "/h264/ch1/main/av_stream",      # Some Hikvision OEM variants
    "/cam/realmonitor?channel=1&subtype=0",  # Dahua + Dahua-OEM (incl. most Amcrest, Lorex, Q-See)
    "/live/ch0",                     # Generic / some cheap Chinese OEM DVRs
    "/live/ch00_0",                  # TVT / some OEM chipsets
    "/h264Preview_01_main",          # Reolink
    "/media/video1",                 # Uniview (UNV) + some OEM
    "/profile2/media.smp",           # Hanwha / Samsung Wisenet
    "/axis-media/media.amp",         # Axis
    "/videoMain",                    # Foscam + some older Foscam-OEM
    "/live.sdp",                     # Vivotek + some OEM
    "/video1",                       # Various cheap OEM / some Ubiquiti UniFi (older firmware)
    "/videoinput_1:0/h264_1/media.stm",  # Bosch
    "/onvif1",                       # Generic ONVIF profile name
    "/11",                           # Some ultra-cheap OEM chipsets use numeric paths
    "/",                             # last resort: bare root (a few devices serve directly here)
]
