"""Real game uplift: A/B/A/B frame-time capture with PresentMon.

uplift.py only estimates the FPS change. Here the offsets are flipped live via
NVML (stock, tuned, stock, tuned...) while the user plays, and PresentMon
records frame times for each segment. Interleaving cancels slow drift (heat,
scene changes) that a single before/after run would blame on the overclock.
"""
import csv
import os
import re
import subprocess
import time

from . import common
from .common import NO_WINDOW, TOOLS_DIR, run, verified_download
from .nvml import CLK_GRAPHICS, CLK_MEM

PROC_RE = re.compile(r"^[A-Za-z0-9 _.\-()]{1,64}\.exe$")
# Frame time column by priority: v2 FrameTime, v1/v2 MsBetweenPresents, then display-based.
FRAME_COLS = ("FrameTime", "MsBetweenPresents", "MsBetweenDisplayChange")
WARMUP_MS = 1000.0
SETTLE_S = 2.0
SESSION = "HeadroomPM"
_SYSTEM = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe", "explorer.exe",
    "conhost.exe", "dllhost.exe", "taskhostw.exe", "sihost.exe", "ctfmon.exe", "searchhost.exe",
    "runtimebroker.exe", "audiodg.exe", "spoolsv.exe", "wmiprvse.exe", "tasklist.exe", "memcompression",
    "python.exe", "pythonw.exe", "headroom.exe", "presentmon.exe", "msedgewebview2.exe", "cmd.exe",
    "powershell.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe", "textinputhost.exe",
    "securityhealthservice.exe", "securityhealthsystray.exe", "msmpeng.exe", "nissrv.exe",
    "applicationframehost.exe", "systemsettings.exe", "backgroundtaskhost.exe", "smartscreen.exe",
}


def valid_process(name):
    # fullmatch: "$" would accept a trailing newline; a leading "-" could read as a flag
    return bool(name) and bool(PROC_RE.fullmatch(name)) and name[0] not in "- "


def presentmon_path(log=None):
    """Install (or re-verify) the pinned console exe."""
    return verified_download("presentmon", os.path.join(TOOLS_DIR, "PresentMon"), log)


def running_processes(out=None):
    """Candidate game exe names from tasklist, minus system noise. `out` is for tests."""
    if out is None:
        out = run(["tasklist", "/fo", "csv", "/nh"])
    names = set()
    for row in csv.reader(out.splitlines()):
        if row and valid_process(row[0]) and row[0].lower() not in _SYSTEM:
            names.add(row[0])
    return sorted(names, key=str.lower)


def parse_frame_times(text):
    """Frame times (ms) from a PresentMon CSV (v1 or v2 columns). Rows with
    NA/empty/non-positive values are ignored."""
    rd = csv.DictReader(text.splitlines())
    col = next((c for c in FRAME_COLS if c in (rd.fieldnames or [])), None)
    if not col:
        return []
    out = []
    for row in rd:
        try:
            v = float(row.get(col) or "")
        except ValueError:
            continue
        if v > 0 and v == v and v != float("inf"):
            out.append(v)
    return out


def trim_warmup(ft, warmup_ms=WARMUP_MS):
    """Drop frames covering the first warmup_ms: clocks and caches are still settling."""
    t = 0.0
    for i, v in enumerate(ft):
        if t >= warmup_ms:
            return ft[i:]
        t += v
    return []


def _low(ft, frac):
    n = max(1, int(round(len(ft) * frac)))
    worst = sorted(ft, reverse=True)[:n]
    return 1000.0 / (sum(worst) / len(worst))


def stats(ft, warmup_ms=0.0):
    """avg FPS (frames / time), 1% and 0.1% lows (mean of slowest frames as FPS)."""
    ft = trim_warmup(ft, warmup_ms) if warmup_ms else list(ft)
    if not ft:
        return {"frames": 0, "avg_fps": 0.0, "low1": 0.0, "low01": 0.0}
    return {"frames": len(ft), "avg_fps": 1000.0 * len(ft) / sum(ft),
            "low1": _low(ft, 0.01), "low01": _low(ft, 0.001)}


def _pct(a, b):
    return (a / b - 1.0) * 100.0 if b else 0.0


def _pool(segs):
    """Combine segments: weight by frames so a longer/faster segment counts fairly."""
    ft = [x for s in segs for x in s["_ft"]]
    return stats(ft)


def default_runner(cmd, csv_path, seconds):
    """Run PresentMon for the timed capture (blocking). Faked in tests."""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 30, stdin=subprocess.DEVNULL,
                       creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
    return p.returncode


def capture(exe, process_name, seconds, csv_path, runner=None):
    """One timed capture; returns frame times (ms)."""
    if not valid_process(process_name):
        raise ValueError("Invalid process name")
    cmd = [exe, "--process_name", process_name, "--output_file", csv_path, "--timed", str(int(seconds)),
           "--terminate_after_timed", "--no_console_stats", "--session_name", SESSION,
           "--stop_existing_session"]
    if os.path.exists(csv_path):
        os.remove(csv_path)
    (runner or default_runner)(cmd, csv_path, seconds)
    try:
        with open(csv_path, encoding="utf-8", errors="replace") as f:
            ft = parse_frame_times(f.read())
    except OSError:
        ft = []
    if not ft:
        raise RuntimeError("No frames captured from %s. Is the game running and showing frames "
                           "(not minimised, and not a process other than the game's own exe)?" % process_name)
    return ft


def measure_game(nv, state, log, process_name, seconds=20, rounds=3, progress=None, cancel=None, runner=None,
                 exe=None, sleep=time.sleep):
    """A/B/A/B: stock and tuned offsets alternate every `seconds`, in one session."""
    if not valid_process(process_name):
        raise ValueError("Invalid process name. Use the game's exe name, e.g. game.exe")
    res = state.get("gpu", "result")
    if not res or res.get("core") is None:
        raise RuntimeError("Run the GPU tuning first: there is no tuned result to compare against.")
    seconds = max(5, min(int(seconds), 120))
    rounds = max(1, min(int(rounds), 6))
    tuned = {"core": int(res["core"]), "mem": int(res.get("mem") or 0)}
    stock = {"core": 0, "mem": 0}
    prev = state.get("gpu", "applied") or stock
    exe = exe or (runner and "PresentMon.exe") or presentmon_path(log)
    csv_path = os.path.join(common.DATA_DIR, "presentmon.csv")
    segs = []
    total = rounds * 2
    try:
        for i in range(total):
            if cancel and cancel():
                raise RuntimeError("Cancelled")
            mode = "stock" if i % 2 == 0 else "tuned"
            off = stock if mode == "stock" else tuned
            if progress:
                progress("seg", "%s %d/%d: %s offsets, play normally" % (process_name, i + 1, total, mode))
            nv.set_offset(CLK_GRAPHICS, off["core"])
            nv.set_offset(CLK_MEM, off["mem"])
            sleep(SETTLE_S)
            ft = capture(exe, process_name, seconds, csv_path, runner)
            s = stats(ft, WARMUP_MS)
            s["mode"] = mode
            s["_ft"] = trim_warmup(ft)
            segs.append(s)
            log("PresentMon %s %s: %.1f fps, 1%% low %.1f" % (process_name, mode, s["avg_fps"], s["low1"]))
    finally:
        try:
            nv.set_offset(CLK_GRAPHICS, int(prev.get("core") or 0))
            nv.set_offset(CLK_MEM, int(prev.get("mem") or 0))
        except Exception as e:  # noqa: BLE001
            log("Could not restore GPU offsets after the game test: %s" % e, "error")
    st = _pool([s for s in segs if s["mode"] == "stock"])
    tu = _pool([s for s in segs if s["mode"] == "tuned"])
    up = {"avg_fps": _pct(tu["avg_fps"], st["avg_fps"]), "low1": _pct(tu["low1"], st["low1"])}
    noise = 0.0
    for m in ("stock", "tuned"):
        v = [s["avg_fps"] for s in segs if s["mode"] == m]
        if len(v) > 1 and sum(v):
            noise = max(noise, (max(v) - min(v)) / (sum(v) / len(v)) * 100.0)
    if abs(up["avg_fps"]) < noise:
        verdict = "%+.1f%% average FPS is within noise (+/-%.1f%% between identical runs). Not distinguishable." % (up["avg_fps"], noise)
    else:
        verdict = "%+.1f%% average FPS and %+.1f%% 1%% lows, above the %.1f%% run-to-run noise." % (up["avg_fps"], up["low1"], noise)
    for s in segs:
        s.pop("_ft", None)
    out = {"process": process_name, "when": time.strftime("%Y-%m-%d %H:%M:%S"), "rounds": rounds,
           "seconds": seconds, "stock": st, "tuned": tu, "uplift_pct": up, "segments": segs,
           "noise_pct": noise, "verdict": verdict}
    games = [out] + list(state.get("games") or [])
    state.set("games", value=games[:20])
    return out
