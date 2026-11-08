"""Run at logon by the 'Headroom GPU Apply' task. Applies the saved GPU
profile, but NOT if the last shutdown was a crash / power loss / bugcheck, if
stability monitoring reverted it, or if the driver changed since tuning: then
the PC boots at stock and the reason is logged. Afterwards it starts
headroom_monitor.pyw (flight recorder, per-game profiles, re-check flags)."""
import ctypes
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo / install root
from headroom import stability  # noqa: E402
from headroom.common import SETTINGS_FILE, Log, State, read_json, run  # noqa: E402
from headroom.nvml import Nvml  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
log = Log()
state = State()
try:
    nv = Nvml()
except Exception as e:  # noqa: BLE001
    log("Logon apply failed: %s" % e, "error")
    sys.exit(0)

# attribute the crash (bugcheck code / minidump -> GPU or CPU) and let the handler back off first
try:
    stability.run_locked(stability.process, state, nv, log, driver=nv.driver())
    state = State()          # the handler may have changed the saved result
except Exception as e:  # noqa: BLE001
    log("Crash attribution failed: %s" % e, "error")

up_ms = ctypes.windll.kernel32.GetTickCount64()
q = ("*[System[((Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=41) or "
     "(Provider[@Name='Microsoft-Windows-WER-SystemErrorReporting'] and EventID=1001) or "
     "(Provider[@Name='EventLog'] and EventID=6008)) and TimeCreated[timediff(@SystemTime) <= %d]]]" % (up_ms + 120000))
crash = "Event[" in run(["wevtutil", "qe", "System", "/q:" + q, "/c:1", "/f:text"])
try:
    result = state.get("gpu", "result")
    mode = (read_json(SETTINGS_FILE, {}) or {}).get("gpu_apply_mode", "always")
    why = stability.driver_change(result, nv) if result else None
    if why:
        state.set("gpu", "recheck", value={"due": True, "reason": why})
        log("Logon apply SKIPPED: %s. Re-run Auto OC to re-check the overclock." % why, "warn")
    elif crash:
        log("Logon apply SKIPPED: the last shutdown was unexpected (crash, freeze or power loss). GPU left at stock.", "warn")
    elif state.get("gpu", "stability_reverted"):
        log("Logon apply SKIPPED: stability monitoring reverted the overclock (%s)." %
            state.get("gpu", "stability_reverted", "reason"), "warn")
    elif (state.get("gpu", "recheck") or {}).get("due"):
        log("Logon apply SKIPPED: a re-check of the overclock is due (%s)." % state.get("gpu", "recheck", "reason"), "warn")
    elif mode == "games":
        log("Per-game mode: the saved profile is applied only while a listed game runs.")
    else:
        stability.apply_clamped(nv, state, log)
except Exception as e:  # noqa: BLE001
    log("Logon apply failed: %s" % e, "error")

try:
    subprocess.Popen([PYW, os.path.join(APP_DIR, "bin", "headroom_monitor.pyw")], cwd=APP_DIR,
                     creationflags=0x00000008 | 0x00000200, close_fds=True)
except OSError as e:
    log("Could not start the monitor: %s" % e, "error")
