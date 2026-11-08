"""Background watcher while an overclock is applied (started at logon by the
GPU Apply script): flight recorder, driver / ambient re-check flags, per-game
profiles and the stale-fan watchdog. Never changes clocks except to apply or
remove the saved profile in "games" mode. Single instance (port 7874)."""
import os
import sys
import time
import traceback
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo / install root
from headroom import fan_curve, stability  # noqa: E402
from headroom.common import DATA_DIR, SETTINGS_FILE, Log, State, read_json  # noqa: E402


def loop(nv, tele, log, now=time.time, sleep=time.sleep, max_ticks=None):
    flight = stability.Flight()
    idle = deque(maxlen=stability.IDLE_WINDOW_S)
    tick = 0
    while max_ticks is None or tick < max_ticks:
        tick += 1
        snap = tele.sample()
        flight.add(stability.sample_flight(snap), now())
        g = snap.get("gpu") or {}
        if g.get("util") is not None and g["util"] < 10 and g.get("temp") is not None:
            idle.append(g["temp"])
        else:
            idle.clear()
        if tick % 3 == 0:
            st = State()
            if st.get("gpu", "tuning", "in_progress"):
                sleep(1)
                continue
            r = st.get("gpu", "result")
            cfg = read_json(SETTINGS_FILE, {}) or {}
            mode, games = cfg.get("gpu_apply_mode", "always"), cfg.get("games") or []
            if r:
                why = stability.driver_change(r, nv)
                if why:
                    stability.set_recheck(st, log, why)
                    if st.get("gpu", "applied"):
                        stability.stock(nv, st, log, why)
                ref = st.get("gpu", "idle_ref", "temp")
                if ref is None and len(idle) >= stability.IDLE_WINDOW_S:
                    ref = sorted(idle)[len(idle) // 2]       # first full idle minute becomes the reference
                    st.set("gpu", "idle_ref", value={"temp": ref, "when": time.strftime("%Y-%m-%d %H:%M")})
                why = stability.should_recheck(list(idle), ref, r.get("when"), now())
                if why:
                    stability.set_recheck(st, log, why)
                if mode == "games":
                    procs = stability.running_processes()
                    want = stability.decide_apply(mode, games, procs, (st.get("gpu", "recheck") or {}).get("due"),
                                                  bool(st.get("gpu", "stability_reverted")))
                    has = bool(st.get("gpu", "applied"))
                    if want and not has:
                        stability.apply_clamped(nv, st, log)
                    elif not want and has:
                        stability.stock(nv, st, log, "no listed game running")
            if stability.fans_stale(fan_curve.status(), now()):
                nv.reset_fans()
                log("Fan process died with the fans on a fixed speed: fans handed back to the card", "warn")
            elif not r and (fan_curve.status() or {}).get("mode") != "manual":
                flight.flush()
                return
        sleep(1)


def main():
    log = Log()
    try:
        lock = stability.monitor_lock()
    except OSError:
        return
    from headroom.nvml import Nvml
    from headroom.sensors import Telemetry
    nv = Nvml()
    try:
        loop(nv, Telemetry(nv), log)
    finally:
        lock.close()


try:
    main()
except Exception:  # noqa: BLE001
    with open(os.path.join(DATA_DIR, "monitor_crash.log"), "a", encoding="utf-8") as f:
        f.write(traceback.format_exc())
