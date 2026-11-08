import os
import struct
import time

from headroom import stability as S
from headroom.common import State

NS = "http://schemas.microsoft.com/win/2004/08/events/event"


def xml(provider, eid, record, data=(), channel="System", t="2026-11-08T10:00:00.123456789Z", names=True):
    d = "".join(('<Data Name="%s">%s</Data>' % x) if names else ("<Data>%s</Data>" % x[1]) for x in data)
    return ('<Event xmlns="%s"><System><Provider Name="%s"/><EventID>%d</EventID><TimeCreated SystemTime="%s"/>'
            '<EventRecordID>%d</EventRecordID><Channel>%s</Channel></System><EventData>%s</EventData></Event>'
            % (NS, provider, eid, t, record, channel, d))


NOW = 1_800_000_000.0


def ev(provider, eid, record, data=(), channel="System", t=None, names=True):
    e = S.parse_events_xml(xml(provider, eid, record, data, channel, names=names))[0]
    e["time"] = t if t is not None else NOW - 30
    return e


class FakeNv:
    def __init__(self):
        self.calls = []

    def set_offset(self, kind, mhz):
        self.calls.append((kind, mhz))

    def reset_all(self):
        self.calls.append("reset")

    def reset_fans(self):
        self.calls.append("fans")

    def driver(self):
        return "581.15"

    def vbios(self):
        return "98.02"


def mkstate(tmp_path, applied=True, **result):
    st = State(str(tmp_path / "state.json"))
    r = {"core": 150, "mem": 400, "mem_ratio": 0.5, "mem_afterburner": 800, "tested": {"x": 1}}
    r.update(result)
    st.set("gpu", "result", value=r)
    st.set("gpu", "applied", value={"core": r["core"], "mem": r["mem"]} if applied else None)
    return st


def run_process(tmp_path, st, nv, events, now=NOW, **kw):
    return S.process(st, nv, lambda *a, **k: None, path=str(tmp_path / "stab.json"), now=now, up_s=3600,
                     minidump_dir=str(tmp_path / "none"), flight_path=str(tmp_path / "flight.json"),
                     scan={"cpu": {"cores": 8, "threads": 16}}, events=events, **kw)


# ---- parsing and classification ----

def test_parse_xml_multiple_events_and_time():
    out = S.parse_events_xml(xml("nvlddmkm", 13, 5) + "\n" + xml("Display", 4101, 6))
    assert [e["id"] for e in out] == [13, 4101]
    assert out[0]["provider"] == "nvlddmkm" and out[0]["record"] == 5 and out[0]["channel"] == "System"
    assert out[0]["time"] > 1.7e9
    assert S.parse_events_xml("garbage") == []


def test_classify_tdr_variants():
    assert S.classify(ev("nvlddmkm", 13, 1))["kind"] == "gpu_tdr"
    c = S.classify(ev("Display", 4101, 2))
    assert c["kind"] == "gpu_tdr" and c["suspect"] == "gpu"
    assert S.classify(ev("Display", 4102, 2)) is None


def test_classify_app_crash_only_nvidia_module():
    d = [("p1", "eldenring.exe"), ("p4", "nvwgf2umx.dll")]
    c = S.classify(ev("Application Error", 1000, 3, d, "Application"))
    assert c["kind"] == "gpu_app_crash" and c["soft"] and c["suspect"] == "gpu" and "nvwgf2umx.dll" in c["detail"]
    assert S.classify(ev("Application Error", 1000, 4, [("p1", "x.exe"), ("p4", "KERNELBASE.dll")], "Application")) is None


def test_classify_whea_core_mapping():
    scan = {"cpu": {"cores": 8, "threads": 16}}
    c = S.classify(ev("Microsoft-Windows-WHEA-Logger", 19, 7, [("ApicId", "5"), ("MCABank", "Unified Bank")]), scan)
    assert c["kind"] == "whea_corrected" and c["suspect"] == "cpu" and c["core"] == 2
    e = ev("Microsoft-Windows-WHEA-Logger", 18, 8, [("x", "Processor APIC ID: 7")], names=True)
    c = S.classify(e, {})
    assert c["kind"] == "whea_fatal" and c["core"] == 3       # default 2 threads/core
    c = S.classify(ev("Microsoft-Windows-WHEA-Logger", 19, 9), {})
    assert c["core"] is None


def test_threads_per_core():
    assert S.threads_per_core({"cpu": {"cores": 8, "threads": 8}}) == 1
    assert S.threads_per_core({"cpu": {"cores": 6, "threads": 12}}) == 2
    assert S.threads_per_core({}) == 2
    assert S.threads_per_core({"cpu": {"cores": "x", "threads": 3}}) == 2


def test_classify_bugchecks():
    txt = "The bugcheck was: 0x00000116 (0xffffd, 0xfffff, 0x0, 0x4). A dump was saved in: C:\\Windows\\MEMORY.DMP."
    c = S.classify(ev("Microsoft-Windows-WER-SystemErrorReporting", 1001, 10, [("param1", txt)]))
    assert c["kind"] == "bugcheck" and c["suspect"] == "gpu" and c["bugcheck"] == "0x00000116"
    for code, sus in (("0x00000124", "cpu"), ("0x00000101", "cpu"), ("0x0000010e", "gpu"), ("0x0000001a", "unknown")):
        c = S.classify(ev("Microsoft-Windows-WER-SystemErrorReporting", 1001, 11, [("param1", "0x%s (0x1, 0x2)" % code[2:])]))
        assert c["suspect"] == sus, code


def test_classify_kernel_power():
    c = S.classify(ev("Microsoft-Windows-Kernel-Power", 41, 12, [("BugcheckCode", "0")]))
    assert c["kind"] == "unexpected_shutdown" and c["suspect"] == "unknown" and c["bugcheck"] is None
    c = S.classify(ev("Microsoft-Windows-Kernel-Power", 41, 13, [("BugcheckCode", "278")]))
    assert c["bugcheck"] == "0x00000116" or c["suspect"] == "unknown"   # decimal 278 == 0x116
    assert c["suspect"] == "gpu"


def test_minidump(tmp_path):
    hdr = bytearray(0x60)
    hdr[:8] = b"PAGEDU64"
    struct.pack_into("<I", hdr, 0x38, 0x116)
    struct.pack_into("<4Q", hdr, 0x40, 1, 2, 3, 4)
    p = tmp_path / "110826-1.dmp"
    p.write_bytes(bytes(hdr))
    m = S.parse_minidump_bugcheck(str(p))
    assert m["bugcheck"] == "0x00000116" and m["params"] == [1, 2, 3, 4] and m["suspect"] == "gpu"
    bad = tmp_path / "bad.dmp"
    bad.write_bytes(b"nope" * 40)
    assert S.parse_minidump_bugcheck(str(bad)) is None
    assert S.parse_minidump_bugcheck(str(tmp_path / "missing")) is None
    old = tmp_path / "old.dmp"
    old.write_bytes(bytes(hdr))
    os.utime(old, (1, 1))
    assert S.newest_minidump(str(tmp_path)).endswith("bad.dmp") or S.newest_minidump(str(tmp_path)).endswith("110826-1.dmp")
    assert S.newest_minidump(str(tmp_path / "empty")) is None


# ---- config / summary ----

def test_config_clamping(tmp_path):
    p = str(tmp_path / "s.json")
    c = S.set_config({"max_crashes": 999, "window_days": 0, "auto_backoff": 0, "bogus": 1}, p)
    assert c == {"auto_backoff": False, "max_crashes": 20, "window_days": 1, "monitor_games_only": False}
    assert S.set_config({"max_crashes": "x"}, p)["max_crashes"] == 3
    assert S.load(str(tmp_path / "nothing"))["config"] == S.DEFAULT_CONFIG


# ---- actions ----

def test_tdr_backs_off_core_then_memory(tmp_path):
    st, nv = mkstate(tmp_path), FakeNv()
    a = run_process(tmp_path, st, nv, [ev("nvlddmkm", 13, 1)])
    assert "core offset +150 -> +135" in a[0]["action"]
    assert (0, 135) in nv.calls and (2, 400) in nv.calls
    r = st.get("gpu", "result")
    assert r["core"] == 135 and r["tested"] == {"x": 1} and r["backoffs"][0]["kind"] == "core"
    assert st.get("gpu", "applied") == {"core": 135, "mem": 400}
    a = run_process(tmp_path, st, nv, [ev("nvlddmkm", 13, 2)], now=NOW + 60)
    r = st.get("gpu", "result")
    assert r["core"] == 135 and r["mem"] == 275 and r["mem_afterburner"] == 550     # -250 AB = -125 NVML
    assert (2, 275) in nv.calls and r["backoffs"][-1]["kind"] == "mem"


def test_dedup_and_no_action_when_not_applied(tmp_path):
    st, nv = mkstate(tmp_path, applied=False), FakeNv()
    e = ev("nvlddmkm", 13, 1)
    assert "nothing changed" in run_process(tmp_path, st, nv, [e])[0]["action"]
    assert nv.calls == []
    assert run_process(tmp_path, st, nv, [e]) == []              # same record id: skipped
    assert len(S.load(str(tmp_path / "stab.json"))["events"]) == 1


def test_revert_after_max_crashes(tmp_path):
    st, nv = mkstate(tmp_path), FakeNv()
    for i in range(3):
        a = run_process(tmp_path, st, nv, [ev("nvlddmkm", 13, i)], now=NOW + i * 60)
    assert a[0]["action"].startswith("Reverted to stock")
    assert "reset" in nv.calls and st.get("gpu", "applied") is None
    assert st.get("gpu", "stability_reverted")["reason"]
    assert S.summary(st, str(tmp_path / "stab.json"))["status"].startswith("reverted to stock")


def test_no_backoff_when_disabled(tmp_path):
    S.set_config({"auto_backoff": False}, str(tmp_path / "stab.json"))
    st, nv = mkstate(tmp_path), FakeNv()
    a = run_process(tmp_path, st, nv, [ev("nvlddmkm", 13, 1)])
    assert "off" in a[0]["action"] and nv.calls == []


def test_soft_app_crash_needs_two(tmp_path):
    st, nv = mkstate(tmp_path), FakeNv()
    d = [("p1", "game.exe"), ("p4", "nvwgf2umx.dll")]
    a = run_process(tmp_path, st, nv, [ev("Application Error", 1000, 1, d, "Application")])
    assert "Soft" in a[0]["action"] and nv.calls == []
    a = run_process(tmp_path, st, nv, [ev("Application Error", 1000, 2, d, "Application")], now=NOW + 100)
    assert "back-off" in a[0]["action"] and nv.calls


def test_cpu_events_only_recorded(tmp_path):
    st, nv = mkstate(tmp_path), FakeNv()
    a = run_process(tmp_path, st, nv, [ev("Microsoft-Windows-WHEA-Logger", 19, 1, [("ApicId", "6")])])
    assert nv.calls == [] and a[0]["core"] == 3 and "BIOS" in a[0]["action"]
    s = S.summary(st, str(tmp_path / "stab.json"))
    assert s["cpu_cores_to_loosen"] == {"3": 1} and s["counts"] == {"whea_corrected": 1}


def test_bugcheck_merges_with_kernel_power(tmp_path):
    st, nv = mkstate(tmp_path), FakeNv()
    bc = ev("Microsoft-Windows-WER-SystemErrorReporting", 1001, 5, [("param1", "0x00000116 (0x1)")], t=NOW - 20)
    kp = ev("Microsoft-Windows-Kernel-Power", 41, 4, [("BugcheckCode", "0")], t=NOW - 40)
    a = run_process(tmp_path, st, nv, [kp, bc])
    assert [r["kind"] for r in a] == ["bugcheck"] and a[0]["suspect"] == "gpu" and nv.calls


def test_kernel_power_uses_minidump(tmp_path):
    hdr = bytearray(0x60)
    hdr[:8] = b"PAGEDU64"
    struct.pack_into("<I", hdr, 0x38, 0x116)
    d = tmp_path / "dumps"
    d.mkdir()
    (d / "a.dmp").write_bytes(bytes(hdr))
    os.utime(d / "a.dmp", (NOW - 35, NOW - 35))
    st, nv = mkstate(tmp_path), FakeNv()
    kp = ev("Microsoft-Windows-Kernel-Power", 41, 4, [("BugcheckCode", "0")], t=NOW - 40)
    a = S.process(st, nv, lambda *x, **k: None, path=str(tmp_path / "s.json"), now=NOW, up_s=3600, minidump_dir=str(d),
                  flight_path=str(tmp_path / "f.json"), events=[kp])
    assert a[0]["bugcheck"] == "0x00000116" and a[0]["suspect"] == "gpu"


def test_flight_attached(tmp_path):
    f = S.Flight(str(tmp_path / "flight.json"), size=3, every=0)
    for i in range(5):
        f.add({"t": i, "temp": 50 + i}, now=NOW + i)
    st, nv = mkstate(tmp_path), FakeNv()
    a = run_process(tmp_path, st, nv, [ev("nvlddmkm", 13, 1)])
    assert [s["t"] for s in a[0]["flight"]] == [2, 3, 4]


def test_old_events_recorded_not_acted(tmp_path):
    st, nv = mkstate(tmp_path), FakeNv()
    a = S.process(st, nv, lambda *x, **k: None, path=str(tmp_path / "s.json"), now=NOW, up_s=60,
                  flight_path=str(tmp_path / "f.json"), events=[ev("nvlddmkm", 13, 1, t=NOW - 86400)])
    assert a[0]["action"].startswith("Old") and nv.calls == []


# ---- tasks ----

def test_install_remove_tasks():
    calls = []

    def fake(cmd, timeout=30):
        calls.append(cmd)
        return "TaskName: x" if cmd[1] == "/query" else ""
    res = S.install_event_tasks(r"C:\py\pythonw.exe", r"C:\app", fake)
    assert set(res) == set(S.TASKS)
    cr = [c for c in calls if c[1] == "/create"]
    assert len(cr) == 2 and cr[0][cr[0].index("/ec") + 1] == "System" and cr[1][cr[1].index("/ec") + 1] == "Application"
    assert all(c[c.index("/sc") + 1] == "onevent" and "/rl" in c and "/f" in c for c in cr)
    assert cr[0][cr[0].index("/tr") + 1] == '"C:\\py\\pythonw.exe" "C:\\app\\headroom_crash.pyw"'
    assert all(len(c[c.index("/mo") + 1]) < 1000 for c in cr)
    S.remove_event_tasks(fake)
    assert sum(1 for c in calls if c[1] == "/delete") == 2


def test_xpath_age_filter_wraps():
    q = S._with_age(S.APP_XPATH, 600)
    assert q.startswith("*[System[TimeCreated[timediff(@SystemTime) <= 600000]] and System[") and q.endswith("]]")
    assert q.count("[") == q.count("]")
    assert q.count("(") == q.count(")")


def test_query_uses_wevtutil_xml():
    seen = []

    def fake(cmd, timeout=30):
        seen.append(cmd)
        return xml("nvlddmkm", 13, 1)
    out = S.query("System", S.SYSTEM_XPATH, 600, fake)
    assert seen[0][:3] == ["wevtutil", "qe", "System"] and "/f:xml" in seen[0] and out[0]["id"] == 13


# ---- monitor helpers ----

def test_decide_apply():
    assert S.decide_apply("always", [], set())
    assert not S.decide_apply("always", [], set(), recheck_due=True)
    assert not S.decide_apply("always", [], set(), reverted=True)
    assert not S.decide_apply("games", ["EldenRing.exe"], {"chrome.exe"})
    assert S.decide_apply("games", ["EldenRing.exe"], {"eldenring.exe"})
    assert not S.decide_apply("games", ["eldenring.exe"], {"eldenring.exe"}, reverted=True)
    assert not S.decide_apply("games", [], {"a.exe"})


def test_running_processes():
    out = '"System","4","Services","0","10 K"\n"EldenRing.exe","123","Console","1","1 K"\n'
    assert S.running_processes(lambda c, timeout=30: out) == {"system", "eldenring.exe"}


def test_should_recheck():
    now = time.mktime(time.strptime("2026-11-08 12:00", "%Y-%m-%d %H:%M"))
    fresh = "2026-11-01 12:00"
    assert S.should_recheck([40] * 60, 40, fresh, now) is None
    assert S.should_recheck([48] * 60, 40, fresh, now)                       # +8 C
    assert S.should_recheck([47] * 60, 40, fresh, now) is None
    assert S.should_recheck([48] * 5, 40, fresh, now) is None                # too few samples
    assert S.should_recheck([40] * 60, 40, "2026-07-01 12:00", now)           # older than 90 days
    assert S.should_recheck([48] * 60, None, fresh, now) is None
    assert S.should_recheck([40] * 60, 40, None, now) is None


def test_fans_stale():
    st = {"mode": "manual", "t": NOW - 30, "pid": 99}
    assert S.fans_stale(st, NOW, lambda p: False)
    assert not S.fans_stale(st, NOW, lambda p: True)
    assert not S.fans_stale({"mode": "card", "t": NOW - 30, "pid": 99}, NOW, lambda p: False)
    assert not S.fans_stale({"mode": "manual", "t": NOW - 3, "pid": 99}, NOW, lambda p: False)
    assert S.fans_stale({"mode": "manual", "t": NOW - 30}, NOW, lambda p: True)


def test_driver_change_and_clamp(tmp_path):
    nv = FakeNv()
    assert S.driver_change({"driver": "581.15"}, nv) is None
    assert "driver changed from 500.1 to 581.15" == S.driver_change({"driver": "500.1"}, nv)
    assert "vbios" in S.driver_change({"vbios": "1"}, nv)
    assert S.driver_change({}, nv) is None
    assert S.clamp_result({"core": 9999, "mem": 99999, "mem_ratio": 0.5}) == (500, 1500)
    assert S.clamp_result({"core": -5, "mem": -1}) == (0, 0)
    st = mkstate(tmp_path, core=900)
    S.apply_clamped(nv, st, lambda *a: None)
    assert (0, 500) in nv.calls and st.get("gpu", "applied")["core"] == 500


def test_sample_flight():
    r = S.sample_flight({"t": 5, "gpu": {"temp": 60, "util": 99}, "gpu_x": {"mem_junction": 80}})
    assert r["temp"] == 60 and r["mem_junction"] == 80 and r["fan"] is None
