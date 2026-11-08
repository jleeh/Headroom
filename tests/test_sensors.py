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


NETSH = """Request queue name: Request queue is unnamed.
    Version: 2.0
    State: Active
    Number of active processes attached: 1
    Processes:
        ID: 24964, image: C:\\Program Files\\Headroom\\tools\\LibreHardwareMonitor\\LibreHardwareMonitor.exe
    URL groups:
    URL group ID: FE00000F20000001
        State: Active
        Request queue name: Request queue is unnamed.
            Number of registered URLs: 1
            Registered URLs:
                HTTP://+:8085/
Request queue name: Request queue is unnamed.
    Processes:
        ID: 999, image: C:\\other.exe
    URL groups:
            Registered URLs:
                HTTP://+:5357/
"""


def test_httpsys_owner_parse():
    assert sensors.httpsys_owners(NETSH) == [24964]
    assert sensors.httpsys_owners(NETSH, 9999) == []


def test_lhm_processes_parse():
    out = '"LibreHardwareMonitor.exe","24964","Console","1","80,000 K"\r\n"LibreHardwareMonitor.exe","37208","Console","1","70,000 K"\r\n'
    assert sensors.lhm_processes(out) == [24964, 37208]
    assert sensors.lhm_processes("INFO: No tasks are running which match the specified criteria.") == []


def test_httpsys_listener_accepted(monkeypatch):
    lhm = sensors.Lhm(verify_owner=True)
    monkeypatch.setattr(sensors, "listener_pid_win", lambda port=8085: 4)
    monkeypatch.setattr("headroom.common.run", lambda cmd, timeout=30: NETSH if cmd[0] == "netsh" else "")
    ok_path = r"C:\Program Files\Headroom\tools\LibreHardwareMonitor\LibreHardwareMonitor.exe"
    monkeypatch.setattr(sensors, "_proc_info", lambda pid: (ok_path, True) if pid == 24964 else (None, False))
    assert lhm.owner_problem() is None
    lhm2 = sensors.Lhm(verify_owner=True)
    monkeypatch.setattr(sensors, "_proc_info", lambda pid: (ok_path, False))
    assert "administrator" in lhm2.owner_problem()
