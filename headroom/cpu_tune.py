"""CPU auto-tune for Ryzen (PBO + per-core Curve Optimizer), driven through
CoreCycler (github.com/sp00n/CoreCycler, uses the PawnIO driver already on
this PC).

Why not apply everything from Windows? On Ryzen 9000, Curve Optimizer can only
be changed at runtime temporarily (ryzen-smu-cli) and the Max Boost Override
can't be changed at runtime at all. So the flow is:

  1  baseline   benchmark at your current settings
  2  BIOS #1    PBO Advanced, +200 MHz boost, 85 C platform thermal limit, CO 0
  3  search     CoreCycler tests each core alone with y-cruncher, starting at
                CO -N; every error raises that core by +1. It resumes on its
                own after a crash or freeze (press reset if frozen).
  4  BIOS #2    enter the found values +3 margin (Headroom shows a sheet)
  5  verify     all-core CoreCycler pass at the BIOS values + benchmark ->
                measured and estimated uplift

Safety: watchdog kills the stress test at 90 C Tdie, or if the AIO pump or
radiator fans stop. The BIOS thermal limit (85 C) is a hardware backstop.
CO only LOWERS voltage; Headroom never raises PPT/TDC/EDC, Vcore or VSOC.
"""
import glob
import ntpath
import os
import re
import shutil
import threading
import time

from . import cpu_bench, uplift
from .common import PINNED, TOOLS_DIR, is_admin, ps, run, verified_download, whea_events_since

TASK = r"\CoreCycler\CoreCycler AutoMode Startup Task"
RESUME_TASK = "Headroom Resume"
MARGIN = 3


def find_corecycler():
    hits = sorted(glob.glob(os.path.join(TOOLS_DIR, "CoreCycler*", "script-corecycler.ps1")))
    return os.path.dirname(hits[-1]) if hits else None


def install_corecycler(log):
    """Download the pinned release (SHA-256 checked) and unpack it with Windows'
    built-in tar (bsdtar reads 7z and refuses '..' paths). It runs elevated, so it
    lives in the admin-only tools folder."""
    dst = verified_download("corecycler", os.path.join(TOOLS_DIR, "_dl"), log)
    run(["tar", "-xf", dst, "-C", TOOLS_DIR], timeout=300)
    path = find_corecycler()
    if not path:
        raise RuntimeError("Unpacking failed: open %s in Explorer, extract it into %s and retry." % (dst, TOOLS_DIR))
    log("CoreCycler %s ready in %s" % (PINNED["corecycler"]["version"], path))
    return path


def write_config(cc, mode, start_value=-25, cores=8):
    """mode 'search' = auto-adjusting single-core hunt; 'verify' = no
    adjusting, all cores, 1 and 2 threads, at whatever the BIOS has set."""
    if mode == "search":
        body = """# Written by Headroom - per-core Curve Optimizer search
[General]
stressTestProgram = YCRUNCHER
runtimePerCore = auto
coreTestOrder = Default
numberOfThreads = 1
maxIterations = 3
lookForWheaErrors = 1
treatWheaWarningAsError = 1
beepOnError = 0
flashOnError = 0

[yCruncher]
mode = auto
tests = SFTv4, FFTv4, N63
testDuration = 20

[AutomaticTestMode]
enableAutomaticAdjustment = 1
startValues = {sv}
maxValue = 0
incrementBy = 1
setVoltageOnlyForTestedCore = 1
repeatCoreOnError = 1
enableResumeAfterUnexpectedExit = 1
waitBeforeAutomaticResume = 120
createSystemRestorePoint = 1
askForSystemRestorePointCreation = 0

[Update]
enableUpdateCheck = 0
""".format(sv=start_value)
    else:
        body = """# Written by Headroom - verification of the BIOS Curve Optimizer values
[General]
stressTestProgram = YCRUNCHER
runtimePerCore = 2m
coreTestOrder = Default
numberOfThreads = 2
maxIterations = 1
skipCoreOnError = 1
lookForWheaErrors = 1
treatWheaWarningAsError = 1

[yCruncher]
mode = auto
tests = SFTv4, FFTv4, N63, VT3
testDuration = 30

[AutomaticTestMode]
enableAutomaticAdjustment = 0

[Update]
enableUpdateCheck = 0
"""
    cfg = os.path.join(cc, "config.ini")
    if os.path.exists(cfg) and not os.path.exists(cfg + ".orig"):
        shutil.copy2(cfg, cfg + ".orig")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(body)


def launch_corecycler(cc):
    bat = os.path.join(cc, "Run CoreCycler.bat")
    run(["cmd.exe", "/c", "start", "", "/d", cc, bat], timeout=10)


def corecycler_running():
    out = ps("Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
             "Where-Object CommandLine -match 'script-corecycler' | Measure-Object | % Count")
    return out.strip() not in ("", "0")


def kill_script(cc):
    """PowerShell that stops CoreCycler and the stress programs it started.
    Matched by exact folder prefix (StartsWith, not -like: '[' in a path is a
    wildcard there). An empty or relative folder must never become 'kill everything'."""
    parts = ["$_.CommandLine -match 'script-corecycler'"]
    if cc and ntpath.isabs(cc) and len(cc.rstrip("\\/")) > 3:
        prefix = cc.rstrip("\\/") + "\\"
        parts.insert(0, "($_.ExecutablePath -and $_.ExecutablePath.StartsWith('%s', "
                        "[StringComparison]::OrdinalIgnoreCase))" % prefix.replace("'", "''"))
    return ("Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and (%s) } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % " -or ".join(parts))


def kill_stress(cc, log, reason):
    """Emergency stop: the stress program, CoreCycler itself, and its resume task."""
    log("CPU EMERGENCY STOP: " + reason, "error")
    ps(kill_script(cc))
    run(["schtasks", "/delete", "/tn", TASK, "/f"])


_INC = re.compile(r"Increasing Curve Optimizer value for core (\d+)\D.*?from (-?\d+) to (-?\d+)")
_ERR = re.compile(r"ERROR: At Core (\d+)")
# one number, then "| number" / "number" repeats: each step must consume a separator, so no backtracking blow-up
_CUR = re.compile(r"^\s*(?:Current values|CO values)\s+(-?\d+(?:[\s|]+-?\d+)*)[\s|]*$")


def parse_results(cc, since, start_value, cores=8):
    """Final per-core CO from the CoreCycler logs written since `since`."""
    vals = [start_value] * cores
    errors = 0
    logs = sorted(f for f in glob.glob(os.path.join(cc, "logs", "CoreCycler_*.log"))
                  if os.path.getmtime(f) >= since - 60)
    summary = None
    failed = {}
    for fn in logs:
        with open(fn, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = _INC.search(ln)
                if m:
                    c = int(m.group(1))
                    if c < cores:
                        vals[c] = int(m.group(3))
                    errors += 1
                m = _ERR.search(ln)
                if m:
                    failed[int(m.group(1))] = failed.get(int(m.group(1)), 0) + 1
                m = _CUR.search(ln)
                if m:
                    nums = [int(x) for x in re.findall(r"-?\d+", m.group(1))]
                    if len(nums) == cores:
                        summary = nums
    if summary:
        vals = summary
    return {"values": vals, "adjustments": errors, "failed_cores": failed, "logs": logs}


def final_values(found):
    return [min(0, v + MARGIN) for v in found]


def bios_sheet(board_name, board, cpu, co):
    """The settings to type into the BIOS, from the active profile (researched,
    built-in or generic for the board maker)."""
    board, cpu = board or {}, cpu or {}
    boost = (cpu.get("typical") or {}).get("boost_override_mhz", 0) or 0
    rows = ["%s - enter these in the BIOS (%s):" % (board_name or "Your motherboard", board.get("bios_key") or "advanced mode"),
            "  " + (board.get("pbo_path") or "AMD Overclocking > Precision Boost Overdrive"), ""]
    for setting, value in board.get("settings_to_enter") or []:
        if "curve optimizer" in setting.lower() and "core" in setting.lower() and setting.lower() != "curve optimizer":
            continue   # per-core values are listed below
        rows.append("  %s %s %s" % (setting, "." * max(2, 34 - len(setting)), value))
    if not board.get("settings_to_enter"):
        rows += ["  Precision Boost Overdrive ........ Advanced", "  PBO Limits ....................... Auto   (do NOT raise)"]
        if boost:
            rows += ["  CPU Boost Clock Override ......... Enabled (Positive)",
                     "  Max CPU Boost Clock Override(+) .. %d" % boost]
        rows += ["  Curve Optimizer .................. Per Core"]
    if co is None:
        rows.append("  Curve Optimizer, every core ...... Negative 0  (Headroom finds these next)")
    else:
        for i, v in enumerate(co):
            rows.append("  Core %d Curve Optimizer %s Negative %d" % (i, "." * (11 if i < 10 else 10), abs(v)))
    rows += ["", "  Save and exit (usually F10), then start Headroom again: it continues where it left off."]
    if board.get("also"):
        rows += ["", "  Note: " + board["also"]]
    return "\n".join(rows)


def set_resume_at_logon(on, app_cmd):
    if on:
        run(["schtasks", "/create", "/f", "/rl", "highest", "/sc", "onlogon", "/tn", RESUME_TASK,
             "/tr", app_cmd, "/delay", "0000:30"])
    else:
        run(["schtasks", "/delete", "/f", "/tn", RESUME_TASK])


def _header_name(text):
    """'Fan #7 (~3,150 rpm)' -> 'Fan #7' (older profiles stored a description)."""
    if not text:
        return None
    m = re.match(r"\s*(Fan #\d+)", text)
    return m.group(1) if m else None


class CpuWatchdog(threading.Thread):
    """Runs whenever a CPU stress test is active. Never changes clocks/voltages,
    only stops the load."""

    def __init__(self, tele, log, profile, cooler, cc, on_trip=None):
        super().__init__(daemon=True)
        self.tele, self.log, self.cc = tele, log, cc
        sf = (profile or {}).get("safety", {})
        self.abort_t = sf.get("abort_tdie_c", 90)
        cooler = cooler or {}
        pf = cooler.get("preflight", {})
        self.pump_min = pf.get("pump_min_rpm", 1000)
        self.rad_min = pf.get("radiator_min_rpm_under_load", 300)
        # which board fan headers to watch; chosen in Setup (None = not watched)
        hd = cooler.get("headers") or {}
        self.pump_sensor = cooler.get("pump_sensor") or _header_name(hd.get("pump"))
        self.rad_sensor = cooler.get("radiator_sensor") or _header_name(hd.get("radiator_fans"))
        self.on_trip = on_trip or (lambda r: None)
        self.stop_ev = threading.Event()
        self.tripped = None
        self.max_t = 0

    def check(self, s):
        c = s.get("cpu") or {}
        t = c.get("tdie") or c.get("tctl")
        fans = s.get("fans") or {}
        if t is None:
            return "CPU temperature unreadable (is LibreHardwareMonitor running?)"
        self.max_t = max(self.max_t, t)
        if t >= self.abort_t:
            return "CPU %.1f C >= %d C" % (t, self.abort_t)
        pump = fans.get(self.pump_sensor) if self.pump_sensor else None
        if pump is not None and pump < self.pump_min:
            return "pump (%s) at %d rpm (< %d)" % (self.pump_sensor, pump, self.pump_min)
        rad = fans.get(self.rad_sensor) if self.rad_sensor else None
        if rad is not None and t > 60 and rad < self.rad_min:
            return "CPU fans (%s) at %d rpm while CPU is %.0f C" % (self.rad_sensor, rad, t)
        return None

    def run(self):
        misses = 0
        while not self.stop_ev.wait(1.0):
            r = self.check(self.tele.get())
            if r and "unreadable" in r:
                misses += 1
                if misses < 10:
                    continue
            elif r is None:
                misses = 0
                continue
            self.tripped = r
            kill_stress(self.cc, self.log, r)
            self.on_trip(r)
            return

    def stop(self):
        self.stop_ev.set()


def boost_check_mhz(cpu_p):
    """Clock a single-thread load must exceed once the boost override is on
    (None = can't check: no override on this CPU, or stock boost unknown)."""
    boost = ((cpu_p or {}).get("typical") or {}).get("boost_override_mhz") or 0
    stock = ((cpu_p or {}).get("stock") or {}).get("boost_mhz")
    if not boost or not stock:
        return None
    return stock + boost * 0.5


class CpuFlow:
    """State machine around the steps above; the GUI calls step() / action()."""

    PHASES = ["baseline", "bios1", "search", "bios2", "verify", "done"]

    def __init__(self, tele, state, log, profiles, app_cmd):
        self.tele, self.state, self.log = tele, state, log
        self.board_name = next(iter(profiles.get("board") or {}), "")
        self.cpu_p = list(profiles.get("cpu", {}).values())[0] if profiles.get("cpu") else {}
        self.board_p = list(profiles.get("board", {}).values())[0] if profiles.get("board") else {}
        self.cooler_p = list(profiles.get("cooler", {}).values())[0] if profiles.get("cooler") else {}
        self.app_cmd = app_cmd
        self.watch = None

    @property
    def phase(self):
        return self.state.get("cpu", "phase", default="baseline")

    def set_phase(self, p):
        self.state.set("cpu", "phase", value=p)
        self.log("[CPU] phase -> " + p)

    def preflight(self):
        probs = []
        if not is_admin():
            probs.append("Run Headroom as administrator (Headroom.cmd).")
        if "Running" not in ps("(Get-Service PawnIO -ErrorAction SilentlyContinue).Status"):
            probs.append("PawnIO driver service is not running (CoreCycler needs it).")
        s = self.tele.sample()
        if "lhm_error" in s:
            probs.append("LibreHardwareMonitor web server isn't answering on :8085 - start it (CPU temps come from it).")
        else:
            w = CpuWatchdog(self.tele, self.log, self.cpu_p, self.cooler_p, "")
            r = w.check(s)
            if r:
                probs.append("Cooling check failed: " + r)
        return probs

    def baseline(self, progress):
        b = cpu_bench.run(self.tele, self.log, progress)
        b["stock_like"] = (b["max_clock"] or 0) < 5300
        self.state.set("cpu", "baseline", value=b)
        self.set_phase("bios1")
        return b

    def check_bios1(self, progress):
        """After the first BIOS visit: a single-thread load should now boost
        above the stock maximum (by about the override amount)."""
        progress("bios1", "Checking that the boost override is active")
        clk = 0
        t_end = time.time() + 8
        th = threading.Thread(target=cpu_bench.single, args=(8, 1), daemon=True)
        th.start()
        while time.time() < t_end:
            c = self.tele.sample().get("cpu") or {}
            clk = max(clk, c.get("max_core_clock") or 0)
            time.sleep(0.5)
        th.join(5)
        self.state.set("cpu", "bios1_clock", value=clk)
        return clk

    def start_search(self, start_value):
        cc = find_corecycler()
        if not cc:
            raise RuntimeError("CoreCycler not installed yet - press 'Install CoreCycler'.")
        write_config(cc, "search", start_value)
        self.state.set("cpu", "search", value={"start": start_value, "since": time.time(), "cc": cc})
        set_resume_at_logon(True, self.app_cmd)
        self.set_phase("search")
        launch_corecycler(cc)
        self.start_watchdog()

    def start_watchdog(self, on_trip=None):
        cc = (self.state.get("cpu", "search") or {}).get("cc") or find_corecycler() or ""
        if self.watch and self.watch.is_alive():
            return
        self.watch = CpuWatchdog(self.tele, self.log, self.cpu_p, self.cooler_p, cc, on_trip)
        self.watch.start()

    def search_status(self):
        s = self.state.get("cpu", "search") or {}
        if not s:
            return None
        r = parse_results(s["cc"], s["since"], s["start"])
        r["running"] = corecycler_running()
        r["whea"] = whea_events_since(s["since"]) if time.time() - s["since"] < 7 * 86400 else 0
        return r

    def finish_search(self):
        r = self.search_status()
        if self.watch:
            self.watch.stop()
        final = final_values(r["values"])
        self.state.set("cpu", "found", value=r["values"])
        self.state.set("cpu", "final", value=final)
        set_resume_at_logon(False, self.app_cmd)
        run(["schtasks", "/delete", "/tn", TASK, "/f"])
        self.set_phase("bios2")
        return final

    def start_verify(self):
        cc = find_corecycler()
        write_config(cc, "verify")
        self.state.set("cpu", "verify", value={"since": time.time()})
        launch_corecycler(cc)
        self.start_watchdog()

    def finish_verify(self, progress):
        v = self.state.get("cpu", "verify") or {}
        if self.watch:
            self.watch.stop()
        res = parse_results(find_corecycler(), v.get("since", time.time()), 0)
        tuned = cpu_bench.run(self.tele, self.log, progress)
        base = self.state.get("cpu", "baseline") or {}
        up = uplift.cpu(base, tuned)
        self.state.set("cpu", "result", value={"tuned": tuned, "uplift": up, "final": self.state.get("cpu", "final"),
                                               "verify_errors": res["failed_cores"],
                                               "when": time.strftime("%Y-%m-%d %H:%M")})
        self.set_phase("done")
        return up

    def reset(self):
        set_resume_at_logon(False, self.app_cmd)
        self.state.set("cpu", value={})
