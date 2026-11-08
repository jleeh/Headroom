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
