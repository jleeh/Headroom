import os

from headroom import common


def test_state_sees_other_process_writes(tmp_path):
    p = str(tmp_path / "state.json")
    a, b = common.State(p), common.State(p)
    a.set("gpu", "applied", value={"core": 100})
    os.utime(p, ns=(1, 1))            # force a different signature even on coarse clocks
    b.set("gpu", "recheck", value={"due": True})
    # a must not drop b's write when it writes next
    a.set("cpu", "phase", value="search")
    fresh = common.State(p)
    assert fresh.get("gpu", "recheck") == {"due": True}
    assert fresh.get("gpu", "applied") == {"core": 100}
    assert fresh.get("cpu", "phase") == "search"


def test_verified_download_rejects_bad_hash(tmp_path, monkeypatch):
    import io
    monkeypatch.setitem(common.PINNED, "t", {"version": "1", "name": "t.bin", "size": 3, "url": "https://x/t.bin",
                                             "sha256": "0" * 64})
    opener = lambda req, timeout: io.BytesIO(b"bad")
    try:
        common.verified_download("t", str(tmp_path), opener=opener)
        raise AssertionError("expected failure")
    except RuntimeError as e:
        assert "integrity" in str(e)
    assert not os.listdir(tmp_path)


def test_verified_download_ok(tmp_path, monkeypatch):
    import hashlib
    import io
    data = b"hello"
    monkeypatch.setitem(common.PINNED, "t", {"version": "1", "name": "t.bin", "size": 5, "url": "https://x/t.bin",
                                             "sha256": hashlib.sha256(data).hexdigest()})
    path = common.verified_download("t", str(tmp_path), opener=lambda req, timeout: io.BytesIO(data))
    assert open(path, "rb").read() == data


def test_load_profiles_reclamps(monkeypatch, tmp_path):
    import json
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"gpu": {"RTX": {"safety": {"abort_core_c": 120, "max_core_offset": 5000}}},
                             "cpu": {"Ryzen": {"family": "amd-zen5", "safety": {"abort_tdie_c": 110}}}}))
    monkeypatch.setattr(common, "PROFILE_FILE", str(p))
    prof = common.load_profiles()
    assert prof["gpu"]["RTX"]["safety"]["abort_core_c"] == 85
    assert prof["gpu"]["RTX"]["safety"]["max_core_offset"] == 500
    assert prof["cpu"]["Ryzen"]["safety"]["abort_tdie_c"] == 90


def test_clamp_offsets():
    from headroom.gpu_tune import clamp_offsets
    assert clamp_offsets(9999, 99999, 0.5) == (500, 6000)
    assert clamp_offsets(-50, -1, 1.0) == (0, 0)
    assert clamp_offsets(100, 400, None) == (100, 400)
