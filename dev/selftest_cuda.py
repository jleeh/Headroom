"""CUDA stress-test smoke check (no admin, no clock changes): calibrate, golden run,
5 s compute and VRAM runs.

    py -3.12 dev\\selftest_cuda.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from headroom.cuda_stress import CoreTest, Cuda, MemTest  # noqa: E402


def main():
    cu = Cuda()
    print("SMs", cu.sms)
    ct = CoreTest(cu)
    print("iterations", ct.calibrate())
    ct.golden()
    print("golden ok", list(ct.ref.values())[:1])
    print("core", ct.run(5))
    mt = MemTest(cu, fraction=0.5)
    print("VRAM GiB", mt.gib)
    print("mem", mt.run(5))
    mt.free()
    cu.close()


if __name__ == "__main__":
    main()
