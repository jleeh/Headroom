"""First-run setup: hardware scan -> profile (built-in / family, or Claude research)
-> sensor app check -> cooling headers. Re-runnable from the Setup tab."""
import os
import shutil
import subprocess
import threading
import time
import zipfile

from . import profiles, research, scan as hwscan
from .common import LEGACY_TOOLS_DIR, PINNED, PROFILE_FILE, SCAN_FILE, TOOLS_DIR, read_json, run, verified_download, write_json
from .sensors import Lhm

LHM_TASK = "Headroom Sensors"


def task_command(name):
    """The program a scheduled task runs ('' if the task doesn't exist)."""
    import re
    out = run(["schtasks", "/query", "/tn", name, "/xml"])
    m = re.search(r"<Command>(.*?)</Command>", out or "", re.S)
    return (m.group(1).strip().strip('"') if m else "")


def remove_legacy_tasks(log):
    """Headroom 1.0.0 ran LibreHardwareMonitor from %LOCALAPPDATA% (user-writable)
    through an elevated logon task: any program could swap the exe and get admin
    rights. Remove such a task; Setup reinstalls the sensors into the tools folder."""
    cmd = task_command(LHM_TASK)
    if cmd and os.path.normcase(os.path.abspath(cmd)).startswith(os.path.normcase(os.path.abspath(LEGACY_TOOLS_DIR))):
        run(["schtasks", "/delete", "/f", "/tn", LHM_TASK])
        log("Removed the old 'Headroom Sensors' logon task (it ran from a user-writable folder). "
            "Press 'Install sensors' in Setup to set it up again safely.", "warn")
        return True
    return False

LHM_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <appSettings>
    <add key="startMinMenuItem" value="true" />
    <add key="minTrayMenuItem" value="true" />
    <add key="minCloseMenuItem" value="true" />
    <add key="runWebServerMenuItem" value="true" />
    <add key="listenerIp" value="127.0.0.1" />
    <add key="listenerPort" value="8085" />
  </appSettings>
</configuration>
"""


def _parts(sc):
    return [(sc.get("cpu") or {}).get("name"), (sc.get("gpu") or {}).get("name"), (sc.get("board") or {}).get("model")]


class Setup:
    def __init__(self, log, on_profile_changed):
        self.log = log
        self.on_profile_changed = on_profile_changed
        self.busy = None
        self.progress = ""
        self.error = None
        self.last_research = None
        self.cancel_ev = threading.Event()

    def cancel(self):
        self.cancel_ev.set()

    # ------------------------------------------------------------- status
    def status(self):
        sc = read_json(SCAN_FILE)
        prof = read_json(PROFILE_FILE)
        return {"needed": not prof, "scan": sc, "profile_sources": (prof or {}).get("_sources"),
                "research": (prof or {}).get("_research"), "has_key": research.has_key(),
                "busy": self.busy, "progress": self.progress, "error": self.error,
                "lhm": Lhm().poll(), "lhm_installed": bool(self._lhm_exe()),
                "cooling": self._cooling(prof)}

    @staticmethod
    def _cooling(prof):
        c = next(iter(((prof or {}).get("cooler") or {}).values()), {})
        return {"pump_sensor": c.get("pump_sensor"), "radiator_sensor": c.get("radiator_sensor"),
                "pump_min_rpm": (c.get("preflight") or {}).get("pump_min_rpm")}

    def _bg(self, label, fn):
        if self.busy:
            raise RuntimeError("Setup is busy: " + self.busy)
        self.busy, self.error, self.progress = label, None, label

        def wrap():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                self.log("Setup: %s" % e, "error")
            finally:
                self.busy = None
        threading.Thread(target=wrap, daemon=True).start()

    # ------------------------------------------------------------- scan + profile
    def run_scan(self, then_build=True):
        def go():
            self.progress = "Scanning hardware"
            sc = hwscan.scan(self.log)
            if then_build:
                self._build(sc)
        self._bg("Scanning hardware", go)

    def _build(self, sc, res=None):
        prof_old = read_json(PROFILE_FILE) or {}
        cool = self._cooling(prof_old)
        if res is None:
            # keep earlier research if it was for the same parts (e.g. a re-scan after installing sensors)
            old = read_json(os.path.join(os.path.dirname(SCAN_FILE), "research.json"))
            if old and old.get("parts") == _parts(sc):
                res = old
        p = profiles.build(sc, research=res, cooling=cool)
        profiles.save(p)
        self.log("Profile built: GPU from %s, CPU from %s, board from %s data" % (
            p["_sources"].get("gpu"), p["_sources"].get("cpu"), p["_sources"].get("board")))
        self.on_profile_changed()

    def rebuild(self):
        sc = read_json(SCAN_FILE)
        if not sc:
            return self.run_scan()
        self._bg("Building profile", lambda: self._build(sc))

    def run_research(self):
        sc = read_json(SCAN_FILE)
        if not sc:
            raise RuntimeError("Scan the hardware first")
        backend = research.get_settings().get("research_backend")
        if backend == "api":
            if not research.has_key():
                raise RuntimeError("Add an Anthropic API key first")
            research.check_budget()
        self.cancel_ev.clear()

        def go():
            def prog(m):
                self.progress = m
            r = research.run_research(sc, prog, cancel=self.cancel_ev)
            r["parts"] = _parts(sc)
            self.last_research = r
            write_json(os.path.join(os.path.dirname(SCAN_FILE), "research.json"), r)
            u = r.get("usage") or {}
            self.log("Research done (%s): %d sources, %d web searches, %d pages, %d input (+%d cached) / %d output tokens, $%.2f" % (
                r.get("backend", "api"), len(r["sources"]), u.get("web_searches", 0), u.get("web_fetches", 0),
                u.get("input_tokens", 0), u.get("cache_read_tokens", 0) + u.get("cache_write_tokens", 0),
                u.get("output_tokens", 0), u.get("cost_usd", 0) or 0))
            rej = (r.get("board") or {}).get("rejected") or []
            if rej:
                self.log("Research: %d BIOS suggestion(s) were left off the sheet because they are outside what Headroom "
                         "considers safe (see Profiles)" % len(rej), "warn")
            self._build(sc, r)
        self._bg("Researching with Claude", go)

    def set_key(self, key):
        research.save_key(key)
        self.log("Anthropic API key %s" % ("saved (encrypted for this Windows user)" if key else "removed"))

    def set_cooling(self, body):
        prof = read_json(PROFILE_FILE)
        if not prof:
            raise RuntimeError("Run setup first")
        name = next(iter(prof.get("cooler") or {}), "CPU cooler")
        c = (prof.get("cooler") or {}).get(name) or {}
        c["pump_sensor"] = body.get("pump_sensor") or None
        c["radiator_sensor"] = body.get("radiator_sensor") or None
        c.setdefault("preflight", {})["pump_min_rpm"] = int(body.get("pump_min_rpm") or 1000)
        prof["cooler"] = {name: c}
        profiles.save(prof)
        self.log("Cooling check: pump %s, CPU fans %s" % (c["pump_sensor"] or "not watched", c["radiator_sensor"] or "not watched"))
        self.on_profile_changed()

    # ------------------------------------------------------------- LibreHardwareMonitor
    @staticmethod
    def _lhm_exe():
        p = os.path.join(TOOLS_DIR, "LibreHardwareMonitor", "LibreHardwareMonitor.exe")
        return p if os.path.isfile(p) else None

    def install_lhm(self):
        def go():
            if Lhm().poll():
                self.log("LibreHardwareMonitor is already running with its web server: nothing to do")
                return
            exe = self._lhm_exe()
            if not exe:
                self.progress = "Downloading LibreHardwareMonitor"
                zpath = verified_download("lhm", os.path.join(TOOLS_DIR, "_dl"), self.log)
                dest = os.path.join(TOOLS_DIR, "LibreHardwareMonitor")
                shutil.rmtree(dest, ignore_errors=True)
                with zipfile.ZipFile(zpath) as z:
                    for m in z.infolist():   # no absolute paths or '..' (zip slip)
                        target = os.path.realpath(os.path.join(dest, m.filename))
                        if not target.startswith(os.path.realpath(dest) + os.sep):
                            raise RuntimeError("Unsafe path in the LibreHardwareMonitor download: %s" % m.filename)
                    z.extractall(dest)
                # some releases nest everything in one folder
                hits = [os.path.join(dp, "LibreHardwareMonitor.exe") for dp, _, fs in os.walk(dest) if "LibreHardwareMonitor.exe" in fs]
                if not hits:
                    raise RuntimeError("LibreHardwareMonitor.exe not found in the download")
                if os.path.dirname(hits[0]) != dest:
                    inner = os.path.dirname(hits[0])
                    for n in os.listdir(inner):
                        shutil.move(os.path.join(inner, n), os.path.join(dest, n))
                exe = os.path.join(dest, "LibreHardwareMonitor.exe")
                self.log("LibreHardwareMonitor %s installed to %s" % (PINNED["lhm"]["version"], dest))
            cfg = os.path.join(os.path.dirname(exe), "LibreHardwareMonitor.config")
            if not os.path.isfile(cfg):
                with open(cfg, "w", encoding="utf-8") as f:
                    f.write(LHM_CONFIG)
            run(["schtasks", "/create", "/f", "/rl", "highest", "/sc", "onlogon", "/tn", LHM_TASK, "/tr", '"%s"' % exe])
            subprocess.Popen([exe], cwd=os.path.dirname(exe), creationflags=0x00000008 | 0x00000200, close_fds=True)
            self.progress = "Waiting for the sensor web server"
            for _ in range(40):
                time.sleep(1)
                if Lhm().poll():
                    self.log("Sensors online (LibreHardwareMonitor, starts at logon)")
                    sc = hwscan.scan(self.log)
                    self._build(sc)
                    return
            raise RuntimeError("LibreHardwareMonitor started but its web server didn't answer on port 8085. "
                               "If it asked to install PawnIO, accept and press this button again.")
        self._bg("Installing sensors", go)
