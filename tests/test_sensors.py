from headroom import sensors

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1220
  TCP    127.0.0.1:7871         0.0.0.0:0              LISTENING       9100
  TCP    127.0.0.1:8085         0.0.0.0:0              LISTENING       4242
  TCP    127.0.0.1:8085         127.0.0.1:50000        ESTABLISHED     4242
"""


def test_listening_pid():
    assert sensors.listening_pid(NETSTAT) == 4242
    assert sensors.listening_pid(NETSTAT, 9999) is None


def test_owner_verdict():
    lhm = r"C:\Program Files\Headroom\tools\LibreHardwareMonitor\LibreHardwareMonitor.exe"
    assert sensors.owner_verdict(lhm, True) is None
    assert "administrator" in sensors.owner_verdict(lhm, False)
    assert "not LibreHardwareMonitor" in sensors.owner_verdict(r"C:\Users\x\evil.exe", True)
    assert "unknown" in sensors.owner_verdict(None, False)


def test_poll_refuses_bad_owner(monkeypatch):
    lhm = sensors.Lhm(verify_owner=True)
    monkeypatch.setattr(lhm, "owner_problem", lambda: "port 8085 is used by evil.exe, not LibreHardwareMonitor")
    assert lhm.poll() is False and "evil" in lhm.err
