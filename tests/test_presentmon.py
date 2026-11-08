import pytest

from headroom import presentmon as pm
from headroom.nvml import CLK_GRAPHICS, CLK_MEM

TASKLIST = '''"System Idle Process","0","Services","0","8 K"
"svchost.exe","900","Services","0","10 K"
"explorer.exe","100","Console","1","10 K"
"pythonw.exe","200","Console","1","10 K"
"Game Name.exe","300","Console","1","1,000 K"
"cyberpunk2077.exe","301","Console","1","1,000 K"
"bad;name.exe","302","Console","1","1 K"
'''


def test_running_processes():
    assert pm.running_processes(TASKLIST) == ["cyberpunk2077.exe", "Game Name.exe"]


def test_valid_process():
    assert pm.valid_process("a-b (1).exe")
    for bad in ("", "a.exe --x", "a;b.exe", "game", "..\\x.exe", "a" * 70 + ".exe", "-x.exe", "x.exe\n"):
        assert not pm.valid_process(bad)


def test_parse_columns_priority_and_na():
    v2 = "Application,FrameTime,MsBetweenPresents\ng.exe,10,5\ng.exe,NA,5\ng.exe,,5\ng.exe,0,5\n"
    assert pm.parse_frame_times(v2) == [10.0]
    v1 = "Application,MsBetweenPresents,MsBetweenDisplayChange\ng.exe,8,0\ng.exe,NA,0\n"
    assert pm.parse_frame_times(v1) == [8.0]
    assert pm.parse_frame_times("Application,Other\ng.exe,1\n") == []
    assert pm.parse_frame_times("") == []


def test_stats():
    s = pm.stats([10.0] * 99 + [100.0])
    assert s["frames"] == 100
    assert s["avg_fps"] == pytest.approx(1000 * 100 / 1090)
    assert s["low1"] == pytest.approx(10.0)
    assert s["low01"] == pytest.approx(10.0)
    assert pm.stats([])["avg_fps"] == 0.0


def test_warmup_trim():
    ft = [100.0] * 20
    assert len(pm.trim_warmup(ft, 1000)) == 10
    assert pm.stats(ft, 1000)["frames"] == 10
    assert pm.trim_warmup([100.0] * 5, 1000) == []


class FakeNv:
    def __init__(self):
        self.calls = []

    def set_offset(self, kind, mhz):
        self.calls.append((kind, mhz))


class FakeState:
    def __init__(self, **d):
        self.d = d

    def get(self, *k, default=None):
        x = self.d
        for i in k:
            if not isinstance(x, dict) or i not in x:
                return default
            x = x[i]
        return x

    def set(self, *k, value):
        self.d[k[0]] = value


def make_runner(modes, order):
    """Writes a CSV whose frame time depends on the offset the fake NVML holds."""
    def runner(cmd, path, seconds):
        assert "--process_name" in cmd and cmd[cmd.index("--process_name") + 1] == "g.exe"
        assert "--terminate_after_timed" in cmd and "--timed" in cmd
        ft = modes[len(order)]
        order.append(ft)
        if ft is None:
            return 0
        with open(path, "w") as f:
            f.write("Application,FrameTime\n" + "".join("g.exe,%s\n" % ft for _ in range(300)))
        return 0
    return runner


def test_measure_game_ab_and_restore():
    nv, order = FakeNv(), []
    st = FakeState(gpu={"result": {"core": 200, "mem": 1000}, "applied": {"core": 50, "mem": 100}})
    runner = make_runner([10, 9, 10, 9], order)
    r = pm.measure_game(nv, st, lambda *a: None, "g.exe", seconds=20, rounds=2, runner=runner, sleep=lambda s: None)
    assert [s["mode"] for s in r["segments"]] == ["stock", "tuned", "stock", "tuned"]
    assert r["uplift_pct"]["avg_fps"] == pytest.approx((1 / 9) / (1 / 10) * 100 - 100)
    assert r["noise_pct"] == pytest.approx(0)
    assert "within noise" not in r["verdict"]
    assert nv.calls[0] == (CLK_GRAPHICS, 0) and nv.calls[2] == (CLK_GRAPHICS, 200)
    assert nv.calls[-2:] == [(CLK_GRAPHICS, 50), (CLK_MEM, 100)]
    assert st.d["games"][0]["process"] == "g.exe"
    assert "_ft" not in r["segments"][0]


def test_measure_noise_verdict_and_cap():
    nv, st = FakeNv(), FakeState(gpu={"result": {"core": 10, "mem": 0}}, games=[{"x": i} for i in range(25)])
    r = pm.measure_game(nv, st, lambda *a: None, "g.exe", rounds=2, runner=make_runner([10, 9.95, 12, 9.95], []),
                        sleep=lambda s: None)
    assert "within noise" in r["verdict"]
    assert len(st.d["games"]) == 20
    assert nv.calls[-2:] == [(CLK_GRAPHICS, 0), (CLK_MEM, 0)]


def test_no_frames_raises_and_restores():
    nv = FakeNv()
    st = FakeState(gpu={"result": {"core": 10, "mem": 0}, "applied": {"core": 5, "mem": 0}})
    with pytest.raises(RuntimeError, match="No frames"):
        pm.measure_game(nv, st, lambda *a: None, "g.exe", runner=make_runner([None], []), sleep=lambda s: None)
    assert nv.calls[-2] == (CLK_GRAPHICS, 5)


def test_requires_result_and_valid_name():
    with pytest.raises(RuntimeError):
        pm.measure_game(FakeNv(), FakeState(), print, "g.exe", runner=lambda *a: 0)
    with pytest.raises(ValueError):
        pm.measure_game(FakeNv(), FakeState(gpu={"result": {"core": 1}}), print, "g.exe & calc", runner=lambda *a: 0)
