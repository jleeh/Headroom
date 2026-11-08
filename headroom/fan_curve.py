"""GPU fan curve. NVML has no curve API, only "set fan %" and "back to auto", so a
small elevated process (headroom_fans.pyw) runs the curve: once a second it reads
the core temp (NVML) and memory junction temp (LibreHardwareMonitor) and sets the fans
to whichever curve asks for more.

Files in %APPDATA%\\Headroom:
  fan_curve.json    config, written by the UI backend
  fans_boost.json   {"pct":85,"until":epoch}, written by the tuner; expires on its own
                    so a crashed tuner can't hold the fans up forever
  fans_status.json  live status, written by the fan process

Safety:
  - fans only ever go faster than the curve, never slower: emergency 100% at
    core >= 80 C or memory junction >= 94 C (below Headroom's 83/96 C cut-offs),
    and 100% if the core temperature can't be read;
  - when the curve says 0% (cool), the card's own control comes back, so zero-rpm
    idle still works;
  - on exit (window closed, logoff, error) the fans go back to the card's own curve.
"""
import atexit
import json
import os
import socket
import time

from .common import DATA_DIR

CONFIG_FILE = os.path.join(DATA_DIR, "fan_curve.json")
BOOST_FILE = os.path.join(DATA_DIR, "fans_boost.json")
STATUS_FILE = os.path.join(DATA_DIR, "fans_status.json")
LOCK_PORT = 7872
FAN_TASK = "Headroom Fan Curve"

DEFAULT = {
    "enabled": False,
    # [temp C, fan %]. 0% = let the card decide (zero-rpm at idle).
    "core": [[45, 0], [50, 35], [60, 50], [68, 65], [75, 80], [80, 100]],
    "mem": [[62, 0], [70, 40], [80, 60], [88, 80], [94, 100]],
}
EMERG_CORE, EMERG_MEM = 80, 94
# Other programs that drive GPU fans. Two controllers writing the same fan fight each
# other (speeds flip every second, neither curve holds), so Headroom yields to them:
# it never writes the fans while one runs and hands control back to the card.
OTHER_FAN_CONTROLLERS = {
    "glint.exe": "Glint", "fancontrol.exe": "FanControl", "msiafterburner.exe": "MSI Afterburner",
    "signalrgb.exe": "SignalRGB", "argusmonitor.exe": "Argus Monitor", "evgaprecisionx1.exe": "EVGA Precision X1",
    "asusgputweakiii.exe": "ASUS GPU Tweak III", "nzxt cam.exe": "NZXT CAM", "icue.exe": "Corsair iCUE",
    "fanctrl.exe": "FanCtrl",
}
CONTEST_SEC = 8           # read-back differs from what we set for this long = someone else is writing
CONTEST_TOL = 12          # % tolerance (fans lag and report coarsely)
DOWN_PER_SEC = 2          # ramp down slowly, up instantly
AUTO_AFTER_SEC = 30       # curve must say 0% this long before handing back to the card


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, path)


def _clean(points, lo_t, hi_t):
    pts = []
    for p in points or []:
        try:
            t, v = int(p[0]), int(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        pts.append([max(lo_t, min(hi_t, t)), max(0, min(100, v))])
    pts.sort()
    # fan % may never drop as temperature rises
    for i in range(1, len(pts)):
        pts[i][1] = max(pts[i][1], pts[i - 1][1])
    return pts


def load_config():
    c = dict(DEFAULT)
    c.update(_read(CONFIG_FILE, {}) or {})
    c["core"] = _clean(c.get("core"), 20, 90) or DEFAULT["core"]
    c["mem"] = _clean(c.get("mem"), 30, 105) or DEFAULT["mem"]
    return c


def save_config(cfg):
    c = load_config()
    for k in ("enabled", "core", "mem"):
        if k in cfg:
            c[k] = cfg[k]
    c["enabled"] = bool(c["enabled"])
    c["core"] = _clean(c["core"], 20, 90) or DEFAULT["core"]
    c["mem"] = _clean(c["mem"], 30, 105) or DEFAULT["mem"]
    _write(CONFIG_FILE, c)
    return c


def set_boost(pct, seconds=90):
    _write(BOOST_FILE, {"pct": int(pct), "until": time.time() + seconds})


def clear_boost():
    try:
        os.remove(BOOST_FILE)
    except OSError:
        pass


def status():
    s = _read(STATUS_FILE, {}) or {}
    s["running"] = bool(s) and time.time() - s.get("t", 0) < 5
    return s


def other_controllers(proc_lister=None):
    """Names of running programs that also control GPU fans."""
    from .common import process_running
    lister = proc_lister or process_running
    return [OTHER_FAN_CONTROLLERS[n] for n in lister(*OTHER_FAN_CONTROLLERS)]


class ContestJudge:
    """Detects an unknown second controller: the fan keeps reading back far from the
    duty we set although the card had time to get there."""

    def __init__(self):
        self.since = None

    def check(self, target, reading, now):
        if target is None or reading is None or abs(reading - target) <= CONTEST_TOL:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return now - self.since >= CONTEST_SEC


def interp(points, t):
    if t is None or not points:
        return 0
    if t <= points[0][0]:
        return points[0][1] if t == points[0][0] else 0
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t <= t1:
            return v0 + (v1 - v0) * (t - t0) / max(1, t1 - t0)
    return points[-1][1]


def target_pct(cfg, core, mj):
    """Pure function: what the curve asks for (0 = card's own control)."""
    if core is None:
        return 100, "core temp unreadable"
    if core >= EMERG_CORE or (mj is not None and mj >= EMERG_MEM):
        return 100, "emergency"
    want = interp(cfg["core"], core)
    if mj is not None:
        want = max(want, interp(cfg["mem"], mj))
    else:
        want = max(want, interp(cfg["core"], core + 8))   # no LHM: be generous
    return int(round(want)), "curve"


def _lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", LOCK_PORT))
    s.listen(1)
    return s


def run_daemon(log=print):
    """Blocking loop. Exits (fans back to auto) when disabled and no boost."""
    from .nvml import Nvml, NvmlError
    from .sensors import Lhm
    try:
        lock = _lock()
    except OSError:
        return  # already running
    nv = Nvml()
    lhm = Lhm()
    fmin, fmax = nv.fan_limits()
    # start from a known place: whatever set the fans before (another tool, a
    # crashed run) is cleared, then the curve takes over
    state = {"manual": True, "cur": 0, "zero_since": None}

    def to_auto():
        if state["manual"]:
            try:
                nv.reset_fans()
            except NvmlError as e:
                log("fan reset failed: %s" % e)
        state["manual"] = False
        state["cur"] = 0

    def on_exit():
        state["manual"] = True
        to_auto()
        try:
            _write(STATUS_FILE, {"t": 0, "mode": "stopped"})
        except OSError:
            pass
    atexit.register(on_exit)
    to_auto()
    log("Fan curve running (fan range %d-%d%%)" % (fmin, fmax))
    idle_off = 0
    judge = ContestJudge()
    yielded = None          # name of the program we yielded to
    last_proc_check = 0
    try:
        while True:
            now = time.time()
            if now - last_proc_check >= 5:
                last_proc_check = now
                others = other_controllers()
                if others and yielded is None:
                    yielded = others[0]
                    to_auto()
                    log("Fans: %s is controlling the GPU fans, Headroom's curve is paused (no fighting over the fans)" % yielded, "warn")
                elif not others and yielded and not yielded.startswith("another program"):
                    log("Fans: %s closed, Headroom's curve resumes" % yielded)
                    yielded = None
            if yielded:
                # don't touch the fans at all (no reset either: that would fight the other program)
                _write(STATUS_FILE, {"t": now, "pid": os.getpid(), "mode": "yielded", "why": "%s controls the fans" % yielded,
                                     "target": None, "fan": nv.fan_pct(), "core": None, "mj": None, "yielded_to": yielded})
                if not load_config()["enabled"]:
                    break
                time.sleep(1)
                continue
            cfg = load_config()
            b = _read(BOOST_FILE)
            boost = b["pct"] if b and b.get("until", 0) > time.time() else 0
            if not cfg["enabled"] and not boost:
                to_auto()
                idle_off += 1
                if idle_off > 5:
                    break
                time.sleep(1)
                continue
            idle_off = 0
            try:
                core = nv.temp()
            except NvmlError:
                core = None
            mj = lhm.gpu_extra().get("mem_junction") if lhm.poll() else None
            want, why = target_pct(cfg, core, mj) if cfg["enabled"] else (0, "boost only")
            if boost > want:
                want, why = boost, "tuning boost"
            now = time.time()
            if want <= 0:
                state["zero_since"] = state["zero_since"] or now
                if state["manual"] and now - state["zero_since"] >= AUTO_AFTER_SEC:
                    to_auto()
                    log("Fans: cool, handed back to the card (zero-rpm allowed)")
                elif state["manual"]:
                    want = max(fmin, state["cur"] - DOWN_PER_SEC)
            else:
                state["zero_since"] = None
            if state["manual"] and judge.check(state["cur"], nv.fan_pct(), now):
                yielded = "another program (fan speed keeps changing away from Headroom's setting)"
                state["manual"] = False   # stop writing; don't reset, the other program is in charge
                log("Fans: another program is setting the GPU fans; Headroom's curve is paused. Close it, then toggle the curve.", "warn")
                continue
            if want > 0:
                want = max(fmin, min(fmax, want))
                if state["manual"] and want < state["cur"] and why == "curve":
                    want = max(want, state["cur"] - DOWN_PER_SEC)
                if not state["manual"] or want != state["cur"]:
                    try:
                        nv.set_fans(want)
                        state["manual"], state["cur"] = True, want
                    except NvmlError as e:
                        log("fan set failed: %s" % e)
            _write(STATUS_FILE, {"t": now, "pid": os.getpid(),
                                 "mode": ("manual" if state["manual"] else "card"),
                                 "why": why, "target": state["cur"] if state["manual"] else None,
                                 "fan": nv.fan_pct(), "core": core, "mj": mj})
            time.sleep(1)
    finally:
        on_exit()
        lock.close()
        log("Fan curve stopped; fans back to the card's own control")
