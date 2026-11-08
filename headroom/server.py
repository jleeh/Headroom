"""Headroom backend: local HTTP API + the web UI (shown by Headroom.exe in a
borderless WebView2 window, same as Riffsmith / the Claude Widget).
Runs elevated (Headroom.exe has a requireAdministrator manifest)."""
import hmac
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import subprocess
import time

from . import __version__, cpu_tune, fan_curve, gpu_tune, presentmon, research, research_cli, stability, update, uplift
from .setup import Setup, remove_legacy_tasks
from .common import APP_DIR, DATA_DIR, SCAN_FILE, SETTINGS_FILE, Log, State, is_admin, load_profiles, read_json, run, script, write_json
from .nvml import CLK_GRAPHICS, CLK_MEM, Nvml
from .sensors import Telemetry

PORT = int(os.environ.get("HEADROOM_PORT") or 7871)
# Per-launch secret from Headroom.exe (an elevated process's environment can't be read by
# non-elevated programs). Every /api call must carry it, so web pages (CSRF / DNS
# rebinding) and unprivileged local programs can't drive this administrator process.
TOKEN = os.environ.pop("HEADROOM_TOKEN", "") or secrets.token_hex(32)
ALLOWED_HOSTS = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}
ORIGIN = "http://127.0.0.1:%d" % PORT
MAX_BODY = 256 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
WEB = os.path.join(APP_DIR, "web")
GPU_TASK = "Headroom GPU Apply"
PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
EXE = os.path.join(APP_DIR, "Headroom.exe")
APP_CMD = '"%s"' % EXE


def hw_names():
    sc = read_json(SCAN_FILE)
    if sc:
        m = sc.get("memory") or {}
        return {"cpu": (sc.get("cpu") or {}).get("name") or "?", "board": (sc.get("board") or {}).get("model") or "?",
                "bios": (sc.get("board") or {}).get("bios") or "?",
                "ram": "%s GB DDR%s-%s (%s DIMMs)" % (m.get("total_gb"), m.get("ddr") or "?", m.get("running_mts"), m.get("sticks"))}
    return {"cpu": "?", "board": "?", "bios": "?", "ram": "?"}


def task_exists(name):
    out = run(["schtasks", "/query", "/tn", name])
    return bool(out) and "ERROR" not in out


class Controller:
    def __init__(self):
        self.log = Log()
        self.lines = []
        self.seq = 0
        self.lock = threading.Lock()
        self.log.listeners.append(self._on_log)
        self.state = State()
        self.setup = Setup(self.log, self.reload_profiles)
        self.hw = hw_names()
        try:
            self.nv = Nvml()
            self.hw["gpu"] = self.nv.name()
        except Exception as e:  # noqa: BLE001
            self.nv = None
            self.hw["gpu"] = "NVIDIA GPU not available (%s)" % e
        self.tele = Telemetry(self.nv)
        self.tele.start()
        self.tuner = None
        self.gpu_prog = {"phase": "idle", "msg": "Idle", "frac": 0}
        self.cpu = None
        self.reload_profiles()
        self.cpu_busy = None
        self.cpu_trip = None
        self.gpu_logon = task_exists(GPU_TASK)
        self.fan_logon = task_exists(fan_curve.FAN_TASK)
        self.crash_watch = stability.tasks_installed()
        self.game = None                       # running game measurement thread
        self.game_prog = {"busy": False, "msg": ""}
        self.game_cancel = threading.Event()
        self.update_info = None                # last update check (with the signed manifest if verified)
        self.cli_info = research_cli.available()
        self._fan_check = 0
        if is_admin():
            remove_legacy_tasks(self.log)
        if fan_curve.load_config()["enabled"]:
            self.fans_ensure()
        self.log("Headroom started (%s)" % ("administrator" if is_admin() else "NOT administrator - read-only"))
        if not self.profiles:
            self.log("First start on this PC: scanning the hardware to build its profile")
            self.setup.run_scan(then_build=True)
        if self.state.get("gpu", "tuning", "in_progress"):
            self.log("An unfinished GPU tuning run was found (the PC probably crashed or froze during a test). "
                     "Start Auto OC again: the step that crashed is marked unstable and skipped.", "warn")
        if self.cpu.phase in ("search", "verify"):
            self.cpu.start_watchdog(self._on_trip)
            self.log("CPU %s in progress: thermal watchdog re-armed." % self.cpu.phase)

    def reload_profiles(self):
        """(Re)load the active profile; called at start and whenever setup rebuilds it."""
        self.profiles = load_profiles()
        self.gpu_key, self.gpu_prof = next(iter((self.profiles.get("gpu") or {}).items()), (None, None))
        self.cpu_key, self.cpu_prof = next(iter((self.profiles.get("cpu") or {}).items()), (None, None))
        if not (self.cpu and self.cpu.watch and self.cpu.watch.is_alive()):
            self.cpu = cpu_tune.CpuFlow(self.tele, self.state, self.log, self.profiles, APP_CMD)
        if hasattr(self, "setup"):
            self.hw.update({k: v for k, v in hw_names().items() if v != "?"})

    def profile_info(self):
        sc = read_json(SCAN_FILE) or {}
        g, nv = sc.get("gpu") or {}, (sc.get("gpu") or {}).get("nvidia") or {}
        gp, cp = self.gpu_prof or {}, self.cpu_prof or {}
        pw = nv.get("power") or {}
        sub = " · ".join(x for x in (g.get("board_vendor"), ("%d W max" % pw["max_w"]) if pw.get("max_w") else "",
                                     ("%d GB" % nv["vram_gb"]) if nv.get("vram_gb") else "") if x)
        return {"gpu_sub": sub, "gpu_typical": gp.get("typical") or {}, "gpu_method": gp.get("tuning_method"),
                "gpu_mem_max": (nv.get("max_clock_mhz") or {}).get("memory") or 14000,
                "gpu_results": (gp.get("community_results") or [])[:2],
                "mem_verdict": (next(iter((self.profiles.get("memory") or {}).values()), {}) or {}).get("verdict"),
                "cpu_method": cp.get("tuning_method"), "cpu_typical": cp.get("typical") or {},
                "cpu_stock_boost": (cp.get("stock") or {}).get("boost_mhz") or (sc.get("cpu") or {}).get("max_mhz") or 5000}

    def caps(self):
        sc = read_json(SCAN_FILE) or {}
        c = dict(sc.get("caps") or {})
        c["gpu_tuning"] = bool(c.get("gpu_tuning") and (self.gpu_prof or {}).get("tuning_supported", True))
        c["cpu_co"] = bool(c.get("cpu_co") and (self.cpu_prof or {}).get("co_supported", True))
        return c

    def _on_log(self, line, lvl):
        with self.lock:
            self.seq += 1
            self.lines.append((self.seq, lvl, line))
            self.lines = self.lines[-400:]

    def _on_trip(self, reason):
        self.cpu_trip = reason

    # ------------------------------------------------------------------ status
    def _fans_watchdog(self):
        """A fan process that was killed can't hand the fans back; do it for it."""
        now = time.time()
        if not self.nv or not is_admin() or now - self._fan_check < 10:
            return
        self._fan_check = now
        try:
            if stability.fans_stale(fan_curve.status(), now):
                self.nv.reset_fans()
                write_json(fan_curve.STATUS_FILE, {"t": 0, "mode": "stopped"})
                self.log("The fan curve process had stopped without handing the fans back: returned them to the card", "warn")
        except Exception as e:  # noqa: BLE001
            self.log("fan watchdog: %s" % e, "warn")

    def status(self, since=0):
        self._fans_watchdog()
        s = self.tele.get()
        g = dict(s.get("gpu") or {})
        if self.nv and g and "error" not in g:
            try:
                g["limit"] = self.nv.power_limit_w()
            except Exception:  # noqa: BLE001
                pass
        with self.lock:
            logs = [{"id": i, "lvl": lv, "t": ln} for i, lv, ln in self.lines if i > since]
        st = self.state
        gsf = (self.gpu_prof or {}).get("safety", {})
        csf = (self.cpu_prof or {}).get("safety", {})
        return {
            "admin": is_admin(), "hw": self.hw, "caps": self.caps(),
            "setup": {k: v for k, v in self.setup.status().items() if k not in ("scan",)},
            "profile": {"gpu": self.gpu_key, "cpu": self.cpu_key, "sources": (self.profiles or {}).get("_sources"),
                        "cpu_label": (self.cpu_prof or {}).get("label") or self.cpu_key,
                        "cooler": next(iter((self.profiles.get("cooler") or {}).values()), {}),
                        "cooler_name": next(iter(self.profiles.get("cooler") or {}), ""),
                        **self.profile_info()},
            "sensors": {"cpu": s.get("cpu"), "gpu": g, "gpu_x": s.get("gpu_x"), "fans": s.get("fans"),
                        "lhm_error": s.get("lhm_error")},
            "limits": {"gpu_core": gsf.get("abort_core_c", 83), "gpu_core_warn": gsf.get("warn_core_c", 78),
                       "gpu_mem": gsf.get("abort_mem_junction_c", 96), "gpu_mem_warn": gsf.get("warn_mem_junction_c", 90),
                       "cpu": csf.get("abort_tdie_c", 90), "cpu_warn": csf.get("warn_tdie_c", 85),
                       "max_core": gsf.get("max_core_offset", 500), "max_mem": gsf.get("max_mem_offset_reported_mhz", 3000)},
            "gpu": {"busy": bool(self.tuner and self.tuner.is_alive()), "progress": self.gpu_prog,
                    "applied": st.get("gpu", "applied"), "efficiency": st.get("gpu", "efficiency"),
                    "baseline": st.get("gpu", "baseline"), "result": st.get("gpu", "result"),
                    "mem_ratio": self.mem_ratio(), "logon": self.gpu_logon,
                    "expected": (self.gpu_prof or {}).get("expected_uplift_pct", {})},
            "fans": {"config": fan_curve.load_config(), "status": fan_curve.status(), "logon": self.fan_logon,
                     "emergency": [fan_curve.EMERG_CORE, fan_curve.EMERG_MEM]},
            "cpu": self.cpu_view(),
            "bench": st.get("bench", default=[]) or [],
            "version": __version__,
            "stability": stability.summary(self.state, tasks=self.crash_watch),
            "recheck": st.get("gpu", "recheck"), "reverted": st.get("gpu", "stability_reverted"),
            "games": {"results": (st.get("games", default=[]) or [])[:5], "progress": self.game_prog,
                      "mode": self.apply_settings()},
            "research": dict(research.get_settings(), estimate=research.estimate(), cli=self.cli_info),
            "update": {k: v for k, v in (self.update_info or {}).items() if k != "manifest"},
            "logs": logs, "seq": self.seq,
        }

    def mem_ratio(self):
        return (self.state.get("gpu", "result") or {}).get("mem_ratio") or \
            (self.state.get("gpu", "tuning") or {}).get("mem_ratio") or 0.5

    def cpu_view(self):
        ph = self.cpu.phase
        v = {"phase": ph, "busy": self.cpu_busy, "trip": self.cpu_trip,
             "corecycler": cpu_tune.find_corecycler(),
             "co_start": self.state.get("cpu", "search", "start", default=-25),
             "baseline": self.state.get("cpu", "baseline"), "found": self.state.get("cpu", "found"),
             "final": self.state.get("cpu", "final"), "result": self.state.get("cpu", "result"),
             "expected": (self.cpu_prof or {}).get("expected_uplift_pct", {}),
             "watchdog": bool(self.cpu.watch and self.cpu.watch.is_alive()),
             "bios1_ok": self.state.get("cpu", "bios1_ok"), "bios1_clock": self.state.get("cpu", "bios1_clock")}
        if ph == "bios1":
            v["sheet"] = cpu_tune.bios_sheet(self.cpu.board_name, self.cpu.board_p, self.cpu.cpu_p, None)
        elif ph == "search":
            try:
                v["search"] = self.cpu.search_status()
            except Exception as e:  # noqa: BLE001
                v["search"] = {"error": str(e)}
        elif ph == "bios2":
            v["sheet"] = cpu_tune.bios_sheet(self.cpu.board_name, self.cpu.board_p, self.cpu.cpu_p, v["final"])
            fin = v["final"] or [0]
            cp = self.cpu.cpu_p or {}
            v["predict"] = uplift.predict_cpu((cp.get("typical") or {}).get("boost_override_mhz") or 0, sum(fin) / len(fin),
                                              (cp.get("stock") or {}).get("boost_mhz") or 5000)
        return v

    # ------------------------------------------------------------------ GPU
    def need(self):
        if not self.nv:
            raise RuntimeError("No NVIDIA GPU found")
        if not is_admin():
            raise RuntimeError("Headroom needs administrator rights to change clocks")

    def gpu_auto(self, quick, mode="full"):
        self.need()
        if mode not in ("full", "refine", "verify"):
            raise RuntimeError("unknown mode")
        if self.tuner and self.tuner.is_alive():
            return

        def prog(ph, msg, fr):
            self.gpu_prog = {"phase": ph, "msg": msg, "frac": fr if fr is not None else self.gpu_prog.get("frac", 0)}
        self.gpu_prog = {"phase": "start", "msg": "Starting", "frac": 0}
        others = fan_curve.other_controllers()
        if others:
            self.log("%s controls the GPU fans, so Headroom won't raise them during the test. The temperature limits "
                     "still stop the test; make sure its curve ramps the fans under load." % ", ".join(others), "warn")
        else:
            fan_curve.set_boost(gpu_tune.GpuTuner.BOOST_PCT, 90)
            self.fans_ensure()
        self.tuner = gpu_tune.GpuTuner(self.nv, self.tele, self.state, self.log, self.gpu_prof, on_progress=prog, quick=quick, mode=mode)
        self.tuner.start()

    def gpu_stop(self):
        if self.tuner:
            self.tuner.stop()

    def gpu_apply(self, core, mem_ab):
        """Manual offsets, limited to this card's profile caps (never above the hard caps)."""
        self.need()
        r = self.mem_ratio()
        sf = (self.gpu_prof or {}).get("safety", {})
        core = max(0, min(int(core), int(sf.get("max_core_offset", 150))))
        mem_ab = max(0, min(int(mem_ab), int(sf.get("max_mem_offset_reported_mhz", 500))))
        core, mem = gpu_tune.clamp_offsets(core, mem_ab / r, r)
        self.nv.set_offset(CLK_GRAPHICS, core)
        self.nv.set_offset(CLK_MEM, mem)
        self.state.set("gpu", "applied", value={"core": core, "mem": mem, "manual": True})
        self.log("Manual GPU offsets applied: core %+d, memory %+d (Afterburner units)" % (core, mem_ab))

    def gpu_save_profile(self, core, mem_ab):
        """Apply these offsets and make them the saved profile (used by 'Apply
        saved' and Apply at logon). Never above what Auto OC proved stable."""
        self.need()
        res = dict(self.state.get("gpu", "result") or {})
        if not res:
            raise RuntimeError("Run Auto OC first")
        tested = res.get("tested") or {"core": res["core"], "mem_afterburner": res["mem_afterburner"]}
        if int(core) > tested["core"] or int(mem_ab) > tested["mem_afterburner"]:
            raise RuntimeError("Higher than Auto OC tested (core %+d, memory %+d)" % (tested["core"], tested["mem_afterburner"]))
        r = self.mem_ratio()
        self.gpu_apply(core, mem_ab)
        res.update(core=int(core), mem=int(mem_ab / r), mem_afterburner=int(mem_ab), tested=tested,
                   adjusted=time.strftime("%Y-%m-%d %H:%M"))
        self.state.set("gpu", "result", value=res)
        self.state.set("gpu", "applied", value={"core": int(core), "mem": int(mem_ab / r)})
        self.log("Saved GPU profile set to core %+d, memory %+d AB (Auto OC tested core %+d, memory %+d)" % (
            core, mem_ab, tested["core"], tested["mem_afterburner"]))

    def gpu_apply_saved(self):
        self.need()
        if not gpu_tune.apply_saved(self.nv, self.state, self.log):
            raise RuntimeError("No saved auto result yet")

    def gpu_stock(self):
        self.need()
        for e in self.nv.reset_all():
            self.log(e, "warn")
        self.state.set("gpu", "applied", value=None)
        self.state.set("gpu", "efficiency", value=None)
        self.log("GPU back to stock")

    def gpu_eff(self, on):
        self.need()
        if on and not self.state.get("gpu", "result"):
            raise RuntimeError("Run Auto OC first: efficiency mode uses the stable offset it finds")
        gpu_tune.efficiency_mode(self.nv, self.state, self.log, on)

    def gpu_set_logon(self, on):
        self.need()
        if on:
            if not self.state.get("gpu", "result"):
                raise RuntimeError("Run Auto OC first")
            cmd = '"%s" "%s"' % (PYW, script("headroom_apply.pyw"))
            run(["schtasks", "/create", "/f", "/rl", "highest", "/sc", "onlogon", "/tn", GPU_TASK, "/tr", cmd, "/delay", "0001:00"])
            self.log("GPU profile will be applied 1 min after logon (not after a crash).")
            if not self.crash_watch:
                self.stability_watch(True)   # an overclock that runs unattended should be watched
        else:
            run(["schtasks", "/delete", "/f", "/tn", GPU_TASK])
            self.log("Apply-at-logon removed")
        self.gpu_logon = task_exists(GPU_TASK)

    # ---- fan curve ---------------------------------------------------------
    def fans_ensure(self):
        """Start the fan process if it isn't running (detached, so the curve keeps
        working after this window closes; it stops itself when the curve is off)."""
        if fan_curve.status()["running"]:
            return
        subprocess.Popen([PYW, script("headroom_fans.pyw")], cwd=APP_DIR,
                         creationflags=0x00000008 | 0x00000200, close_fds=True)
        time.sleep(0.3)

    def fans_set(self, body):
        self.need()
        others = fan_curve.other_controllers()
        if body.get("enabled") and others:
            raise RuntimeError("%s is controlling the fans. Two fan controllers fight each other: close it or turn off its GPU "
                               "fan control, then turn Headroom's curve on." % ", ".join(others))
        c = fan_curve.save_config(body)
        if c["enabled"]:
            self.fans_ensure()
        self.log("Fan curve %s. Core %s / VRAM %s" % ("ON" if c["enabled"] else "off (card's own curve)",
                                                    c["core"], c["mem"]))
        if not c["enabled"] and self.fan_logon:
            self.fans_set_logon(False)
        return c

    def fans_set_logon(self, on):
        self.need()
        if on:
            cmd = '"%s" "%s"' % (PYW, script("headroom_fans.pyw"))
            run(["schtasks", "/create", "/f", "/rl", "highest", "/sc", "onlogon", "/tn", fan_curve.FAN_TASK, "/tr", cmd])
            self.log("Fan curve will start at logon.")
        else:
            run(["schtasks", "/delete", "/f", "/tn", fan_curve.FAN_TASK])
            self.log("Fan curve at logon removed")
        self.fan_logon = task_exists(fan_curve.FAN_TASK)

    def reset_all(self):
        if self.tuner and self.tuner.is_alive():
            self.tuner.stop()
            self.tuner.join(15)
        if self.nv and is_admin():
            self.nv.reset_all()
            self.state.set("gpu", "applied", value=None)
            self.state.set("gpu", "efficiency", value=None)
        fan_curve.clear_boost()
        if fan_curve.load_config()["enabled"]:
            fan_curve.save_config({"enabled": False})   # fan process hands back to the card
        if self.cpu.watch and self.cpu.watch.is_alive():
            cpu_tune.kill_stress(cpu_tune.find_corecycler() or "", self.log, "user pressed RESET ALL")
        self.log("RESET: GPU at stock. CPU settings live in the BIOS: set PBO / Curve Optimizer back to Auto there "
                 "(or Load Optimized Defaults, F5) to fully revert the CPU.", "warn")

    # ---- crash watch ---------------------------------------------------------
    def stability_watch(self, on):
        self.need()
        if on:
            stability.install_event_tasks(PYW, APP_DIR)
        else:
            stability.remove_event_tasks()
        self.crash_watch = stability.tasks_installed()
        self.log("Crash watch %s" % ("on: driver resets, WHEA errors and bugchecks are recorded and the overclock "
                                     "backs off on its own" if self.crash_watch else "off"))

    def stability_check(self):
        self.need()
        return {"new": stability.run_locked(stability.process, self.state, self.nv, self.log) or 0}

    # ---- per-game apply + game measurement ----------------------------------
    @staticmethod
    def apply_settings():
        s = read_json(SETTINGS_FILE, {}) or {}
        return {"mode": s.get("gpu_apply_mode", "always"), "games": s.get("games", [])}

    def set_apply_settings(self, body):
        mode = body.get("mode", "always")
        if mode not in ("always", "games"):
            raise RuntimeError("mode must be 'always' or 'games'")
        games = [g for g in (body.get("games") or []) if isinstance(g, str) and presentmon.valid_process(g)][:30]
        s = read_json(SETTINGS_FILE, {}) or {}
        s.update(gpu_apply_mode=mode, games=games)
        write_json(SETTINGS_FILE, s)
        self.log("GPU overclock applies %s" % ("always" if mode == "always" else "only while these run: " + ", ".join(games)))
        return self.apply_settings()

    def game_measure(self, body):
        self.need()
        if self.game and self.game.is_alive():
            raise RuntimeError("A measurement is already running")
        proc = str(body.get("process") or "")
        if not presentmon.valid_process(proc):
            raise RuntimeError("Pick the game's .exe from the list")
        self.game_cancel.clear()

        def prog(phase, msg):
            self.game_prog = {"busy": True, "msg": msg}

        def go():
            self.game_prog = {"busy": True, "msg": "Starting PresentMon"}
            try:
                r = presentmon.measure_game(self.nv, self.state, self.log, proc, int(body.get("seconds", 20)),
                                            int(body.get("rounds", 3)), progress=prog, cancel=self.game_cancel)
                self.game_prog = {"busy": False, "msg": r.get("verdict", "Done")}
            except Exception as e:  # noqa: BLE001
                self.game_prog = {"busy": False, "msg": str(e), "error": True}
                self.log("Game measurement: %s" % e, "error")
        self.game = threading.Thread(target=go, daemon=True)
        self.game.start()

    # ---- research settings + updates ---------------------------------------
    def research_settings(self, body):
        d = {}
        if "backend" in body:
            d["research_backend"] = body["backend"]
        if "cap" in body:
            d["research_cap_usd_month"] = body["cap"]
        out = research.set_research_settings(d)
        self.cli_info = research_cli.available()
        return out

    def update_check(self):
        self.update_info = update.check(__version__)
        return {k: v for k, v in self.update_info.items() if k != "manifest"}

    def update_install(self):
        """Download the installer named by the signed manifest, check its SHA-256, run it."""
        self.need()
        info = self.update_info or {}
        if not (info.get("available") and info.get("manifest")):
            raise RuntimeError("Check for updates first")
        path = update.download_and_verify(info["manifest"], os.path.join(DATA_DIR, "updates"))
        self.log("Update %s downloaded and verified; starting the installer" % info.get("latest"))
        os.startfile(path)

    # ------------------------------------------------------------------ CPU
    def _bg(self, label, fn):
        if self.cpu_busy:
            raise RuntimeError("Busy: " + self.cpu_busy)
        self.cpu_busy = label

        def wrap():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.log("CPU: %s" % e, "error")
            finally:
                self.cpu_busy = None
        threading.Thread(target=wrap, daemon=True).start()

    def cpu_preflight(self):
        return self.cpu.preflight()

    def cpu_action(self, body):
        ph = self.cpu.phase
        prog = lambda p, m: self.log("[CPU %s] %s" % (p, m))  # noqa: E731
        self.cpu_trip = None
        if ph == "baseline":
            self._bg("Benchmarking", lambda: self.cpu.baseline(prog))
        elif ph == "bios1":
            def chk():
                need = cpu_tune.boost_check_mhz(self.cpu.cpu_p)
                clk = self.cpu.check_bios1(prog)
                ok = need is None or clk >= need
                self.state.set("cpu", "bios1_ok", value=ok)
                self.log("[CPU] peak %.0f MHz: boost override %s" % (clk, "active" if ok else "NOT active yet"),
                         "info" if ok else "warn")
            self._bg("Checking boost", chk)
        elif ph == "search":
            self.cpu.finish_search()
        elif ph == "bios2":
            self.cpu.set_phase("verify")
            self.cpu.start_verify()
            self.cpu.start_watchdog(self._on_trip)
        elif ph in ("verify", "done"):
            self._bg("Measuring", lambda: self.cpu.finish_verify(prog))

    def cpu_start_search(self, start):
        self.cpu.start_search(max(-50, min(0, int(start))))
        self.cpu.start_watchdog(self._on_trip)

    def cpu_install(self):
        self._bg("Installing CoreCycler", lambda: cpu_tune.install_corecycler(self.log))

    def bench(self, rows):
        """[name, stock score, tuned score] rows typed in the UI; stored as plain values only."""
        clean = []
        for r in (rows if isinstance(rows, list) else [])[:50]:
            try:
                clean.append([str(r[0])[:60], float(r[1]), float(r[2])])
            except (TypeError, ValueError, IndexError):
                continue
        self.state.set("bench", value=clean)

    def shutdown_ok(self):
        return not (self.tuner and self.tuner.is_alive())

    def shutdown(self):
        self.game_cancel.set()
        self.setup.cancel()
        if self.tuner and self.tuner.is_alive():
            self.tuner.stop()
            self.tuner.join(15)
        self.tele.stop()


class Handler(BaseHTTPRequestHandler):
    ctl = None
    server_ref = None
    token = TOKEN
    server_version = "Headroom"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        """DNS rebinding: a hostile name that resolves to 127.0.0.1 still sends its own Host.
        A browser request from another site carries its Origin: refused too."""
        origin = self.headers.get("Origin")
        return (self.headers.get("Host") or "").lower() in ALLOWED_HOSTS and (origin is None or origin == ORIGIN)

    def _authed(self):
        return hmac.compare_digest((self.headers.get("X-Headroom-Token") or "").encode(), self.token.encode())

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        path, _, q = self.path.partition("?")
        if path == "/api/ping":   # liveness for Headroom.exe (its pid tells it whether it's the backend it started)
            return self._send(200, {"ok": True, "pid": os.getpid()})
        if path.startswith("/api/"):
            if not self._authed():
                return self._send(403, {"error": "forbidden"})
            if path == "/api/status":
                try:
                    since = int(dict(x.split("=", 1) for x in q.split("&") if "=" in x).get("since", 0) or 0)
                except ValueError:
                    since = 0
                return self._send(200, self.ctl.status(since))
            if path == "/api/profiles":
                return self._send(200, self.ctl.profiles)
            if path == "/api/setup":
                return self._send(200, self.ctl.setup.status())
            return self._send(404, {"error": "not found"})
        if path in ("/", "/index.html"):
            path = "/index.html"
        f = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not f.startswith(WEB + os.sep) or not os.path.isfile(f):
            return self._send(404, {"error": "not found"})
        ext = f.rsplit(".", 1)[-1].lower()
        ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css", "png": "image/png",
                 "svg": "image/svg+xml", "ico": "image/x-icon"}.get(ext, "application/octet-stream")
        with open(f, "rb") as fh:
            data = fh.read()
        extra = {}
        if ext == "html":
            # only the page's own <script> blocks run: injected markup can't execute script
            nonce = secrets.token_urlsafe(18)
            data = data.replace(b"<script>", ('<script nonce="%s">' % nonce).encode())
            extra["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'nonce-%s'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % nonce)
        self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text") else ""), extra)

    def _body(self):
        """JSON only: a cross-site 'simple' request (text/plain, form) is refused outright."""
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            raise ValueError("Content-Type must be application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            raise ValueError("request too large")
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("JSON object expected")
        return body

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if not self._authed():
            return self._send(403, {"error": "forbidden"})
        try:
            body = self._body()
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        if self.path == "/api/shutdown":
            self._send(200, {"ok": True})
            stop_server(self.ctl, self.server_ref)
            return
        fn = routes(self.ctl, body).get(self.path)
        if not fn:
            return self._send(404, {"error": "unknown"})
        try:
            r = fn()
            self._send(200, r if isinstance(r, dict) else {"ok": True, "result": r})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": str(e)})


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def routes(c, body):
    return {
        "/api/gpu/auto": lambda: c.gpu_auto(bool(body.get("quick")), str(body.get("mode", "full"))),
        "/api/gpu/recheck": lambda: c.gpu_auto(True, "verify"),
        "/api/stability/watch": lambda: c.stability_watch(bool(body.get("on"))),
        "/api/stability/config": lambda: stability.set_config(body),
        "/api/stability/check": c.stability_check,
        "/api/games/processes": lambda: {"processes": presentmon.running_processes()},
        "/api/games/measure": lambda: c.game_measure(body),
        "/api/games/stop": c.game_cancel.set,
        "/api/games/apply_mode": lambda: c.set_apply_settings(body),
        "/api/research/settings": lambda: c.research_settings(body),
        "/api/setup/research_cancel": c.setup.cancel,
        "/api/update/check": c.update_check,
        "/api/update/install": c.update_install,
        "/api/gpu/stop": c.gpu_stop,
        "/api/gpu/apply": lambda: c.gpu_apply(_int(body.get("core")), _int(body.get("mem"))),
        "/api/gpu/apply_saved": c.gpu_apply_saved,
        "/api/gpu/save_profile": lambda: c.gpu_save_profile(_int(body.get("core")), _int(body.get("mem"))),
        "/api/gpu/stock": c.gpu_stock,
        "/api/gpu/efficiency": lambda: c.gpu_eff(bool(body.get("on"))),
        "/api/gpu/logon": lambda: c.gpu_set_logon(bool(body.get("on"))),
        "/api/fans/set": lambda: c.fans_set(body),
        "/api/fans/default": lambda: c.fans_set({"core": fan_curve.DEFAULT["core"], "mem": fan_curve.DEFAULT["mem"]}),
        "/api/fans/logon": lambda: c.fans_set_logon(bool(body.get("on"))),
        "/api/reset_all": c.reset_all,
        "/api/setup/scan": lambda: c.setup.run_scan(True),
        "/api/setup/rebuild": c.setup.rebuild,
        "/api/setup/research": c.setup.run_research,
        "/api/setup/key": lambda: c.setup.set_key(str(body.get("key") or "")),
        "/api/setup/cooling": lambda: c.setup.set_cooling(body),
        "/api/setup/lhm": c.setup.install_lhm,
        "/api/cpu/preflight": c.cpu_preflight,
        "/api/cpu/action": lambda: c.cpu_action(body),
        "/api/cpu/start_search": lambda: c.cpu_start_search(_int(body.get("start"), -25)),
        "/api/cpu/install": c.cpu_install,
        "/api/cpu/reset": c.cpu.reset,
        "/api/bench": lambda: c.bench(body.get("rows", [])),
        "/api/openfolder": lambda: os.startfile(DATA_DIR),
        "/api/can_close": lambda: {"ok": c.shutdown_ok(), "cpu_watch": bool(c.cpu.watch and c.cpu.watch.is_alive())},
    }


_stopping = threading.Lock()


def stop_server(ctl, srv):
    if not _stopping.acquire(blocking=False):
        return
    ctl.shutdown()
    threading.Thread(target=srv.shutdown, daemon=True).start()


def watch_shutdown_event(ctl, srv):
    """The installer/uninstaller can't know the token. It signals this named event
    instead; objects made by an elevated process can't be opened for signalling by
    non-elevated ones (integrity level), so this isn't a way around the token."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
    except (ImportError, AttributeError):
        return
    k32.CreateEventW.restype = ctypes.c_void_p
    h = k32.CreateEventW(None, True, False, "Local\\Headroom.Shutdown")
    if not h:
        return

    def loop():
        k32.WaitForSingleObject(ctypes.c_void_p(h), 0xFFFFFFFF)
        stop_server(ctl, srv)
    threading.Thread(target=loop, daemon=True).start()


class Exclusive(ThreadingHTTPServer):
    """Windows lets SO_REUSEADDR sockets share a port, which would allow two
    backends at once. Claim the port exclusively instead."""
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    srv = Exclusive(("127.0.0.1", PORT), Handler)  # bind first: a second copy fails here, before touching hardware
    Handler.ctl = Controller()
    Handler.server_ref = srv
    watch_shutdown_event(Handler.ctl, srv)
    srv.serve_forever()

