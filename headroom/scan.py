"""Hardware scan: what is in this PC, what Headroom can do with it, and what is
missing. Runs on first start (and from Setup > Re-scan). Read-only.

Output (scan.json) feeds profile building (profiles.py) and Claude research
(research.py), so it carries exact model names, IDs and BIOS versions.
"""
import json
import re
import time

from .common import SCAN_FILE, ps, write_json
from .sensors import Lhm

_CIM = r"""
$ErrorActionPreference='SilentlyContinue'
$c=Get-CimInstance Win32_Processor | Select -First 1
$b=Get-CimInstance Win32_BaseBoard
$bi=Get-CimInstance Win32_BIOS
$os=Get-CimInstance Win32_OperatingSystem
$cs=Get-CimInstance Win32_ComputerSystem
[pscustomobject]@{
 cpu=[pscustomobject]@{name=$c.Name.Trim(); maker=$c.Manufacturer; cores=$c.NumberOfCores; threads=$c.NumberOfLogicalProcessors; max_mhz=$c.MaxClockSpeed}
 board=[pscustomobject]@{maker=$b.Manufacturer; model=$b.Product; bios=$bi.SMBIOSBIOSVersion; bios_date=([string]$bi.ReleaseDate)}
 system=[pscustomobject]@{maker=$cs.Manufacturer; model=$cs.Model; type=$cs.PCSystemType}
 ram=@(Get-CimInstance Win32_PhysicalMemory | % { [pscustomobject]@{slot=$_.DeviceLocator; maker=$_.Manufacturer; part=([string]$_.PartNumber).Trim(); gb=[math]::Round($_.Capacity/1GB); rated=$_.Speed; running=$_.ConfiguredClockSpeed; type=$_.SMBIOSMemoryType} })
 gpus=@(Get-CimInstance Win32_VideoController | % { [pscustomobject]@{name=$_.Name; vendor=$_.AdapterCompatibility; driver=$_.DriverVersion; pnp=$_.PNPDeviceID} })
 os=[pscustomobject]@{name=$os.Caption; build=$os.BuildNumber}
 pawnio=[bool](Get-CimInstance Win32_SystemDriver -Filter "Name='PawnIO'")
 cooler_hint=@(Get-PnpDevice -PresentOnly | ? { $_.FriendlyName -match '\bAIO\b|Kraken|iCUE|Lian Li|Thermalright|NZXT|Corsair|Liquid Freezer|\bEK[- ]' } | % FriendlyName | Select -Unique)
} | ConvertTo-Json -Depth 4 -Compress
"""

GPU_FAMILIES = [
    (r"RTX\s*50\d\d", "nvidia-blackwell"),
    (r"RTX\s*40\d\d", "nvidia-ada"),
    (r"RTX\s*30\d\d", "nvidia-ampere"),
    (r"RTX\s*20\d\d|GTX\s*16\d\d", "nvidia-turing"),
    (r"RTX\s*PRO|RTX\s*A\d|Quadro|Tesla|\bA100\b|\bH100\b|\bL40", "nvidia-workstation"),
    (r"NVIDIA", "nvidia-other"),
    (r"Radeon|AMD", "amd"),
    (r"Intel", "intel"),
]


def gpu_family(name):
    for rx, fam in GPU_FAMILIES:
        if re.search(rx, name or "", re.I):
            return fam
    return "unknown"


def cpu_family(name):
    """amd-zen5[-x3d] / amd-zen4[-x3d] / amd-zen3[-x3d] / amd-zen2 / amd-other /
    intel-arrowlake / intel-raptorlake / intel-alderlake / intel-other / unknown."""
    n = name or ""
    m = re.search(r"Ryzen\s+(?:Threadripper\s+)?\d\s+(?:PRO\s+)?(\d{4})(\w*)", n, re.I)
    if m:
        num, suf = int(m.group(1)), m.group(2).upper()
        x3d = "-x3d" if "X3D" in suf else ""
        apu = "G" in suf
        if 9000 <= num < 10000:
            return "amd-zen5" + x3d
        if 7000 <= num < 8000 and num % 100 in (0, 50) and not apu:
            return "amd-zen4" + x3d
        if 8000 <= num < 9000:
            return "amd-zen4-apu"
        if 5000 <= num < 6000:
            return "amd-zen3" + x3d
        if 3000 <= num < 4000:
            return "amd-zen2"
        return "amd-other"
    if re.search(r"AMD", n, re.I):
        return "amd-other"
    if re.search(r"Core\(TM\)\s+Ultra\s+\d\s+2\d\d", n, re.I) or re.search(r"Ultra\s+\d\s+2\d\dK", n, re.I):
        return "intel-arrowlake"
    m = re.search(r"i\d-(\d{2})\d{3}", n)
    if m:
        gen = int(m.group(1))
        if gen in (13, 14):
            return "intel-raptorlake"
        if gen == 12:
            return "intel-alderlake"
        return "intel-other"
    if re.search(r"Intel", n, re.I):
        return "intel-other"
    return "unknown"


def board_vendor(maker, model=""):
    s = ("%s %s" % (maker or "", model or "")).lower()
    for key, v in (("asus", "asus"), ("asustek", "asus"), ("micro-star", "msi"), ("msi", "msi"),
                   ("gigabyte", "gigabyte"), ("aorus", "gigabyte"), ("asrock", "asrock"),
                   ("nzxt", "nzxt"), ("biostar", "biostar")):
        if key in s:
            return v
    return "other"


def _nvidia():
    from .nvml import CLK_GRAPHICS, Nvml, NvmlError
    try:
        nv = Nvml()
    except Exception as e:  # noqa: BLE001
        return {"present": False, "error": str(e)}
    out = {"present": True}
    for k, fn in (("name", nv.name), ("vbios", nv.vbios), ("driver", nv.driver)):
        try:
            out[k] = fn()
        except Exception:  # noqa: BLE001
            out[k] = None
    try:
        lo, hi = nv.power_limits_w()
        out["power"] = {"default_w": round(nv._uint("nvmlDeviceGetPowerManagementDefaultLimit") / 1000.0),
                        "min_w": round(lo), "max_w": round(hi), "current_w": round(nv.power_limit_w())}
    except Exception:  # noqa: BLE001
        out["power"] = None
    try:
        out["max_clock_mhz"] = {"graphics": nv.max_clock(0), "memory": nv.max_clock(2)}
    except Exception:  # noqa: BLE001
        pass
    try:
        out["vram_gb"] = round(nv.mem_total_bytes() / 2**30)
    except Exception:  # noqa: BLE001
        pass
    out["fans"] = nv.num_fans()
    try:
        cur, lo, hi = nv.get_offset(CLK_GRAPHICS)
        out["offsets"] = {"api": "clock_offsets", "core_range": [lo, hi]}
    except (NvmlError, AttributeError):
        out["offsets"] = {"api": "vf_offset" if nv.vf_offset_supported() else None}
    return out


def microcode_revision(raw=None):
    """CPU microcode revision Windows loaded ('Update Revision' registry value: the
    revision is the upper 32 bits of the little-endian 8-byte value). None if unknown."""
    if raw is None:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                raw = winreg.QueryValueEx(k, "Update Revision")[0]
        except (ImportError, OSError):
            return None
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 8:
        return None
    return int.from_bytes(raw[4:8], "little") or None


def scan(log=print):
    t0 = time.time()
    raw = ps(_CIM, timeout=90)
    try:
        d = json.loads(raw)
    except ValueError:
        d = {}
        log("Hardware scan: Windows inventory query failed (%r)" % raw[:200], "warn")
    for k in ("ram", "gpus", "cooler_hint"):
        v = d.get(k)
        d[k] = v if isinstance(v, list) else ([v] if v else [])
    cpu = d.get("cpu") or {}
    cpu["family"] = cpu_family(cpu.get("name"))
    cpu["vendor"] = "amd" if cpu["family"].startswith("amd") else "intel" if cpu["family"].startswith("intel") else "other"
    cpu["microcode"] = microcode_revision()
    board = d.get("board") or {}
    board["vendor"] = board_vendor(board.get("maker"), board.get("model"))
    nv = _nvidia()
    gpu_name = nv.get("name") if nv.get("present") else next(
        (g["name"] for g in d["gpus"] if not re.search(r"Basic|Virtual|Parsec|Remote", g.get("name") or "")), "")
    gpu = {"name": gpu_name, "family": gpu_family(gpu_name), "nvidia": nv}
    m = re.search(r"SUBSYS_([0-9A-F]{4})([0-9A-F]{4})", next((g.get("pnp") or "" for g in d["gpus"] if g.get("name") == gpu_name), ""), re.I)
    if m:
        gpu["board_vendor_id"] = m.group(2).upper()
    gpu["board_vendor"] = {"1462": "MSI", "1043": "ASUS", "3842": "EVGA", "19DA": "Zotac", "1458": "Gigabyte",
                           "196E": "PNY", "10DE": "NVIDIA (Founders Edition)", "1569": "Palit", "1B4C": "KFA2/Galax",
                           "7377": "Colorful", "1DA2": "Sapphire", "148C": "PowerColor", "1682": "XFX",
                           "1849": "ASRock", "10B0": "Gainward", "1ACC": "Point of View"}.get(gpu.get("board_vendor_id", ""), "")

    lhm = Lhm()
    sensors = {"lhm": lhm.poll(), "lhm_error": lhm.err}
    if sensors["lhm"]:
        c = lhm.cpu()
        sensors["cpu_temp"] = c.get("tdie") or c.get("tctl") or c.get("package_temp")
        sensors["gpu_mem_junction"] = lhm.gpu_extra().get("mem_junction")
        sensors["fans"] = lhm.board_fans()
    ram = d["ram"]
    kits = sorted({(r.get("maker") or "?", r.get("part") or "?") for r in ram})
    mem = {"sticks": len(ram), "total_gb": sum(r.get("gb") or 0 for r in ram),
           "kits": [{"maker": k[0], "part": k[1], "count": sum(1 for r in ram if (r.get("maker"), r.get("part")) == k)} for k in kits],
           "rated_mts": max([r.get("rated") or 0 for r in ram] or [0]),
           "running_mts": max([r.get("running") or 0 for r in ram] or [0]),
           "ddr": 5 if any((r.get("type") or 0) == 34 for r in ram) else 4 if any((r.get("type") or 0) == 26 for r in ram) else None}

    caps = {
        "gpu_tuning": bool(nv.get("present") and (nv.get("offsets") or {}).get("api")),
        "gpu_fans": bool(nv.get("present") and nv.get("fans")),
        "cpu_co": cpu["family"] in ("amd-zen5", "amd-zen5-x3d", "amd-zen4", "amd-zen4-x3d", "amd-zen3", "amd-zen3-x3d"),
        "cpu_temps": sensors.get("cpu_temp") is not None,
        "vram_temp": sensors.get("gpu_mem_junction") is not None,
        "pawnio": bool(d.get("pawnio")),
        "laptop": (d.get("system") or {}).get("type") == 2,
    }
    out = {"when": time.strftime("%Y-%m-%d %H:%M"), "seconds": round(time.time() - t0, 1),
           "cpu": cpu, "board": board, "gpu": gpu, "memory": mem, "ram_sticks": ram,
           "os": d.get("os"), "system": d.get("system"), "sensors": sensors,
           "cooler_hint": d.get("cooler_hint"), "caps": caps}
    write_json(SCAN_FILE, out)
    log("Hardware scan: %s | %s %s (BIOS %s) | %s | %d GB in %d sticks" % (
        cpu.get("name"), board.get("maker"), board.get("model"), board.get("bios"), gpu_name or "no GPU found",
        mem["total_gb"], mem["sticks"]))
    return out


if __name__ == "__main__":
    print(json.dumps(scan(), indent=2))
