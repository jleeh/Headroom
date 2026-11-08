"""Puts the GPU back to stock (offsets 0, clock lock removed, default power limit)
and hands the fans back to the card. Run by the uninstaller; safe to run any time.
  --fans-only   only hand the fans back (used while upgrading)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo / install root
try:
    from headroom.nvml import Nvml
    nv = Nvml()
    if "--fans-only" not in sys.argv:
        nv.reset_all()
    try:
        nv.reset_fans()
    except Exception:  # noqa: BLE001
        pass
except Exception:  # noqa: BLE001
    pass   # no NVIDIA GPU / driver: nothing to reset
