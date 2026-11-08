"""Headless CPU thermal watchdog, started at logon by the 'Headroom CPU Watchdog' task
while a Curve Optimizer search or verify is in progress. CoreCycler resumes on its
own after a crash; if Headroom itself then fails to start, this still stops the
stress test on the temperature limit or a failed pump / CPU fan. Exits when the CPU
flow leaves the search/verify phases."""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from headroom import cpu_tune  # noqa: E402
from headroom.common import Log, State, load_profiles  # noqa: E402
from headroom.sensors import Telemetry  # noqa: E402

LOCK_PORT = 7875


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return   # already running
    log, state = Log(), State()
    if state.get("cpu", "phase") not in ("search", "verify"):
        cpu_tune.set_cpu_watchdog_at_logon(False)
        return
    prof = load_profiles()
    cpu_p = next(iter((prof.get("cpu") or {}).values()), {})
    cooler = next(iter((prof.get("cooler") or {}).values()), {})
    cc = (state.get("cpu", "search") or {}).get("cc") or cpu_tune.find_corecycler() or ""
    tele = Telemetry(None)
    tele.start()
    w = cpu_tune.CpuWatchdog(tele, log, cpu_p, cooler, cc)
    w.start()
    log("CPU watchdog running (headless) for the %s phase" % state.get("cpu", "phase"))
    while w.is_alive() and state.get("cpu", "phase") in ("search", "verify"):
        time.sleep(5)
    w.stop()
    tele.stop()


if __name__ == "__main__":
    main()
