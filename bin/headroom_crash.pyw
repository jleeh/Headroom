"""Started by the 'Headroom Crash Watch' scheduled tasks when Windows logs a GPU
reset, WHEA error, bugcheck or unexpected shutdown. Classifies and records it and
backs the GPU overclock off if it looks like the cause (see headroom/stability.py)."""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo / install root
from headroom.common import DATA_DIR, Log, State  # noqa: E402
from headroom import stability  # noqa: E402


def main():
    log = Log()
    state = State()
    nv = None
    try:
        from headroom.nvml import Nvml
        nv = Nvml()
    except Exception as e:  # noqa: BLE001
        log("Crash watch: GPU not available (%s); events are recorded but not acted on" % e, "warn")
    drv = None
    try:
        drv = nv.driver() if nv else None
    except Exception:  # noqa: BLE001
        pass
    stability.run_locked(stability.process, state, nv, log, driver=drv)


try:
    main()
except Exception:  # noqa: BLE001
    with open(os.path.join(DATA_DIR, "crash_watch_error.log"), "a", encoding="utf-8") as f:
        f.write(traceback.format_exc())
