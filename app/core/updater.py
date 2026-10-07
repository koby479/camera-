"""Self-update of the single-file EXE (no installer). No Qt in here, so it can be tested.

How it works
- Every push to GitHub builds a release called "build-N" with UniversalCamViewer.exe and a .sha256 file (see
  .github/workflows). The program asks which build is the latest by reading the redirect of .../releases/latest
  (no API key and no API rate limit) and compares N with its own build number (app/_build.py, stamped by the build).
- The new EXE is downloaded next to the running one, checked (SHA-256 when the release has one, size, "MZ"), then
  swapped in: Windows lets a running EXE be RENAMED, so the old one becomes UniversalCamViewer.exe.old (kept: that is
  the "previous version" for a rollback), the new one takes its name, and a delayed command starts it after this
  process has ended.
- Second route that needs no internet (a filtered network): an EXE copied by hand - menu "update from file", or a
  file named UniversalCamViewer.new.exe put next to the program, which is offered at the next start."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_URL = "https://github.com/koby479/camera-"
API_URL = "https://api.github.com/repos/koby479/camera-"
ASSET = "UniversalCamViewer.exe"
DROPIN_NAME = "UniversalCamViewer.new.exe"
MIN_EXE_BYTES = 10 * 1024 * 1024          # a real build is >100 MB; this only rejects obviously wrong files
TIMEOUT = 15
USER_AGENT = "UniversalCamViewer-updater"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


class UpdateError(Exception):
    """A readable (Hebrew) reason why an update step failed."""


# ---- where am I -----------------------------------------------------------------------
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_path() -> Path:
    return Path(sys.executable)


def can_write(directory: Path) -> bool:
    try:
        probe = Path(directory) / f".write-test-{os.getpid()}"
        probe.write_bytes(b"x")
        probe.unlink()
        return True
    except OSError:
        return False


def old_path(exe: Path | None = None) -> Path:
    exe = Path(exe or exe_path())
    return exe.with_name(exe.name + ".old")


def dropin_path(exe: Path | None = None) -> Path:
    return Path(exe or exe_path()).with_name(DROPIN_NAME)


# ---- asking GitHub ------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def parse_build_tag(location: str) -> int | None:
    m = re.search(r"/tag/build-(\d+)", location or "")
    return int(m.group(1)) if m else None


def latest_build(base: str | None = None, timeout: float = TIMEOUT) -> int:
    """The number of the newest published build (reads the redirect of releases/latest)."""
    url = (base or REPO_URL) + "/releases/latest"
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        resp = opener.open(req, timeout=timeout)
        location = resp.headers.get("Location", "") or resp.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise UpdateError(f"האתר החזיר שגיאה {exc.code}") from exc
        location = exc.headers.get("Location", "")
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError("אין חיבור לאתר העדכונים (ייתכן שהאינטרנט מסונן)") from exc
    build = parse_build_tag(location)
    if build is None:
        raise UpdateError("לא נמצאה גרסה באתר")
    return build


def release_notes(build: int, base: str | None = None, timeout: float = 8) -> str:
    """What is new in a build (the commit message the release was made from). Best effort: '' on any problem."""
    import json
    api = API_URL if base is None else base + "/api"
    try:
        req = urllib.request.Request(f"{api}/releases/tags/build-{build}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return str(json.loads(resp.read().decode("utf-8")).get("body") or "").strip()[:1500]
    except (OSError, ValueError):
        return ""


def _open(url: str, timeout: float = TIMEOUT):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


def expected_sha256(build: int, base: str | None = None) -> str | None:
    try:
        with _open(f"{base or REPO_URL}/releases/download/build-{build}/{ASSET}.sha256", 10) as resp:
            m = re.search(r"\b([0-9a-fA-F]{64})\b", resp.read(4096).decode("ascii", "ignore"))
            return m.group(1).lower() if m else None
    except (OSError, ValueError):
        return None


def verify_exe(path: Path, min_bytes: int = MIN_EXE_BYTES) -> None:
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(2)
    except OSError as exc:
        raise UpdateError(f"לא ניתן לקרוא את הקובץ ({exc})") from exc
    if head != b"MZ":
        raise UpdateError("הקובץ אינו תוכנת Windows (EXE)")
    if size < min_bytes:
        raise UpdateError(f"הקובץ קטן מדי ({size // 1024} KB), כנראה הורדה חלקית")


def download(build: int, dest: Path, progress=None, cancelled=None, base: str | None = None,
             min_bytes: int = MIN_EXE_BYTES) -> str:
    """Download build N to dest and verify it. progress(done, total) is called while it runs.
    Returns the SHA-256. Removes dest and raises UpdateError when anything is wrong."""
    base = base or REPO_URL
    dest = Path(dest)
    expected = expected_sha256(build, base)
    sha = hashlib.sha256()
    done = 0
    try:
        try:
            resp = _open(f"{base}/releases/download/build-{build}/{ASSET}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpdateError("הגרסה עדיין בבנייה. נסה שוב בעוד דקה") from exc
            raise UpdateError(f"ההורדה נכשלה (שגיאה {exc.code})") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise UpdateError("אין חיבור לאתר העדכונים (ייתכן שהאינטרנט מסונן)") from exc
        with resp, open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            while True:
                if cancelled and cancelled():
                    raise UpdateError("ההורדה בוטלה")
                try:
                    chunk = resp.read(256 * 1024)
                except OSError as exc:
                    raise UpdateError("החיבור נקטע באמצע ההורדה") from exc
                if not chunk:
                    break
                out.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if total and done != total:
            raise UpdateError("ההורדה לא הושלמה")
        digest = sha.hexdigest()
        if expected and digest != expected:
            raise UpdateError("בדיקת התקינות (SHA-256) נכשלה: הקובץ שהורד שונה מהמקור")
        verify_exe(dest, min_bytes)
        return digest
    except BaseException:
        try:
            dest.unlink()
        except OSError:
            pass
        raise


# ---- swapping the files -------------------------------------------------------------------
def apply(new_path: Path, exe: Path | None = None) -> None:
    """Make new_path the program: the running EXE is renamed to *.old (the previous version), then new_path takes
    its name. new_path must be on the same drive (normally the same folder). Undoes itself if the second step fails."""
    exe = Path(exe or exe_path())
    new_path = Path(new_path)
    old = old_path(exe)
    if old.exists():
        try:
            old.unlink()
        except OSError:                               # still locked (an earlier copy running): use another name
            old = exe.with_name(f"{exe.name}.old{int(time.time())}")
    try:
        os.replace(exe, old)
    except OSError as exc:
        raise UpdateError(f"לא ניתן להחליף את הקובץ ({exc}). הרץ כמנהל או העבר את התוכנה לתיקייה רגילה") from exc
    try:
        os.replace(new_path, exe)
    except OSError as exc:
        try:
            os.replace(old, exe)
        except OSError:
            pass
        raise UpdateError(f"ההחלפה נכשלה ({exc})") from exc


def rollback_available(exe: Path | None = None) -> bool:
    old = old_path(exe)
    try:
        return old.is_file() and old.stat().st_size >= MIN_EXE_BYTES
    except OSError:
        return False


def rollback(exe: Path | None = None) -> None:
    """Go back to the previous version (the current one becomes the new 'previous', so it can be undone)."""
    exe = Path(exe or exe_path())
    tmp = exe.with_name(exe.name + ".rollback.tmp")
    try:
        shutil.copy2(old_path(exe), tmp)
    except OSError as exc:
        raise UpdateError(f"הגרסה הקודמת לא זמינה ({exc})") from exc
    try:
        apply(tmp, exe)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def cleanup_leftovers(exe: Path | None = None) -> None:
    """At start: remove half-finished downloads and spare '.oldNNN' copies. The plain .old stays (rollback)."""
    exe = Path(exe or exe_path())
    try:
        for p in exe.parent.iterdir():
            n = p.name
            if n.startswith(exe.name) and (n.endswith(".update.tmp") or n.endswith(".rollback.tmp")
                                           or re.search(r"\.old\d+$", n)):
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _clean_env() -> dict:
    """A child started from a one-file PyInstaller program must not inherit its temp-folder variables."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("_MEI") and not k.startswith("_PYI")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def relaunch_args(exe: Path, delay: int = 3) -> list[str]:
    """Wait a few seconds (this process is closing), then start the program again."""
    return ["cmd.exe", "/c", "ping", "-n", str(delay + 1), "127.0.0.1", ">nul", "&", "start", "", str(exe), "--restarted"]


def relaunch_later(exe: Path | None = None, delay: int = 3) -> None:
    exe = Path(exe or exe_path())
    if os.name == "nt":
        subprocess.Popen(relaunch_args(exe, delay), env=_clean_env(), close_fds=True,
                         creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS)
    else:
        subprocess.Popen([str(exe)], env=_clean_env(), close_fds=True)


# ---- an EXE that was copied by hand: which version is it? -------------------------------------------
def write_build_info(target: str) -> None:
    """Run as: UniversalCamViewer.exe --build-info-file FILE  (writes 'version|build' and exits; no window)."""
    from app import BUILD, VERSION
    Path(target).write_text(f"{VERSION}|{BUILD}", encoding="utf-8")


def candidate_info(path: Path, timeout: float = 45.0) -> tuple[str, int] | None:
    """Ask another copy of the program for its version. None if it cannot say (an old build that does not know the
    flag just opens its window, so it is closed again after the timeout)."""
    out = Path(tempfile.gettempdir()) / f"ucv-info-{os.getpid()}-{time.time_ns()}.txt"
    kwargs = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = subprocess.Popen([str(path), "--build-info-file", str(out)], env=_clean_env(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except OSError:
        return None
    result = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                text = out.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            m = re.fullmatch(r"([\w.\-]+)\|(\d+)", text)
            if m:
                result = (m.group(1), int(m.group(2)))
                break
            if proc.poll() is not None and not out.exists():
                break
            time.sleep(0.3)
    finally:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
            else:
                proc.kill()
        try:
            out.unlink()
        except OSError:
            pass
    return result
