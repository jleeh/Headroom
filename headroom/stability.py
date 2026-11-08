"""Crash monitoring for an applied overclock. Windows logs GPU resets, WHEA
hardware errors and bugchecks; a scheduled task starts headroom_crash.pyw on
each of those events (no polling daemon), which calls process() here:
query recent events, classify, record in stability.json, and back the GPU
overclock off (or revert it) when it looks like the culprit. CPU settings live
in the BIOS, so CPU-suspect events are only recorded (with the core) for the UI.

Everything Windows-specific (wevtutil, schtasks, uptime, the GPU) is behind a
small function or an injectable parameter so tests can fake it."""
import glob
import ntpath
import os
import re
import socket
import struct
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .common import DATA_DIR, SCAN_FILE, read_json, run, write_json

STAB_FILE = os.path.join(DATA_DIR, "stability.json")
FLIGHT_FILE = os.path.join(DATA_DIR, "flight.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
MINIDUMP_DIR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Minidump")
LOCK_PORT = 7873
TASKS = ["Headroom Crash Watch (System)", "Headroom Crash Watch (Application)"]
DEFAULT_CONFIG = {"auto_backoff": True, "max_crashes": 3, "window_days": 7, "monitor_games_only": False}
MAX_EVENTS, MAX_SEEN = 200, 500
CORE_STEP_MHZ, MEM_STEP_AB = 15, 250
NV_MODULES = ("nvwgf2umx.dll", "nvwgf2um.dll", "nvd3dumx.dll", "nvoglv64.dll", "nvlddmkm.sys")
GPU_BUGCHECKS = {0x116, 0x117, 0x119, 0x10E}
CPU_BUGCHECKS = {0x124, 0x101}
CLK_GRAPHICS, CLK_MEM = 0, 2

SYSTEM_XPATH = (
    "*[System[Provider[@Name='nvlddmkm'] or (Provider[@Name='Display'] and EventID=4101) or "
    "(Provider[@Name='Microsoft-Windows-WHEA-Logger'] and (EventID=18 or EventID=19)) or "
    "(Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=41) or "
    "(Provider[@Name='Microsoft-Windows-WER-SystemErrorReporting'] and EventID=1001)]]")
APP_XPATH = ("*[System[Provider[@Name='Application Error'] and EventID=1000] and EventData[" +
             " or ".join("Data='%s'" % m for m in NV_MODULES) + "]]")


# ---- scheduled tasks ----------------------------------------------------------

def install_event_tasks(pythonw, app_dir, run_fn=run):
    """Create the two event-triggered tasks (needs admin). Returns {task: ok}."""
    tr = '"%s" "%s"' % (pythonw, ntpath.join(app_dir, "bin", "headroom_crash.pyw"))
    res = {}
    for name, chan, xp in zip(TASKS, ("System", "Application"), (SYSTEM_XPATH, APP_XPATH), strict=True):
        run_fn(["schtasks", "/create", "/f", "/rl", "highest", "/sc", "onevent", "/ec", chan,
                "/mo", xp, "/tn", name, "/tr", tr])
        res[name] = task_exists(name, run_fn)
    return res


def task_exists(name, run_fn=run):
    return "TaskName" in (run_fn(["schtasks", "/query", "/tn", name, "/fo", "list"]) or "")


def remove_event_tasks(run_fn=run):
    for name in TASKS:
        run_fn(["schtasks", "/delete", "/f", "/tn", name])
    return {n: task_exists(n, run_fn) for n in TASKS}


def tasks_installed(run_fn=run):
    return all(task_exists(n, run_fn) for n in TASKS)


# ---- event parsing / classification (pure) -------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_events_xml(text):
    """wevtutil /f:xml prints bare <Event> elements one after another, so wrap
    them. Returns [{channel, id, record, provider, time, data{name:text}, texts[], message}]."""
    text = re.sub(r"<\?xml[^>]*\?>", "", text or "")
    try:
        root = ET.fromstring("<Events>%s</Events>" % text)
    except ET.ParseError:
        return []
    out = []
    for ev in root:
        d = {"data": {}, "texts": [], "message": "", "provider": "", "id": 0, "record": 0, "channel": "", "time": 0.0}
        for el in ev.iter():
            tag = _local(el.tag)
            if tag == "Provider":
                d["provider"] = el.get("Name", "")
            elif tag == "EventID":
                try:
                    d["id"] = int((el.text or "0").strip())
                except ValueError:
                    pass
            elif tag == "EventRecordID":
                d["record"] = int((el.text or "0").strip() or 0)
            elif tag == "Channel":
                d["channel"] = (el.text or "").strip()
            elif tag == "TimeCreated":
                d["time"] = _parse_time(el.get("SystemTime", ""))
            elif tag == "Data":
                v = (el.text or "").strip()
                d["texts"].append(v)
                if el.get("Name"):
                    d["data"][el.get("Name")] = v
            elif tag == "Message":
                d["message"] = (el.text or "").strip()
        out.append(d)
    return out


def _parse_time(s):
    try:
        s = s.strip().rstrip("Z")
        s = re.sub(r"(\.\d{6})\d*$", r"\1", s)
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def event_key(ev):
    return "%s:%s" % (ev.get("channel", ""), ev.get("record", 0))


def parse_bugcheck_code(ev):
    """BugCheck 1001 text is 'The bugcheck was: 0x00000116 (0x..., ...)'; Kernel-Power 41
    has a numeric BugCheckCode instead. Returns int or None."""
    bc = ev["data"].get("BugcheckCode") or ev["data"].get("BugCheckCode")
    if bc and bc.isdigit() and int(bc):
        return int(bc)
    for t in list(ev["texts"]) + [ev.get("message", "")]:
        m = re.search(r"0x([0-9a-fA-F]{8})\b", t) or re.search(r"bugcheck was:?\s*0x([0-9a-fA-F]+)", t, re.I)
        if m:
            return int(m.group(1), 16)
    return None


def bugcheck_suspect(code):
    if code in GPU_BUGCHECKS:
        return "gpu"
    if code in CPU_BUGCHECKS:
        return "cpu"
    return "unknown"


def threads_per_core(scan=None):
    cpu = ((scan if scan is not None else read_json(SCAN_FILE, {})) or {}).get("cpu") or {}
    try:
        t, c = int(cpu.get("threads")), int(cpu.get("cores"))
        if c > 0 and t % c == 0 and t >= c:
            return t // c
    except (TypeError, ValueError):
        pass
    return 2


def apic_to_core(apic, tpc=2):
    return int(apic) // max(1, tpc)


def _apic_id(ev):
    for k, v in ev["data"].items():
        if "apic" in k.lower() and str(v).strip().isdigit():
            return int(v)
    for t in list(ev["texts"]) + [ev.get("message", "")]:
        m = re.search(r"APIC ID:?\s*(\d+)", t, re.I)
        if m:
            return int(m.group(1))
    return None


def classify(ev, scan=None):
    """-> dict(kind, suspect, detail, bugcheck, core) or None if not interesting.
    suspect: gpu | cpu | unknown. gpu_app_crash is 'soft' (a game crashed in the
    NVIDIA user-mode driver; the driver recovered)."""
    prov, eid = ev["provider"], ev["id"]
    r = {"kind": None, "suspect": "unknown", "detail": "", "bugcheck": None, "core": None, "soft": False}
    if prov == "nvlddmkm" or (prov == "Display" and eid == 4101):
        r.update(kind="gpu_tdr", suspect="gpu", detail="NVIDIA driver reset (%s %d)" % (prov, eid))
    elif prov == "Application Error" and eid == 1000:
        mod = next((m for m in NV_MODULES if any(m in t.lower() for t in ev["texts"])), None)
        if not mod:
            return None
        app = ev["texts"][0] if ev["texts"] else "?"
        r.update(kind="gpu_app_crash", suspect="gpu", soft=True, detail="%s crashed in %s" % (app, mod))
    elif prov == "Microsoft-Windows-WHEA-Logger" and eid in (18, 19):
        apic = _apic_id(ev)
        core = apic_to_core(apic, threads_per_core(scan)) if apic is not None else None
        r.update(kind="whea_corrected" if eid == 19 else "whea_fatal", suspect="cpu", core=core,
                 detail="WHEA %s hardware error%s" % ("corrected" if eid == 19 else "fatal",
                                                      " on core %d (APIC %d)" % (core, apic) if core is not None else ""))
    elif prov == "Microsoft-Windows-Kernel-Power" and eid == 41:
        code = parse_bugcheck_code(ev)
        r.update(kind="unexpected_shutdown", suspect=bugcheck_suspect(code) if code else "unknown", bugcheck=code,
                 detail="Unexpected shutdown (power loss, freeze or crash)")
    elif prov == "Microsoft-Windows-WER-SystemErrorReporting" and eid == 1001:
        code = parse_bugcheck_code(ev)
        r.update(kind="bugcheck", suspect=bugcheck_suspect(code), bugcheck=code,
                 detail="Blue screen %s" % ("0x%08X" % code if code is not None else "(unknown code)"))
    else:
        return None
    if r["bugcheck"] is not None:
        r["bugcheck"] = "0x%08X" % r["bugcheck"]
    return r


# ---- minidump fallback ------------------------------------------------------------

def parse_minidump_bugcheck(path):
    """Kernel dump header: 'PAGEDU64' at 0, BugCheckCode u32 at 0x38, 4 x u64 params at 0x40."""
    try:
        with open(path, "rb") as f:
            h = f.read(0x60)
    except OSError:
        return None
    if len(h) < 0x60 or h[:8] != b"PAGEDU64":
        return None
    code = struct.unpack_from("<I", h, 0x38)[0]
    params = list(struct.unpack_from("<4Q", h, 0x40))
    return {"code": code, "bugcheck": "0x%08X" % code, "params": params, "suspect": bugcheck_suspect(code),
            "when": os.path.getmtime(path)}


def newest_minidump(dirpath=MINIDUMP_DIR):
    files = glob.glob(os.path.join(dirpath, "*.dmp"))
    return max(files, key=os.path.getmtime) if files else None


# ---- records ------------------------------------------------------------------------

def load(path=STAB_FILE):
    d = read_json(path, {}) or {}
    d["config"] = clean_config(d.get("config"))
    d["seen"] = list(d.get("seen") or [])[-MAX_SEEN:]
    d["events"] = list(d.get("events") or [])[-MAX_EVENTS:]
    return d


def save(d, path=STAB_FILE):
    d["seen"] = d["seen"][-MAX_SEEN:]
    d["events"] = d["events"][-MAX_EVENTS:]
    write_json(path, d)


def _int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def clean_config(c):
    c = c if isinstance(c, dict) else {}
    return {"auto_backoff": bool(c.get("auto_backoff", True)),
            "max_crashes": _int(c.get("max_crashes", 3), 1, 20, 3),
            "window_days": _int(c.get("window_days", 7), 1, 90, 7),
            "monitor_games_only": bool(c.get("monitor_games_only", False))}


def set_config(new, path=STAB_FILE):
    d = load(path)
    merged = dict(d["config"])
    merged.update({k: v for k, v in (new or {}).items() if k in DEFAULT_CONFIG})
    d["config"] = clean_config(merged)
    save(d, path)
    return d["config"]


def events_in_window(events, days, now=None):
    t0 = (now or time.time()) - days * 86400
    return [e for e in events if e.get("t", 0) >= t0]


def summary(state=None, path=STAB_FILE, tasks=None):
    d = load(path)
    cfg = d["config"]
    win = events_in_window(d["events"], cfg["window_days"])
    counts, cores = {}, {}
    for e in win:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        if e.get("core") is not None:
            cores[str(e["core"])] = cores.get(str(e["core"]), 0) + 1
    st = None
    if state is not None:
        rv = state.get("gpu", "stability_reverted")
        if rv:
            st = "reverted to stock on %s" % rv.get("when", "?")
        elif state.get("gpu", "recheck", "due"):
            st = "re-check due: %s" % state.get("gpu", "recheck", "reason")
        elif state.get("gpu", "applied"):
            st = "overclock active, %d GPU-suspect crash(es) in window" % sum(1 for e in win if e.get("counts"))
    out = {"config": cfg, "recent": d["events"][-20:][::-1], "counts": counts, "cpu_cores_to_loosen": cores,
           "status": st or "no overclock applied", "total": len(d["events"])}
    if tasks is not None:
        out["tasks_installed"] = tasks
    return out


def flight_tail(n=60, path=FLIGHT_FILE):
    f = read_json(path, {}) or {}
    return (f.get("samples") or [])[-n:]


# ---- actions ------------------------------------------------------------------------------

def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def act(rec, state, nv, log, cfg, events, now=None):
    """Decide and perform the reaction to one new event `rec` (already classified
    and appended to `events` by the caller). Returns the action text."""
    now = now or time.time()
    applied = state.get("gpu", "applied")
    rec["applied"] = dict(applied) if applied else None
    if rec["suspect"] == "cpu":
        return "CPU-suspect: nothing changed (CPU settings live in the BIOS)" + \
            (", note core %d" % rec["core"] if rec.get("core") is not None else "")
    if rec["suspect"] != "gpu":
        return "Cause unknown: nothing changed"
    if not applied:
        return "No GPU overclock was active: nothing changed"
    if rec["kind"] == "gpu_app_crash":
        recent = [e for e in events if e is not rec and e["kind"] == "gpu_app_crash" and now - e["t"] <= 86400]
        if not recent:
            return "Soft event (game crashed in the driver): watching, no change yet"
        rec["detail"] += " (2nd within 24 h, treated as a driver reset)"
    rec["counts"] = True
    win = [e for e in events_in_window(events, cfg["window_days"], now) if e.get("counts")]
    if len(win) >= cfg["max_crashes"]:
        return revert(state, nv, log, "%d GPU-suspect crashes in %d days" % (len(win), cfg["window_days"]), now)
    if not cfg["auto_backoff"]:
        return "Auto back-off is off: nothing changed"
    return backoff(state, nv, log, cfg, now)


def backoff(state, nv, log, cfg, now=None):
    now = now or time.time()
    r = dict(state.get("gpu", "result") or {})
    if not r:
        return "No saved result to back off"
    ratio = r.get("mem_ratio") or 0.5
    recent = [b for b in r.get("backoffs", []) if now - b.get("t", 0) <= cfg["window_days"] * 86400]
    last = recent[-1]["kind"] if recent else None
    core, mem = int(r.get("core", 0)), int(r.get("mem", 0))
    order = ["mem", "core"] if last == "core" else ["core", "mem"]
    for kind in order:
        if kind == "core" and core > 0:
            new = max(0, core - CORE_STEP_MHZ)
            r["backoffs"] = r.get("backoffs", []) + [{"t": now, "kind": "core", "from": core, "to": new}]
            r["core"], core = new, new
            what = "core offset %+d -> %+d MHz" % (r["backoffs"][-1]["from"], new)
            break
        if kind == "mem" and mem > 0:
            new = max(0, mem - int(round(MEM_STEP_AB * ratio)))
            ab = r.get("mem_afterburner")
            r["backoffs"] = r.get("backoffs", []) + [{"t": now, "kind": "mem", "from": mem, "to": new}]
            r["mem"], mem = new, new
            if ab is not None:
                r["mem_afterburner"] = max(0, int(ab) - MEM_STEP_AB)
            what = "memory offset lowered by %d (Afterburner units)" % MEM_STEP_AB
            break
    else:
        return revert(state, nv, log, "nothing left to back off", now)
    nv.set_offset(CLK_GRAPHICS, core)
    nv.set_offset(CLK_MEM, mem)
    state.set("gpu", "result", value=r)
    state.set("gpu", "applied", value={"core": core, "mem": mem})
    msg = "Auto back-off after a GPU crash: " + what
    log(msg, "warn")
    return msg


def revert(state, nv, log, reason, now=None):
    now = now or time.time()
    nv.reset_all()
    state.set("gpu", "applied", value=None)
    state.set("gpu", "stability_reverted", value={"when": _ts(now), "reason": reason})
    msg = "Reverted to stock: %s. Re-run Auto OC to try again." % reason
    log(msg, "error")
    return msg


# ---- processing -----------------------------------------------------------------------------

def uptime_s():
    try:
        import ctypes
        return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:  # noqa: BLE001
        return 0.0


def query(channel, xpath, seconds, run_fn=run, count=50):
    q = _with_age(xpath, seconds)
    out = run_fn(["wevtutil", "qe", channel, "/q:" + q, "/f:xml", "/c:%d" % count, "/rd:true"], 20)
    evs = parse_events_xml(out)
    for e in evs:
        e["channel"] = e["channel"] or channel
    return evs


def _with_age(xpath, seconds):
    """Add the time filter as an extra System[] predicate: '*[System[...] and EventData[...]]'
    -> '*[System[TimeCreated[...]] and (<original inner>)]'."""
    inner = xpath[2:-1]
    return "*[System[TimeCreated[timediff(@SystemTime) <= %d]] and %s]" % (int(seconds * 1000), inner)


def _lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", LOCK_PORT))
    s.listen(1)
    return s


def collect(run_fn=run, recent_s=600, up_s=None):
    """Recent events from both channels, plus everything since boot for the
    after-reboot ones (Kernel-Power 41, BugCheck 1001)."""
    up = uptime_s() if up_s is None else up_s
    evs = query("System", SYSTEM_XPATH, max(recent_s, up + 120), run_fn)
    cutoff = time.time() - recent_s
    evs = [e for e in evs if e["id"] in (41, 1001) or e["time"] >= cutoff]
    evs += query("Application", APP_XPATH, recent_s, run_fn)
    evs.sort(key=lambda e: e["time"])
    return evs


def process(state, nv, log, run_fn=run, path=STAB_FILE, now=None, up_s=None, minidump_dir=MINIDUMP_DIR,
            flight_path=FLIGHT_FILE, scan=None, driver=None, events=None):
    """Handle every new event. Returns the list of records added. `events`
    lets tests (or the apply script) supply already-parsed events."""
    now = now or time.time()
    d = load(path)
    cfg = d["config"]
    evs = events if events is not None else collect(run_fn, up_s=up_s)
    new = [e for e in evs if event_key(e) not in d["seen"]]
    d["seen"] += [event_key(e) for e in new]
    # Kernel-Power 41 and BugCheck 1001 describe the same crash: keep the richer 1001
    bc_times = [e["time"] for e in new if e["id"] == 1001 and e["provider"].endswith("SystemErrorReporting")]
    added = []
    for ev in new:
        c = classify(ev, scan)
        if not c:
            continue
        if c["kind"] == "unexpected_shutdown" and any(0 <= t - ev["time"] <= 900 for t in bc_times):
            continue
        if c["kind"] == "unexpected_shutdown" and c["bugcheck"] is None:
            dump = newest_minidump(minidump_dir)
            m = parse_minidump_bugcheck(dump) if dump else None
            if m and abs(m["when"] - ev["time"]) < 900:
                c.update(bugcheck=m["bugcheck"], suspect=m["suspect"], detail=c["detail"] + " (minidump %s)" % m["bugcheck"])
        rec = {"t": ev["time"] or now, "kind": c["kind"], "suspect": c["suspect"], "detail": c["detail"],
               "bugcheck": c["bugcheck"], "core": c["core"], "driver": driver, "flight": flight_tail(60, flight_path)}
        rec["action"] = act(rec, state, nv, log, cfg, d["events"] + [rec], now) if _fresh(ev, now, up_s) else "Old event: recorded only"
        d["events"].append(rec)
        added.append(rec)
        log("Stability: %s (%s suspect): %s. %s" % (rec["kind"], rec["suspect"], rec["detail"], rec["action"]),
            "warn" if rec["suspect"] != "unknown" else "info")
    save(d, path)
    return added


def _fresh(ev, now, up_s):
    """Only act on events from this boot (or the last 10 min): older ones were
    already dealt with by the setting that was applied back then."""
    up = uptime_s() if up_s is None else up_s
    return ev["time"] == 0 or now - ev["time"] <= max(600, up) or ev["id"] == 1001


def run_locked(fn, *a, **k):
    """Run fn under the single-instance lock; a second trigger waits for the first
    (up to 60 s) and then finds nothing new thanks to the dedup list."""
    end = time.time() + 60
    while True:
        try:
            lock = _lock()
            break
        except OSError:
            if time.time() > end:
                return None
            time.sleep(1)
    try:
        return fn(*a, **k)
    finally:
        lock.close()


# ---- monitor helpers (used by headroom_monitor.pyw) ---------------------------------------------

MONITOR_PORT = 7874
RING = 120
IDLE_WINDOW_S, IDLE_MARGIN_C, MAX_AGE_DAYS = 60, 8, 90


def clamp_offsets(core, mem):
    """The hard caps in profiles.CAP; mem is in NVML units, so the cap is converted by the ratio."""
    from .profiles import CAP
    return max(0, min(CAP["gpu_max_core"], int(core))), max(0, int(mem))


def clamp_result(r):
    from .profiles import CAP
    ratio = r.get("mem_ratio") or 0.5
    core, mem = clamp_offsets(r.get("core", 0), r.get("mem", 0))
    return core, min(mem, int(CAP["gpu_max_mem_ab"] * ratio))


def should_recheck(idle_temps, idle_ref, when, now=None, margin=IDLE_MARGIN_C, max_age_days=MAX_AGE_DAYS, min_samples=IDLE_WINDOW_S // 2):
    """Reason string if the saved overclock should be re-tested, else None. A GPU
    that idles >= margin C hotter than when it was tuned (ambient/dust/fans) was
    tuned for cooler conditions; and a result over max_age_days old is stale."""
    now = now or time.time()
    try:
        age = (now - time.mktime(time.strptime(when, "%Y-%m-%d %H:%M"))) / 86400
        if age > max_age_days:
            return "tuning result is %d days old" % age
    except (TypeError, ValueError):
        pass
    if idle_ref is not None and len(idle_temps) >= min_samples:
        hot = sum(1 for t in idle_temps if t >= idle_ref + margin)
        if hot >= 0.9 * len(idle_temps):
            return "idle GPU temperature is %d C, tuned at %d C (ambient or cooling changed)" % (
                sorted(idle_temps)[len(idle_temps) // 2], idle_ref)
    return None


def decide_apply(mode, games, running_procs, recheck_due=False, reverted=False):
    """Should the saved offsets be applied right now?"""
    if reverted or recheck_due:
        return False
    if mode != "games":
        return True
    want = {g.lower() for g in games or []}
    return bool(want & {p.lower() for p in running_procs})


def running_processes(run_fn=run):
    out = run_fn(["tasklist", "/fo", "csv", "/nh"])
    procs = set()
    for ln in (out or "").splitlines():
        m = re.match(r'"([^"]+)"', ln.strip())
        if m:
            procs.add(m.group(1).lower())
    return procs


def pid_alive(pid):
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return True    # can't tell: don't fight a live process


def fans_stale(status, now=None, alive=pid_alive, max_age=10):
    """True if the fan process left the fans on a fixed % and is gone."""
    now = now or time.time()
    s = status or {}
    if s.get("mode") != "manual" or now - s.get("t", 0) <= max_age:
        return False
    return not (s.get("pid") and alive(s["pid"]))


def sample_flight(tele_snap):
    """One compact flight-recorder row from a Telemetry snapshot."""
    g, x = tele_snap.get("gpu") or {}, tele_snap.get("gpu_x") or {}
    return {"t": round(tele_snap.get("t", time.time()), 1), "temp": g.get("temp"), "power": g.get("power"),
            "clk": g.get("clk"), "mem_clk": g.get("mem_clk"), "util": g.get("util"), "fan": g.get("fan"),
            "throttle": g.get("throttle"), "mem_junction": x.get("mem_junction") if isinstance(x, dict) else None}


class Flight:
    """Ring of the last RING samples, flushed to disk every few seconds so a hard
    freeze still leaves the last minute or two of temperatures and clocks."""

    def __init__(self, path=FLIGHT_FILE, size=RING, every=5):
        self.path, self.size, self.every = path, size, every
        self.ring, self.last = [], 0.0

    def add(self, row, now=None):
        now = now or time.time()
        self.ring = (self.ring + [row])[-self.size:]
        if now - self.last >= self.every:
            self.flush(now)

    def flush(self, now=None):
        write_json(self.path, {"written": now or time.time(), "samples": self.ring})
        self.last = now or time.time()


def driver_change(result, nv):
    """Reason string if the driver/VBIOS differ from what the result was tuned on."""
    for key, fn in (("driver", nv.driver), ("vbios", nv.vbios)):
        was = (result or {}).get(key)
        if was:
            try:
                now = fn()
            except Exception:  # noqa: BLE001
                continue
            if now != was:
                return "%s changed from %s to %s" % (key, was, now)
    return None


def monitor_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", MONITOR_PORT))
    s.listen(1)
    return s


def apply_clamped(nv, state, log):
    """gpu_tune.apply_saved, but with the offsets clamped to the hard caps first."""
    r = state.get("gpu", "result")
    if not r:
        return False
    core, mem = clamp_result(r)
    nv.set_offset(CLK_GRAPHICS, core)
    nv.set_offset(CLK_MEM, mem)
    state.set("gpu", "applied", value={"core": core, "mem": mem})
    log("Applied saved GPU profile: core %+d, mem %+d" % (core, mem))
    return True


def stock(nv, state, log, why):
    nv.reset_all()
    state.set("gpu", "applied", value=None)
    log("GPU back to stock (%s)" % why)


def set_recheck(state, log, reason):
    if not (state.get("gpu", "recheck") or {}).get("due"):
        state.set("gpu", "recheck", value={"due": True, "reason": reason})
        log("Re-check recommended: %s" % reason, "warn")
