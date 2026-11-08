"""GPU fan curve process (needs admin). Started by Headroom, or at logon by the
'Headroom Fan Curve' task. Exits on its own when the curve is turned off."""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo / install root
from headroom.common import DATA_DIR, Log  # noqa: E402
from headroom.fan_curve import run_daemon  # noqa: E402

log = Log()
try:
    run_daemon(log)
except Exception:  # noqa: BLE001
    with open(os.path.join(DATA_DIR, "fans_crash.log"), "a", encoding="utf-8") as f:
        f.write(traceback.format_exc())
