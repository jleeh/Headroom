"""Elevated hardware self-test (run as administrator, NVIDIA GPU required):
stock load run, small offsets (+50 core / +250 memory) with read-back, a second
load run, then everything back to stock. Writes dev/selftest_result.txt.

    py -3.12 dev\\selftest_admin.py
"""
import os
import sys
import threading
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from headroom.common import is_admin  # noqa: E402
from headroom.cuda_stress import CoreTest, Cuda, MemTest  # noqa: E402
from headroom.nvml import CLK_GRAPHICS, CLK_MEM, Nvml  # noqa: E402
from headroom.sensors import Telemetry  # noqa: E402

KINDS = ((CLK_GRAPHICS, "core"), (CLK_MEM, "mem"))


def mean(xs):
    return sum(xs) / len(xs) if xs else 0


def loaded_run(label, tele, core, mem, log, secs=8):
    """One compute + one VRAM run while sampling clocks and power under load."""
    samples, stop = [], threading.Event()

    def sample():
        while not stop.wait(1):
            g = tele.get().get("gpu", {})
            if g.get("util", 0) >= 90:
                samples.append(g)
    th = threading.Thread(target=sample, daemon=True)
    th.start()
    r, m = core.run(secs), mem.run(secs / 2)
    stop.set()
    th.join()
    log("%-10s core ok=%s rate=%.3e  clk=%.0f MHz  mem=%.0f MHz  power=%.0f W  bw=%.1f GB/s  vram ok=%s" % (
        label, r["ok"], r["rate"], mean([s["clk"] for s in samples]), mean([s["mem_clk"] for s in samples]),
        mean([s["power"] for s in samples]), m["gbps"], m["ok"]))


def offsets(nv, log, when):
    for kind, name in KINDS:
        for pstate in (0, 2):
            log("offset %-6s %-4s P%d: %s" % (when, name, pstate, nv.get_offset(kind, pstate)))


def main():
    path = os.path.join(ROOT, "dev", "selftest_result.txt")
    with open(path, "w", buffering=1, encoding="utf-8") as out:
        def log(*a):
            out.write(" ".join(str(x) for x in a) + "\n")
            print(*a)
        nv = Nvml()
        try:
            log("admin", is_admin())
            tele = Telemetry(nv)
            tele.start()
            cu = Cuda()
            core = CoreTest(cu)
            core.calibrate()
            core.golden()
            mem = MemTest(cu, fraction=0.5)
            loaded_run("stock", tele, core, mem, log)
            offsets(nv, log, "before")
            nv.set_offset(CLK_GRAPHICS, 50)
            nv.set_offset(CLK_MEM, 250)
            offsets(nv, log, "after")
            log("max clocks now", nv.max_clock(0), nv.max_clock(2))
            loaded_run("+50/+250", tele, core, mem, log)
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
        finally:
            log("reset errors", nv.reset_all())
            offsets(nv, log, "final")
    print("written to", path)


if __name__ == "__main__":
    main()
