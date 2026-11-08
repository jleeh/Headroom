"""Sensor access: LibreHardwareMonitor web server (CPU/board/GPU memory junction)
plus NVML for the GPU. LHM must be running with its web server on :8085
(it already autostarts elevated on this PC)."""
import json
import os
import re
import sys
import threading
import time
import urllib.request

LHM_URL = "http://127.0.0.1:8085/data.json"
LHM_PORT = 8085
OWNER_RECHECK_SEC = 30


def listening_pid(netstat_out, port=LHM_PORT):
    """PID listening on 127.0.0.1:<port> (or 0.0.0.0) from `netstat -ano -p TCP`."""
    for ln in (netstat_out or "").splitlines():
        f = ln.split()
        if len(f) >= 5 and f[0].upper() == "TCP" and f[3].upper() == "LISTENING" and \
                f[1] in ("127.0.0.1:%d" % port, "0.0.0.0:%d" % port):
            try:
                return int(f[4])
            except ValueError:
                return None
    return None


def listener_pid_win(port=LHM_PORT):
    """PID listening on 127.0.0.1/0.0.0.0:<port> from the IPv4 listener table
    (GetExtendedTcpTable). netstat output isn't reliable enough for this: on some
    systems it shows listeners as CLOSED."""
    import ctypes
    import ctypes.wintypes as W
    ip = ctypes.windll.iphlpapi
    size = W.DWORD(0)
    ip.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)   # AF_INET, TCP_TABLE_OWNER_PID_LISTENER
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = W.DWORD(len(buf))
        if ip.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 3, 0) == 0:
            break
    else:
        raise OSError("GetExtendedTcpTable failed")
    n = int.from_bytes(buf.raw[0:4], "little")
    for i in range(n):
        off = 4 + i * 24          # MIB_TCPROW_OWNER_PID: state, laddr, lport, raddr, rport, pid
        row = buf.raw[off:off + 24]
        laddr = row[4:8]
        lport = int.from_bytes(row[8:10], "big")
        pid = int.from_bytes(row[20:24], "little")
        if lport == port and laddr in (b"\x7f\x00\x00\x01", b"\x00\x00\x00\x00"):
            return pid
    return None


def _proc_info(pid):
    """(image path, elevated) for a PID, via the Win32 API. (None, False) if unknown."""
    import ctypes
    import ctypes.wintypes as W
    k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
    k32.OpenProcess.restype = W.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None, False
    try:
        buf, n = ctypes.create_unicode_buffer(1024), W.DWORD(1024)
        path = buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
        tok, elev, ret = W.HANDLE(), W.DWORD(0), W.DWORD(0)
        elevated = False
        if adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):   # TOKEN_QUERY
            if adv.GetTokenInformation(tok, 20, ctypes.byref(elev), 4, ctypes.byref(ret)):   # TokenElevation
                elevated = bool(elev.value)
            k32.CloseHandle(tok)
        return path, elevated
    finally:
        k32.CloseHandle(h)


def owner_verdict(path, elevated):
    """The safety watchdogs act on these temperatures, so only an elevated
    LibreHardwareMonitor counts: an unprivileged program that grabbed port 8085
    could otherwise feed fake 'cool' readings."""
    if not path:
        return "port %d: owner unknown" % LHM_PORT
    if os.path.basename(path.replace("\\", "/")).lower() != "librehardwaremonitor.exe":
        return "port %d is used by %s, not LibreHardwareMonitor" % (LHM_PORT, os.path.basename(path))
    if not elevated:
        return "LibreHardwareMonitor on port %d isn't running as administrator" % LHM_PORT
    return None


def _num(v):
    if v is None:
        return None
    m = re.match(r"\s*(-?[\d.,]+)", str(v))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


class Lhm:
    """Flattens data.json into {SensorId: (type, text, value, hardware)}."""

    def __init__(self, url=LHM_URL, verify_owner=None):
        self.url = url
        self.ok = False
        self.err = ""
        self.s = {}
        self.verify_owner = (sys.platform == "win32") if verify_owner is None else verify_owner
        self._owner = (None, 0, None)      # (pid, checked_at, problem)

    def owner_problem(self):
        if not self.verify_owner:
            return None
        from .common import run
        cpid, at, prob = self._owner
        if cpid is not None and time.time() - at < OWNER_RECHECK_SEC:
            return prob      # netstat is too slow to run every second; the owner rarely changes
        try:
            pid = listener_pid_win()
        except Exception:  # noqa: BLE001
            pid = listening_pid(run(["netstat", "-ano", "-p", "TCP"], 10))
        if pid is None:
            self._owner = (None, 0, None)
            return None    # nothing listening: the request fails on its own
        try:
            prob = owner_verdict(*_proc_info(pid))
        except Exception as e:  # noqa: BLE001
            prob = "port %d: couldn't check its owner (%s)" % (LHM_PORT, e)
        self._owner = (pid, time.time(), prob)
        return prob

    def poll(self):
        prob = self.owner_problem()
        if prob:
            self.ok, self.err = False, prob
            return False
        try:
            with urllib.request.urlopen(self.url, timeout=2) as r:
                d = json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            self.ok, self.err = False, str(e)
            self._owner = (None, 0, None)   # whoever answers next gets checked again
            return False
        out = {}

        def walk(n, hw):
            if n.get("SensorId") and n.get("Type"):
                out[n["SensorId"]] = (n["Type"], n.get("Text", ""), _num(n.get("Value")), hw)
            for c in n.get("Children", []):
                walk(c, hw if n.get("SensorId") else n.get("Text", hw))
        walk(d, "")
        self.s, self.ok, self.err = out, True, ""
        return True

    def val(self, sid):
        v = self.s.get(sid)
        return v[2] if v else None

    def find(self, prefix, typ, text_re):
        rx = re.compile(text_re)
        for sid, (t, txt, v, _) in self.s.items():
            if sid.startswith(prefix) and t == typ and rx.search(txt):
                return v
        return None

    def all(self, prefix, typ, text_re):
        rx = re.compile(text_re)
        return [(txt, v) for sid, (t, txt, v, _) in sorted(self.s.items())
                if sid.startswith(prefix) and t == typ and rx.search(txt)]

    # ---- convenience views --------------------------------------------------
    def _cpu_prefix(self):
        for p in ("/amdcpu/0/", "/intelcpu/0/"):
            if any(k.startswith(p) for k in self.s):
                return p
        return "/amdcpu/0/"

    def cpu(self):
        p = self._cpu_prefix()
        eff = self.all(p, "Clock", r"^Core #\d+ \(Effective\)")
        tdie = self.find(p, "Temperature", r"CCD1 \(Tdie\)") or self.find(p, "Temperature", r"^Core \(Tctl/Tdie\)$")
        pkg_t = self.find(p, "Temperature", r"^CPU Package$") or self.find(p, "Temperature", r"^Core Max$")
        return {
            "tdie": tdie if tdie is not None else pkg_t,   # the temperature the safety limits use
            "tctl": self.find(p, "Temperature", r"Tctl"),
            "package_w": self.find(p, "Power", r"^(Package|CPU Package)$"),
            "eff_avg": self.find(p, "Clock", r"Average Effective"),
            "eff_cores": [v for _, v in eff],
            "max_core_clock": max([v for _, v in self.all(p, "Clock", r"^(Core|P-Core) #\d+$") if v] or [0]),
            "load": self.find(p, "Load", r"CPU Total"),
            "vcore": self.find("/lpc/", "Voltage", r"^Vcore$") or self.find(p, "Voltage", r"^Core( \(SVI\d TFN\))?$"),
        }

    def gpu_extra(self):
        p = "/gpu-nvidia/0/"
        return {
            "mem_junction": self.find(p, "Temperature", r"Memory Junction"),
            "hotspot": self.find(p, "Temperature", r"Hot ?Spot"),
            "vcore": self.find(p, "Voltage", r"Core Voltage"),
            "fans": [v for _, v in self.all(p, "Fan", r"GPU Fan")],
        }

    def board_fans(self):
        return {txt: v for txt, v in self.all("/lpc/", "Fan", r"Fan")}


class Telemetry(threading.Thread):
    """Background sampler shared by the GUI and the tuners. Readers get the
    latest snapshot via .snap; nothing here ever changes hardware."""

    def __init__(self, nvml=None, period=1.0):
        super().__init__(daemon=True)
        self.nv = nvml
        self.lhm = Lhm()
        self.period = period
        self.snap = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.period)

    def sample(self):
        s = {"t": time.time()}
        if self.nv:
            try:
                s["gpu"] = {
                    "temp": self.nv.temp(), "power": self.nv.power_w(),
                    "clk": self.nv.clock(0), "mem_clk": self.nv.clock(2),
                    "util": self.nv.util()[0], "fan": self.nv.fan_pct(),
                    "throttle": self.nv.throttle(), "pstate": self.nv.pstate(),
                }
            except Exception as e:  # noqa: BLE001
                s["gpu"] = {"error": str(e)}
        if self.lhm.poll():
            s["cpu"] = self.lhm.cpu()
            s["gpu_x"] = self.lhm.gpu_extra()
            s["fans"] = self.lhm.board_fans()
        else:
            s["lhm_error"] = self.lhm.err
        with self.lock:
            self.snap = s
        return s

    def get(self):
        with self.lock:
            return dict(self.snap)

    def stop(self):
        self._stop.set()
