"""Automatic GPU overclock search.

Order: preflight -> baseline at stock -> core offset search -> memory offset
search -> combined soak -> measure -> apply with a safety margin.

Safety (on top of the card's own 300 W firmware cap, which offsets can't lift):
  * the watchdog checks core and memory-junction temperature every tick and
    resets everything to stock the instant a limit is crossed;
  * every result is checked bit for bit against the golden run done at stock;
  * NVIDIA driver resets (TDR / Xid) in the event log count as a failure;
  * the step being tested is saved to disk BEFORE it is applied, so if the
    PC freezes or reboots, the next launch marks that step as failed and resumes;
  * offsets live in the driver only: a reboot always returns to stock unless
    you turn on "apply at logon";
  * hard caps from the research profile (+500 core, +3000 memory) are never exceeded.
"""
import threading
import time

from . import fan_curve, uplift
from .common import is_admin, process_running, tdr_events_since
from .cuda_stress import Cuda, CudaError, CoreTest, MemTest
from .nvml import CLK_GRAPHICS, CLK_MEM, NvmlError
from .profiles import CAP


class Abort(Exception):
    pass


class GpuTuner(threading.Thread):
    CORE_START, CORE_STEP, CORE_FINE = 150, 50, 15
    MEM_STEP = 250
    CORE_MARGIN, MEM_MARGIN = 30, 250
    STEP_SECONDS = 40
    SOAK_SECONDS = 180

    def __init__(self, nv, tele, state, log, profile, on_progress=None, quick=False, mode="full"):
        super().__init__(daemon=True)
        self.nv, self.tele, self.state, self.log = nv, tele, state, log
        self.prof = profile or {}
        sf = self.prof.get("safety", {})
        self.lim_core = sf.get("abort_core_c", 83)
        self.lim_mem = sf.get("abort_mem_junction_c", 96)
        # re-clamped here too: profile.json lives in a user-writable folder
        self.lim_core = min(self.lim_core, CAP["gpu_abort_core_c"][1])
        self.lim_mem = min(self.lim_mem, CAP["gpu_abort_mem_c"][1])
        self.max_core = max(0, min(sf.get("max_core_offset", 150), CAP["gpu_max_core"]))
        self.max_mem_rep = max(0, min(sf.get("max_mem_offset_reported_mhz", 500), CAP["gpu_max_mem_ab"]))
        self.on_progress = on_progress or (lambda *a: None)
        self.stop_ev = threading.Event()
        self.cu = None
        self.result = None
        self.error = None
        self.mode = mode
        if quick:
            self.STEP_SECONDS, self.SOAK_SECONDS = 15, 45

    # ------------------------------------------------------------------ utils
    def stop(self):
        self.stop_ev.set()

    def _guard(self, *_):
        if self.stop_ev.is_set():
            raise Abort("stopped by user")
        s = self.tele.get()
        g = s.get("gpu", {})
        x = s.get("gpu_x", {}) or {}
        t = g.get("temp")
        if t is not None and t >= self.lim_core:
            raise Abort("GPU core %d C >= limit %d C" % (t, self.lim_core))
        mj = x.get("mem_junction")
        if mj is not None and mj >= self.lim_mem:
            raise Abort("GPU memory junction %.0f C >= limit %d C" % (mj, self.lim_mem))

    def _set(self, core=None, mem=None):
        if core is not None:
            self.nv.set_offset(CLK_GRAPHICS, core)
        if mem is not None:
            self.nv.set_offset(CLK_MEM, mem)
        time.sleep(0.5)

    def _stock(self):
        errs = self.nv.reset_all()
        for e in errs:
            self.log("reset: " + e, "warn")

    def _progress(self, phase, msg, frac=None):
        self.on_progress(phase, msg, frac)
        self.log("[GPU %s] %s" % (phase, msg))

    def _sampler(self, min_util=90):
        """Collects telemetry while a test runs; returns (start, finish)."""
        samples = []
        stop = threading.Event()

        def loop():
            while not stop.wait(1.0):
                g = self.tele.get().get("gpu", {})
                if g.get("util", 0) >= min_util:
                    samples.append(g)
        th = threading.Thread(target=loop, daemon=True)
        th.start()

        def finish():
            stop.set()
            th.join(2)
            if not samples:
                return {}
            n = len(samples)
            return {"eff_clk": sum(s["clk"] for s in samples) / n, "max_clk": max(s["clk"] for s in samples),
                    "mem_clk": sum(s["mem_clk"] for s in samples) / n,
                    "power": sum(s["power"] for s in samples) / n,
                    "temp": max(s["temp"] for s in samples),
                    "throttle": sorted({r for s in samples for r in s.get("throttle", [])})}
        return finish

    def _core_run(self, seconds, test=None):
        fin = self._sampler(min_util=50 if test is not None else 90)
        t0 = time.time()
        try:
            r = (test or self.core).run(seconds, self.stop_ev.is_set, self._guard)
        except CudaError as e:
            fin()
            return {"ok": False, "why": "GPU fault (%s)" % e, "fault": True}
        tel = fin()
        ev = tdr_events_since(t0)
        r.update(tel)
        if ev:
            r.update(ok=False, why="driver reset: " + "; ".join(ev[:2]), fault=True)
        elif not r["ok"]:
            r["why"] = "%d wrong results out of %d" % (r["errors"], r["launches"])
        return r

    def _mem_run(self, seconds):
        fin = self._sampler()
        t0 = time.time()
        try:
            r = self.mem.run(seconds, self.stop_ev.is_set, self._guard)
        except CudaError as e:
            fin()
            return {"ok": False, "why": "GPU fault (%s)" % e, "fault": True}
        r.update(fin())
        ev = tdr_events_since(t0)
        if ev:
            r.update(ok=False, why="driver reset: " + "; ".join(ev[:2]), fault=True)
        elif not r["ok"]:
            r["why"] = "%d VRAM errors" % r["errors"]
        return r

    def _recover(self):
        self._stock()
        time.sleep(3)
        self.cu.reopen()
        self._make_tests(recal=False)

    def _make_tests(self, recal=True):
        iters = getattr(self, "core", None) and self.core.iters
        ref = getattr(self, "core", None) and self.core.ref
        self.core = CoreTest(self.cu)
        if recal or not iters:
            self.core.calibrate()
        else:
            self.core.iters = iters
        if ref and not recal:
            self.core.ref = ref
        lt = getattr(self, "light", None)
        self.light = CoreTest(self.cu, blocks=self.cu.sms)
        if lt is not None and not recal:
            self.light.iters, self.light.ref = lt.iters, lt.ref
        else:
            self.light.calibrate()
        self.mem = MemTest(self.cu, fraction=0.75)

    def _mark(self, **kw):
        cur = self.state.get("gpu", "tuning", default={}) or {}
        cur.update(kw)
        self.state.set("gpu", "tuning", value=cur)

    # ------------------------------------------------------------------ phases
    def preflight(self):
        if not is_admin():
            raise Abort("Headroom must run as administrator to change GPU clocks (use Headroom.cmd).")
        clash = process_running("MSIAfterburner.exe", "EVGAPrecisionX1.exe", "ASUSGPUTweakIII.exe")
        if clash:
            raise Abort("Close %s first: it re-applies its own clocks and would fight Headroom." % ", ".join(clash))
        s = self.tele.sample()
        t = s.get("gpu", {}).get("temp", 0)
        if t > 60:
            raise Abort("GPU is already %d C at idle: let it cool or check the fans first." % t)
        if "lhm_error" in s:
            self.log("LibreHardwareMonitor not reachable: memory junction temp won't be watched (%s)" % s["lhm_error"], "warn")
        # A step that was being tested when the PC last died counts as failed.
        tu = self.state.get("gpu", "tuning", default={}) or {}
        if tu.get("in_progress") and tu.get("testing"):
            kind, val = tu["testing"]
            self.log("Last run ended during %s %+d (crash/freeze?) - treating it as unstable" % (kind, val), "warn")
            fails = tu.setdefault("crashed", [])
            fails.append([kind, val])
            self._mark(crashed=fails, testing=None)

    def baseline(self):
        self._progress("baseline", "Resetting to stock and compiling stress kernels")
        self._stock()
        self.cu = Cuda()
        self._make_tests()
        self._progress("baseline", "Recording golden results at stock clocks")
        self.core.golden()
        self.light.golden()
        self._progress("baseline", "Stock compute run (%ds)" % self.STEP_SECONDS)
        c = self._core_run(self.STEP_SECONDS)
        if not c["ok"]:
            raise Abort("Failed at STOCK clocks (%s): the GPU or driver has a problem, not safe to overclock." % c.get("why"))
        self._progress("baseline", "Stock VRAM run over %.1f GiB" % self.mem.gib)
        m = self._mem_run(self.STEP_SECONDS / 2)
        if not m["ok"]:
            raise Abort("VRAM errors at STOCK (%s)." % m.get("why"))
        b = {"eff_clk": c.get("eff_clk"), "rate": c["rate"], "power": c.get("power"),
             "temp": c.get("temp"), "gbps": m["gbps"], "mem_clk": m.get("mem_clk"),
             "throttle": c.get("throttle"), "when": time.strftime("%Y-%m-%d %H:%M")}
        self.state.set("gpu", "baseline", value=b)
        self._progress("baseline", "Stock: %.0f MHz under load, %.0f W, %d C, %.0f GB/s" %
                       (b["eff_clk"] or 0, b["power"] or 0, b["temp"] or 0, b["gbps"]))
        return b

    def _crashed(self, kind):
        return [v for k, v in (self.state.get("gpu", "tuning", "crashed", default=[]) or []) if k == kind]

    def search_core(self):
        crashed = self._crashed("core")
        ceiling = min([self.max_core + 1] + crashed)
        best = 0
        off = self.CORE_START
        step = self.CORE_STEP
        fine = False
        while off < ceiling:
            self._mark(in_progress=True, phase="core", testing=["core", off])
            self._set(core=off)
            self._progress("core", "Testing core %+d MHz" % off, off / self.max_core)
            r = self._core_run(self.STEP_SECONDS)
            self._mark(testing=None)
            if r["ok"]:
                best = off
                self._mark(core_best=best)
                self._progress("core", "core %+d PASS (%.0f MHz under load, %.0f W, %d C)" %
                               (off, r.get("eff_clk", 0), r.get("power", 0), r.get("temp", 0)))
                off += self.CORE_FINE if fine else step
            else:
                self._progress("core", "core %+d FAIL: %s" % (off, r.get("why")))
                if r.get("fault"):
                    self._recover()
                if fine:
                    break
                fine = True
                ceiling = off
                off = best + self.CORE_FINE
        final = max(0, best - self.CORE_MARGIN)
        self._set(core=final)
        self._mark(core_final=final)
        self._progress("core", "Core best %+d, using %+d (-%d MHz margin for games)" % (best, final, self.CORE_MARGIN))
        return best, final

    LIGHT_SECONDS = 60
    LIGHT_STEP = 25

    def light_check(self, core):
        """Low-power test: only 1 block per SM, so the card is far from its
        300 W cap and boosts to the TOP of its voltage/frequency curve - the
        clocks that light games reach and the full-load test never does. Big
        offsets usually fail here first. Steps the core down until it passes."""
        while core > 0:
            self._mark(in_progress=True, phase="light", testing=["core", core])
            self._set(core=core, mem=0)
            self._progress("light", "Light-load check at core %+d (max boost clocks)" % core)
            r = self._core_run(self.LIGHT_SECONDS, test=self.light)
            self._mark(testing=None)
            if r["ok"]:
                self._progress("light", "core %+d PASS at light load (peak %.0f MHz, %.0f W)" %
                               (core, r.get("max_clk", 0), r.get("power", 0)))
                break
            self._progress("light", "core %+d FAIL at light load: %s" % (core, r.get("why")))
            if r.get("fault"):
                self._recover()
            core -= self.LIGHT_STEP
        core = max(0, core)
        self._set(core=core)
        self._mark(core_final=core, light_ok=core)
        return core

    def _cool(self, target, timeout=240):
        t0 = time.time()
        while time.time() - t0 < timeout and not self.stop_ev.is_set():
            mj = (self.tele.get().get("gpu_x") or {}).get("mem_junction")
            if mj is None or mj <= target:
                return
            time.sleep(3)

    def _mem_unit(self, core):
        """Work out how NVML's memory offset maps to the reported memory clock
        (Afterburner's slider = reported-clock delta)."""
        self._set(core=core, mem=0)
        c0 = self._mem_run(4).get("mem_clk")
        self._set(mem=500)
        c1 = self._mem_run(4).get("mem_clk")
        self._set(mem=0)
        if c0 and c1 and c1 > c0:
            ratio = (c1 - c0) / 500.0
        else:
            ratio = 1.0
        self.log("memory offset unit: NVML +500 -> reported +%.0f MHz (%.2fx)" % (500 * ratio, ratio))
        return ratio

    def search_mem(self, core):
        ratio = self._mem_unit(core)
        max_nvml = int(self.max_mem_rep / ratio)
        step = int(self.MEM_STEP / ratio)
        crashed = self._crashed("mem")
        ceiling = min([max_nvml + 1] + crashed)
        # Reference bandwidth measured exactly like the steps (same core offset,
        # same duration): a stock figure from a shorter run at stock core is
        # not comparable (a 2-3% difference stopped the search at +0 once).
        self._set(core=core, mem=0)
        self._progress("mem", "Measuring reference bandwidth at memory +0")
        ref = self._mem_run(self.STEP_SECONDS)
        base_bw = ref.get("gbps") or self.state.get("gpu", "baseline", "gbps", default=0)
        self._progress("mem", "Reference %.0f GB/s" % base_bw)
        best, best_bw = 0, base_bw
        flat = 0
        off = step
        while off < ceiling:
            self._mark(in_progress=True, phase="mem", testing=["mem", off])
            self._set(mem=off)
            self._progress("mem", "Testing memory %+d (Afterburner %+d)" % (off, off * ratio), off / max_nvml)
            try:
                r = self._mem_run(self.STEP_SECONDS)
            except Abort as e:
                if "memory junction" not in str(e):
                    raise
                # VRAM too hot under this (much harsher than gaming) test: end the
                # memory search at the last passing step instead of failing the run.
                self._mark(testing=None)
                self._set(mem=max(0, best - int(self.MEM_MARGIN / ratio)))
                self._progress("mem", "%s - ending memory search at AB %+d and cooling down" % (e, best * ratio))
                self._cool(80)
                break
            self._mark(testing=None)
            if not r["ok"]:
                self._progress("mem", "memory AB %+d FAIL: %s" % (off * ratio, r.get("why")))
                if r.get("fault"):
                    self._recover()
                break
            # GDDR7 EDC: past the edge the clock rises but bandwidth stalls or
            # falls because transfers are being retried. Keep the offset with
            # the best measured bandwidth; stop after 2 steps with no gain or
            # on a clear drop. (Bandwidth readings are noisy, about +-1%.)
            if r["gbps"] > best_bw * 1.002:
                best, best_bw, flat = off, r["gbps"], 0
                self._mark(mem_best=best)
                self._progress("mem", "memory AB %+d PASS (%.0f GB/s, new best)" % (off * ratio, r["gbps"]))
            else:
                flat += 1
                self._progress("mem", "memory AB %+d PASS but no bandwidth gain (%.0f GB/s)" % (off * ratio, r["gbps"]))
                if flat >= 2 or r["gbps"] < best_bw * 0.985:
                    self._progress("mem", "bandwidth stopped scaling (error retries): stopping")
                    break
            off += step
        final = max(0, best - int(self.MEM_MARGIN / ratio))
        self._set(mem=final)
        self._mark(mem_final=final, mem_ratio=ratio)
        self._progress("mem", "Memory best %+d, using %+d" % (best, final))
        return best, final, ratio

    def soak(self, core, mem):
        self._set(core=core, mem=mem)
        t_end = time.time() + self.SOAK_SECONDS
        n = 0
        while time.time() < t_end:
            self._progress("soak", "Combined soak, %ds left" % (t_end - time.time()),
                           1 - (t_end - time.time()) / self.SOAK_SECONDS)
            # blame the part under test: a freeze during a VRAM run must not mark the core offset unstable
            self._mark(in_progress=True, phase="soak", testing=["core", core] if n % 2 == 0 else ["mem", mem])
            try:
                r = self._core_run(30) if n % 2 == 0 else self._mem_run(30)
            except Abort as e:
                if "memory junction" not in str(e):
                    raise
                self._mark(testing=None)
                self._cool(80)
                return False, str(e)
            if not r["ok"]:
                self._mark(testing=None)
                return False, r.get("why")
            n += 1
        self._mark(testing=None)
        return True, ""

    def measure(self):
        c = self._core_run(self.STEP_SECONDS)
        m = self._mem_run(self.STEP_SECONDS / 2)
        return {"eff_clk": c.get("eff_clk"), "rate": c.get("rate"), "power": c.get("power"),
                "temp": c.get("temp"), "gbps": m.get("gbps"), "ok": c["ok"] and m["ok"]}

    # ------------------------------------------------------------------ main
    BOOST_PCT = 85

    def _boost_loop(self):
        # keeps the fans at >= BOOST_PCT while the tuner runs; the boost file
        # expires 90 s after the last refresh if this process dies
        while not self._boost_stop.is_set():
            try:
                fan_curve.set_boost(self.BOOST_PCT, 90)
            except OSError:
                pass
            self._boost_stop.wait(30)

    def run(self):
        self._boost_stop = threading.Event()
        threading.Thread(target=self._boost_loop, daemon=True).start()
        try:
            self.preflight()
            if self.mode == "verify":
                return self.verify()
            self._mark(in_progress=True, started=time.strftime("%Y-%m-%d %H:%M"))
            base = self.baseline()
            if self.mode == "refine":
                core = (self.state.get("gpu", "tuning", "core_final") or
                        (self.state.get("gpu", "result") or {}).get("core") or 0)
                self._progress("core", "Refine: keeping the core result %+d from the last run" % core)
            else:
                _, core = self.search_core()
            core = self.light_check(core)
            _, mem, ratio = self.search_mem(core)
            ok, why = self.soak(core, mem)
            tries = 0
            while not ok and tries < 3:
                tries += 1
                core = max(0, core - 30)
                mem = max(0, mem - int(250 / ratio))
                self._progress("soak", "Soak failed (%s): backing off to core %+d / mem %+d" % (why, core, mem))
                self._recover()
                ok, why = self.soak(core, mem)
            if not ok:
                raise Abort("Could not find a stable combined setting; left at stock.")
            self._progress("measure", "Measuring tuned performance")
            tuned = self.measure()
            up = uplift.gpu(base, tuned)
            self.result = {"core": core, "mem": mem, "mem_ratio": ratio,
                           "mem_afterburner": int(mem * ratio), "tuned": tuned, "uplift": up,
                           "when": time.strftime("%Y-%m-%d %H:%M"), **self._versions()}
            self.state.set("gpu", "result", value=self.result)
            self._fresh_result()
            self._mark(in_progress=False, testing=None, crashed=[])
            self.state.set("gpu", "applied", value={"core": core, "mem": mem})
            m = up["measured"]
            self._progress("done", "DONE core %+d / mem %+d (AB %+d). Measured: clock %+.1f%%, compute %+.1f%%, "
                           "VRAM %+.1f%%. Estimated games: %+.1f%% (%.1f to %.1f%%)" % (
                               core, mem, mem * ratio, m["core_clock_pct"], m["compute_pct"],
                               m["mem_bandwidth_pct"], up["estimated_games_pct"], *up["estimated_range_pct"]), 1.0)
        except Abort as e:
            self.error = str(e)
            self._progress("abort", str(e))
            self._safe_exit()
        except (NvmlError, CudaError, RuntimeError) as e:
            self.error = str(e)
            self._progress("error", str(e))
            self._safe_exit()
        finally:
            self._boost_stop.set()
            fan_curve.clear_boost()
            try:
                if self.cu:
                    self.cu.close()
            except Exception:  # noqa: BLE001
                pass

    def _versions(self):
        """Driver and VBIOS the result was proven on: a change means re-check before applying."""
        out = {}
        for k in ("driver", "vbios"):
            try:
                out[k] = getattr(self.nv, k)()
            except Exception:  # noqa: BLE001
                pass
        return out

    def _fresh_result(self):
        for k in ("recheck", "stability_reverted", "idle_ref"):
            self.state.set("gpu", k, value=None)

    def verify(self):
        """Quick re-check of the saved result (after a driver update, a hot summer, or
        a crash back-off): stock golden run, then the combined soak at the saved
        offsets, backing off on failure. Much shorter than a full search."""
        res = dict(self.state.get("gpu", "result") or {})
        if not res:
            raise Abort("Nothing to re-check: run Auto OC first")
        self._mark(in_progress=True, started=time.strftime("%Y-%m-%d %H:%M"))
        self.baseline()
        core, mem, ratio = res["core"], res["mem"], res.get("mem_ratio") or 0.5
        self._progress("soak", "Re-checking the saved result: core %+d / memory AB %+d" % (core, mem * ratio))
        ok, why = self.soak(core, mem)
        tries = 0
        while not ok and tries < 3:
            tries += 1
            core, mem = max(0, core - 15), max(0, mem - int(250 / ratio))
            self._progress("soak", "Re-check failed (%s): backing off to core %+d / mem %+d" % (why, core, mem))
            self._recover()
            ok, why = self.soak(core, mem)
        if not ok:
            raise Abort("The saved result is no longer stable; left at stock. Run Auto OC again.")
        res.update(core=core, mem=mem, mem_afterburner=int(mem * ratio), verified=time.strftime("%Y-%m-%d %H:%M"),
                   **self._versions())
        self.state.set("gpu", "result", value=res)
        self._fresh_result()
        self._mark(in_progress=False, testing=None, crashed=[])
        self.state.set("gpu", "applied", value={"core": core, "mem": mem})
        self._progress("done", "Re-check passed: core %+d / memory AB %+d%s" % (
            core, mem * ratio, "" if tries == 0 else " (backed off %d step%s)" % (tries, "s" if tries > 1 else "")), 1.0)

    def _safe_exit(self):
        self._stock()
        self.state.set("gpu", "applied", value=None)
        self._mark(in_progress=False, testing=None)


def clamp_offsets(core, mem, ratio):
    """Hard caps for anything about to be written to the card (state.json and
    API input are not trusted): core 0..CAP MHz, memory 0..CAP Afterburner units."""
    ratio = ratio if isinstance(ratio, (int, float)) and ratio > 0 else 0.5
    core = max(0, min(int(core or 0), CAP["gpu_max_core"]))
    mem = max(0, min(int(mem or 0), int(CAP["gpu_max_mem_ab"] / ratio)))
    return core, mem


def apply_saved(nv, state, log):
    r = state.get("gpu", "result")
    if not r:
        return False
    core, mem = clamp_offsets(r.get("core"), r.get("mem"), r.get("mem_ratio"))
    nv.set_offset(CLK_GRAPHICS, core)
    nv.set_offset(CLK_MEM, mem)
    state.set("gpu", "applied", value={"core": core, "mem": mem})
    log("Applied saved GPU profile: core %+d, mem %+d" % (core, mem))
    return True


def efficiency_mode(nv, state, log, on):
    """Undervolt-style: keep the tuned offset but cap the clock at the stock
    loaded clock. Same performance as stock, at a lower point on the voltage
    curve, so less power and heat."""
    if not on:
        nv.reset_gpu_clocks()
        state.set("gpu", "efficiency", value=None)
        log("Efficiency mode off")
        return None
    b = state.get("gpu", "baseline") or {}
    r = state.get("gpu", "result") or {}
    cap = int(b.get("eff_clk") or 2800)
    nv.set_offset(CLK_GRAPHICS, clamp_offsets(r.get("core", 0), 0, 1)[0])
    nv.lock_gpu_clocks(210, cap)
    state.set("gpu", "efficiency", value={"cap": cap})
    log("Efficiency mode: clock capped at %d MHz with core offset %+d" % (cap, r.get("core", 0)))
    return cap
