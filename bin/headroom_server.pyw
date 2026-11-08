"""Headroom backend (started by Headroom.exe, elevated). Serves the UI + API on 127.0.0.1:7871."""
import multiprocessing
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo / install root

if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        from headroom.server import main
        main()
    except Exception:  # noqa: BLE001
        d = os.path.join(os.environ.get("APPDATA", "."), "Headroom")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "server_crash.log"), "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        raise
