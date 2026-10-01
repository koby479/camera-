"""
Standalone NVR connectivity diagnostic (Python standard library only).

Usage (from the project root):
    python tools/diagnose_nvr.py 83.229.22.45 --user admin --password 1234
    python tools/diagnose_nvr.py 83.229.22.45 --user admin --password 1234 --ports 80,554,8000

Prints which ports answer, whether each speaks ONVIF (and whether it demands
a login), which addresses the NVR advertises (a private 192.168.x.x address
there is the classic reason ONVIF "times out" from outside the LAN), and
whether RTSP accepts the username/password.
"""
import argparse
import base64
import datetime
import hashlib
import http.client
import ipaddress
import os
import re
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.core.rtsp_auth import check_rtsp_auth  # noqa: E402

DEFAULT_PORTS = [80, 81, 443, 554, 8000, 8080, 8081, 8443, 8554, 8899, 2020, 5000, 6036, 9000, 34567, 37777, 37778]
RTSP_PATHS = ["/Streaming/Channels/101", "/cam/realmonitor?channel=1&subtype=0", "/live/ch0", "/onvif1", "/"]
NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
      'xmlns:d="http://www.onvif.org/ver10/device/wsdl" '
      'xmlns:c="http://www.onvif.org/ver10/schema"')


def tcp_open(host, port, timeout=4.0):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def soap(host, port, body, user=None, pw=None, timeout=8):
    header = ""
    if user is not None:
        nonce = os.urandom(16)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + pw.encode()).digest()).decode()
        header = ('<s:Header><Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
                  f'<UsernameToken><Username>{user}</Username>'
                  f'<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
                  f'<Nonce>{base64.b64encode(nonce).decode()}</Nonce>'
                  f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
                  '</UsernameToken></Security></s:Header>')
    xml = f'<?xml version="1.0"?><s:Envelope {NS}>{header}<s:Body>{body}</s:Body></s:Envelope>'
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", "/onvif/device_service", xml.encode(),
                     {"Content-Type": "application/soap+xml; charset=utf-8"})
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    finally:
        conn.close()


def is_private(h):
    try:
        return ipaddress.ip_address(h).is_private
    except ValueError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--ports", default="")
    ap.add_argument("--stream-test", action="store_true",
                    help="also try to pull live video from channel 1 over the XM protocol and print raw details")
    a = ap.parse_args()
    ports = [int(p) for p in a.ports.split(",") if p.strip()] or DEFAULT_PORTS

    print(f"== 1. סריקת פורטים על {a.host} ==")
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda p: tcp_open(a.host, p), ports))
    open_ports = [p for p, ok in zip(ports, results) if ok]
    print("פתוחים:", open_ports or "אין אף אחד -> חומת אש / פורט-פורוורד לא מוגדר / IP לא נכון")

    print("\n== 2. ONVIF על כל פורט פתוח ==")
    for p in open_ports:
        try:
            st, txt = soap(a.host, p, "<d:GetSystemDateAndTime/>")
            ok = "GetSystemDateAndTimeResponse" in txt
            print(f"פורט {p}: HTTP {st} - {'ONVIF עונה (ללא אימות)' if ok else 'עונה אבל לא נראה כמו ONVIF'}")
            if ok and a.user:
                st2, txt2 = soap(a.host, p, "<d:GetCapabilities><d:Category>All</d:Category></d:GetCapabilities>", a.user, a.password)
                addrs = sorted(set(re.findall(r"<[^>]*XAddr>([^<]+)<", txt2)))
                if "Fault" in txt2 and not addrs:
                    print(f"   עם משתמש/סיסמה: נדחה ({'Sender not authorized' if 'NotAuthorized' in txt2 else 'Fault'})")
                for x in addrs:
                    host_in = re.match(r"https?://([^/:]+)", x)
                    warn = "  <-- כתובת פנימית! מבחוץ זה לא נגיש" if host_in and is_private(host_in.group(1)) else ""
                    print("   מפרסם:", x, warn)
        except OSError as exc:
            print(f"פורט {p}: לא ענה ל-ONVIF ({type(exc).__name__}: {exc})")
        except Exception as exc:  # noqa: BLE001
            print(f"פורט {p}: שגיאה {exc}")

    print("\n== 3. RTSP (בדיקת שם משתמש/סיסמה) ==")
    for p in open_ports:
        for path in RTSP_PATHS:
            res = check_rtsp_auth(a.host, p, path, a.user, a.password, timeout=5)
            if res.value in ("ok", "open", "bad"):
                label = {"ok": "התחברות הצליחה", "open": "אין דרישת סיסמה", "bad": "שם משתמש/סיסמה שגויים"}[res.value]
                print(f"פורט {p} נתיב {path}: {label}")
                break
        else:
            continue
    print("\n== 4. פרוטוקול XM / Provision CMS (DVRIP) ==")
    from app.core import dvrip
    for p in open_ports:
        res, info = dvrip.check_login(a.host, p, a.user, a.password, timeout=6)
        if res.value in ("ok", "bad"):
            print(f"פורט {p}: {'התחברות הצליחה' if res.value == 'ok' else 'המכשיר XM אבל שם משתמש/סיסמה שגויים'} -> {info}")
            if res.value == "ok" and a.stream_test:
                stream_test(a.host, p, a.user, a.password)
        elif res.value == "error":
            print(f"פורט {p}: ענה אבל לא כמו XM ({info})")
    print("\nסיום. שלח לי את כל הפלט הזה.")


def stream_test(host, port, user, pw):
    from app.core import dvrip
    c = dvrip.DVRIPClient(host, port, user, pw, timeout=6)
    try:
        c.connect()
        c.login()
        c.start_monitor(0, "Main", first_data_timeout=8)
        parser, seen, first = dvrip.XMFrameParser(), 0, []
        audio_info: dict = {}
        import time
        end = time.monotonic() + 4
        try:
            for chunk in c.read_video_payloads(idle_timeout=1):
                if chunk and len(first) < 3:
                    first.append(chunk[:48].hex(" "))
                if chunk:
                    seen += len(parser.feed(chunk))
                    for media, rate, body in parser.audio:
                        audio_info.setdefault((media, rate), [0, 0])
                        audio_info[(media, rate)][0] += 1
                        audio_info[(media, rate)][1] += len(body)
                    parser.audio = []
                if time.monotonic() > end:
                    break
        except (OSError, dvrip.DVRIPError) as exc:
            print("   הזרם נסגר:", exc)
        print("   וידאו: התקבלו", seen, "פריימים תקינים ב-4 שניות")
        if audio_info:
            for (media, rate), (n, size) in audio_info.items():
                print(f"   שמע: {n} פריימים ({size} בייט), סוג 0x{media:02X}, {rate} Hz")
        else:
            print("   שמע: לא התקבלו פריימי שמע (ייתכן שהשמע כבוי בהגדרות הקידוד של ערוץ 1 ב-NVR)")
        for i, h in enumerate(first):
            print(f"   בייטים ראשונים #{i + 1}: {h}")
    except Exception as exc:  # noqa: BLE001
        print("   בדיקת וידאו נכשלה:", type(exc).__name__, exc)
    finally:
        c.close()


if __name__ == "__main__":
    main()
