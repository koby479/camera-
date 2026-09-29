"""
Real credential check against an RTSP endpoint (no OpenCV needed).

Why this exists: many NVRs answer ONVIF discovery calls (GetProfiles /
GetStreamUri) WITHOUT checking the password, so "ONVIF call succeeded" says
nothing about whether the username/password are right. The only reliable
test is an RTSP DESCRIBE that goes through the 401 challenge and comes back
200 with our credentials.
"""
from __future__ import annotations

import base64
import hashlib
import re
import socket
from enum import Enum
from urllib.parse import quote


class AuthResult(Enum):
    OK = "ok"                    # 200 with our credentials
    BAD_CREDENTIALS = "bad"      # 401 even after sending credentials
    NO_AUTH_REQUIRED = "open"    # 200 with no credentials at all (device does not enforce auth)
    UNREACHABLE = "unreachable"  # cannot connect / timeout
    ERROR = "error"              # other RTSP status (404 etc.) - creds could not be judged


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _digest_header(user, pw, method, uri, params) -> str:
    realm, nonce = params.get("realm", ""), params.get("nonce", "")
    ha1 = _md5(f"{user}:{realm}:{pw}")
    ha2 = _md5(f"{method}:{uri}")
    if "qop" in params:
        nc, cnonce = "00000001", "0a4f113b"
        resp = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        extra = f', qop=auth, nc={nc}, cnonce="{cnonce}"'
    else:
        resp = _md5(f"{ha1}:{nonce}:{ha2}")
        extra = ""
    h = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
         f'uri="{uri}", response="{resp}"{extra}')
    if "opaque" in params:
        h += f', opaque="{params["opaque"]}"'
    return h


def _request(host, port, url, cseq, auth_header, timeout):
    req = f"DESCRIBE {url} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\nUser-Agent: UniversalCamViewer\r\n"
    if auth_header:
        req += f"Authorization: {auth_header}\r\n"
    req += "\r\n"
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    text = data.decode("latin-1", "replace")
    m = re.match(r"RTSP/1\.\d\s+(\d+)", text)
    return (int(m.group(1)) if m else 0), text


def check_rtsp_auth(host: str, port: int, path: str, username: str, password: str,
                    timeout: float = 5.0) -> AuthResult:
    path = path if path.startswith("/") else f"/{path}"
    url = f"rtsp://{host}:{port}{path}"
    try:
        status, text = _request(host, port, url, 1, None, timeout)
        if status == 200:
            return AuthResult.NO_AUTH_REQUIRED
        if status != 401:
            return AuthResult.ERROR

        if not username:
            return AuthResult.BAD_CREDENTIALS

        # pick the challenge the server offers (prefer Digest)
        digest = re.search(r"WWW-Authenticate:\s*Digest\s+(.*)", text, re.I)
        basic = re.search(r"WWW-Authenticate:\s*Basic", text, re.I)
        if digest:
            params = dict(re.findall(r'(\w+)="?([^",\r\n]*)"?', digest.group(1)))
            header = _digest_header(username, password, "DESCRIBE", url, params)
        elif basic:
            header = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        else:
            return AuthResult.ERROR

        status, _ = _request(host, port, url, 2, header, timeout)
        if status == 200:
            return AuthResult.OK
        if status in (401, 403):
            return AuthResult.BAD_CREDENTIALS
        return AuthResult.ERROR
    except OSError:
        return AuthResult.UNREACHABLE
