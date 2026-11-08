import ctypes
import json
import os
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# HEADROOM_DATA lets a test copy run beside a real install without sharing state
DATA_DIR = os.environ.get("HEADROOM_DATA") or os.path.join(os.environ.get("APPDATA", APP_DIR), "Headroom")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "headroom.log")
SCAN_FILE = os.path.join(DATA_DIR, "scan.json")
PROFILE_FILE = os.path.join(DATA_DIR, "profile.json")       # the active profile for THIS PC
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
BUILTIN_DIR = os.path.join(APP_DIR, "profiles", "builtin")    # researched profiles shipped with the app
FAMILIES_FILE = os.path.join(APP_DIR, "profiles", "families.json")
# Tools (CoreCycler, LibreHardwareMonitor, PresentMon) are downloaded on demand, never shipped.
# They run elevated, so they live inside the install folder (admin-only ACL set by the
# installer), never in a user-writable folder such as %LOCALAPPDATA%.
TOOLS_DIR = os.environ.get("HEADROOM_TOOLS") or os.path.join(APP_DIR, "tools")
LEGACY_TOOLS_DIR = os.path.join(os.environ.get("LOCALAPPDATA", DATA_DIR), "Headroom", "tools")   # <= 1.0.0, never run from here

# Pinned downloads: exact version + SHA-256 (GitHub release asset digests). Bump deliberately.
PINNED = {
    "lhm": {"version": "v0.9.6", "name": "LibreHardwareMonitor.zip", "size": 6632626,
            "url": "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip",
            "sha256": "086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001"},
    "corecycler": {"version": "v0.11.0.3", "name": "CoreCycler-v0.11.0.3.7z", "size": 45436459,
                   "url": "https://github.com/sp00n/CoreCycler/releases/download/v0.11.0.3/CoreCycler-v0.11.0.3.7z",
                   "sha256": "721ed2948b51a2fc7c60289ecb7cbf62057b22d54c4ba5dc70ac5a107fcc1a63"},
    "presentmon": {"version": "v2.6.0", "name": "PresentMon-2.6.0-x64.exe", "size": 980320,
                   "url": "https://github.com/GameTechDev/PresentMon/releases/download/v2.6.0/PresentMon-2.6.0-x64.exe",
                   "sha256": "b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af"},
}
VENDOR_DIR = os.path.join(APP_DIR, "vendor")                   # pip packages (anthropic SDK) in dev checkouts
if os.path.isdir(VENDOR_DIR) and VENDOR_DIR not in sys.path:
    sys.path.append(VENDOR_DIR)

NO_WINDOW = 0x08000000


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verified_download(key, dest_dir, log=None, opener=None):
    """Download PINNED[key] into dest_dir and check its SHA-256. Returns the file
    path. A file that is already there is re-hashed, never trusted by name."""
    import shutil
    import urllib.request
    pin = PINNED[key]
    os.makedirs(dest_dir, exist_ok=True)
    dst = os.path.join(dest_dir, pin["name"])
    if os.path.isfile(dst) and sha256_file(dst) == pin["sha256"]:
        return dst
    if log:
        log("Downloading %s %s (%d MB)" % (pin["name"], pin["version"], pin["size"] >> 20))
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(pin["url"], headers={"User-Agent": "Headroom"})
    part = dst + ".part"
    with opener(req, timeout=300) as r, open(part, "wb") as f:
        shutil.copyfileobj(r, f)
    got = sha256_file(part)
    if got != pin["sha256"]:
        os.remove(part)
        raise RuntimeError("%s failed its integrity check (SHA-256 %s, expected %s). Not installed." % (pin["name"], got, pin["sha256"]))
    os.replace(part, dst)
    return dst


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


SYS32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
_SYS_TOOLS = {"powershell.exe": os.path.join(SYS32, "WindowsPowerShell", "v1.0", "powershell.exe")}


def _resolve(exe):
    """Absolute path for Windows tools: PATH can be incomplete when started
    from a scheduled task or another app."""
    if os.path.dirname(exe):
        return exe
    if exe in _SYS_TOOLS:
        return _SYS_TOOLS[exe]
    for name in (exe, exe + ".exe"):
        p = os.path.join(SYS32, name)
        if os.path.exists(p):
            return p
    return exe


def run(cmd, timeout=30):
    """Run a command hidden, return stdout text ('' on failure)."""
    cmd = [_resolve(cmd[0])] + list(cmd[1:])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        return r.stdout
    except Exception:  # noqa: BLE001
        return ""


def ps(script, timeout=30):
    return run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout)


def process_running(*names):
    out = run(["tasklist", "/fo", "csv", "/nh"]).lower()
    return [n for n in names if ('"%s"' % n.lower()) in out]


class State:
    """Small JSON store. Written on every change, so a crash or freeze in the
    middle of a test leaves an accurate record of what was being tested.
    Several processes share the file (backend, logon apply, crash handler,
    monitor), so it is re-read whenever it changed on disk: a stale in-memory
    copy must never overwrite what another process wrote."""

    def __init__(self, path=STATE_FILE):
        self.path = path
        self.lock = threading.RLock()
        self.d = {}
        self._sig = None
        self._reload(force=True)

    def _stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _reload(self, force=False):
        sig = self._stat()
        if not force and sig == self._sig:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.d = d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            if force:
                self.d = {}
        self._sig = sig

    def get(self, *keys, default=None):
        with self.lock:
            self._reload()
            d = self.d
            for k in keys:
                if not isinstance(d, dict) or k not in d:
                    return default
                d = d[k]
            return d

    def set(self, *keys, value):
        with self.lock:
            self._reload()
            d = self.d
            for k in keys[:-1]:
                if not isinstance(d.get(k), dict):
                    d[k] = {}
                d = d[k]
            d[keys[-1]] = value
            tmp = "%s.%d.tmp" % (self.path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.d, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self._sig = self._stat()


class Log:
    def __init__(self, path=LOG_FILE):
        self.path = path
        self.listeners = []

    def __call__(self, msg, level="info"):
        line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        for fn in list(self.listeners):
            try:
                fn(line, level)
            except Exception:  # noqa: BLE001
                pass


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def write_json(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_profiles():
    """The active profile for this PC (written by setup). Empty until setup has run."""
    from .profiles import clamp_all
    return clamp_all(read_json(PROFILE_FILE, {}) or {})


def match_profile(section, name, profiles=None):
    p = (profiles or load_profiles()).get(section, {})
    name = (name or "").lower()
    for k, v in p.items():
        if k.lower() in name or name in k.lower():
            return k, v
    return None, None


def tdr_events_since(t0):
    """NVIDIA driver resets / Xid errors logged in the System event log since
    t0 (epoch seconds). Any of these during a test = unstable."""
    ms = max(1000, int((time.time() - t0 + 5) * 1000))
    q = ("*[System[(Provider[@Name='nvlddmkm'] or (Provider[@Name='Display'] and EventID=4101)) "
         "and TimeCreated[timediff(@SystemTime) <= %d]]]" % ms)
    out = run(["wevtutil", "qe", "System", "/q:" + q, "/f:text", "/c:10", "/rd:true"], 15)
    events = []
    for block in out.split("Event[")[1:]:
        eid = desc = ""
        for ln in block.splitlines():
            ln = ln.strip()
            if ln.startswith("Event ID:"):
                eid = ln.split(":", 1)[1].strip()
            elif ln.startswith("Description:"):
                desc = ln.split(":", 1)[1].strip()
        events.append("event %s %s" % (eid, desc[:120]))
    return events


def whea_events_since(t0):
    ms = max(1000, int((time.time() - t0 + 5) * 1000))
    q = ("*[System[Provider[@Name='Microsoft-Windows-WHEA-Logger'] "
         "and TimeCreated[timediff(@SystemTime) <= %d]]]" % ms)
    out = run(["wevtutil", "qe", "System", "/q:" + q, "/f:text", "/c:20", "/rd:true"], 15)
    return out.count("Event[")
