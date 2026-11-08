"""Builds the active profile for this PC (profile.json) from the hardware scan.

For each part the best available source wins:
  1. research   - Claude web research for the exact parts (research.py), with sources
  2. builtin    - researched profiles shipped in profiles/builtin/ (exact model match)
  3. family     - conservative per-family guidance (profiles/families.json)

Whatever the source, safety limits are clamped to Headroom's hard caps here, so
neither a typo in a JSON file nor a research answer can loosen them.
"""
import glob
import os
import re
import time

from .common import BUILTIN_DIR, FAMILIES_FILE, PROFILE_FILE, read_json, write_json

# hard caps: nothing may exceed these
CAP = {
    "gpu_abort_core_c": (70, 85), "gpu_abort_mem_c": (80, 100),
    "gpu_max_core": 500, "gpu_max_mem_ab": 3000,
    "cpu_abort_amd": (70, 90), "cpu_abort_intel": (70, 95),
}
_VARIANT = {"ti", "super", "xt", "xtx", "x3d", "x", "gre", "f", "k", "kf", "ks"}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def name_matches(profile_name, hw_name):
    """'AMD Ryzen 7 9800X3D' matches 'AMD Ryzen 7 9800X3D 8-Core Processor', but
    'RTX 5070' must not match 'RTX 5070 Ti'."""
    p, h = _norm(profile_name), _norm(hw_name)
    if not p or not h:
        return False
    if p == h:
        return True
    i = h.find(p)
    end = i + len(p)
    if i < 0 or (i > 0 and h[i - 1] != " ") or (end < len(h) and h[end] != " "):
        return False   # must match whole words: '9800x' is not '9800x3d'
    rest = h[end:].split()
    return not rest or rest[0] not in _VARIANT


def builtin_profiles():
    out = []
    for f in sorted(glob.glob(os.path.join(BUILTIN_DIR, "*.json"))):
        d = read_json(f)
        if d and d.get("section") and d.get("name"):
            out.append(d)
    return out


def find_builtin(section, hw_name):
    hits = [b for b in builtin_profiles() if b["section"] == section and name_matches(b["name"], hw_name)]
    hits.sort(key=lambda b: -len(b["name"]))
    return hits[0] if hits else None


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def clamp_gpu(p):
    sf = dict(p.get("safety") or {})
    lo, hi = CAP["gpu_abort_core_c"]
    sf["abort_core_c"] = int(_clamp(sf.get("abort_core_c"), lo, hi, 83))
    sf["warn_core_c"] = int(min(_clamp(sf.get("warn_core_c"), 60, hi, sf["abort_core_c"] - 5), sf["abort_core_c"] - 3))
    lo, hi = CAP["gpu_abort_mem_c"]
    sf["abort_mem_junction_c"] = int(_clamp(sf.get("abort_mem_junction_c"), lo, hi, 96))
    sf["warn_mem_junction_c"] = int(min(_clamp(sf.get("warn_mem_junction_c"), 60, hi, sf["abort_mem_junction_c"] - 6),
                                        sf["abort_mem_junction_c"] - 3))
    sf["max_core_offset"] = int(_clamp(sf.get("max_core_offset"), 0, CAP["gpu_max_core"], 150))
    sf["max_mem_offset_reported_mhz"] = int(_clamp(sf.get("max_mem_offset_reported_mhz"), 0, CAP["gpu_max_mem_ab"], 500))
    sf.setdefault("notes", [])
    p["safety"] = sf
    return p


def clamp_cpu(p, vendor):
    sf = dict(p.get("safety") or {})
    lo, hi = CAP["cpu_abort_intel"] if vendor == "intel" else CAP["cpu_abort_amd"]
    sf["abort_tdie_c"] = int(_clamp(sf.get("abort_tdie_c"), lo, hi, hi))
    sf["warn_tdie_c"] = int(min(_clamp(sf.get("warn_tdie_c"), 60, hi, sf["abort_tdie_c"] - 5), sf["abort_tdie_c"] - 3))
    if "platform_thermal_limit_c" in sf:
        sf["platform_thermal_limit_c"] = int(_clamp(sf["platform_thermal_limit_c"], 70, 90, 85))
    sf.setdefault("notes", [])
    p["safety"] = sf
    return p


def memory_advice(scan):
    m = scan.get("memory") or {}
    fam = (scan.get("cpu") or {}).get("family", "")
    notes, verdict = [], "Headroom doesn't tune RAM. Your memory settings are left as they are."
    if m.get("ddr") == 5 and m.get("running_mts") and m["running_mts"] <= 4800:
        notes.append("Running at %d MT/s: EXPO/XMP is probably off. Turning it on in the BIOS is usually the biggest free "
                     "speed-up for games (check the kit's rated speed on its label)." % m["running_mts"])
    if m.get("ddr") == 4 and m.get("running_mts") and m["running_mts"] <= 2666:
        notes.append("Running at %d MT/s: XMP/DOCP is probably off. Turning it on in the BIOS is a free speed-up." % m["running_mts"])
    if m.get("sticks") == 4 and m.get("ddr") == 5:
        notes.append("4 DDR5 sticks (two per channel) are much harder to run fast than 2. If you see crashes, try a lower "
                     "memory speed before blaming the CPU or GPU tune.")
        if fam.startswith("amd") and (m.get("running_mts") or 0) >= 6000:
            verdict = "Leave alone: 4 sticks at %d MT/s is already well past AMD's official rating for two sticks per channel." % m["running_mts"]
    if len(m.get("kits") or []) > 1:
        notes.append("Mixed memory kits (%s). Mixed kits work, but are the first suspect if the PC is unstable." %
                     ", ".join("%dx %s %s" % (k["count"], k["maker"], k["part"]) for k in m["kits"]))
    label = "%d GB DDR%s (%d sticks)" % (m.get("total_gb") or 0, m.get("ddr") or "?", m.get("sticks") or 0)
    return label, {"detected": ", ".join("%dx %s %s" % (k["count"], k["maker"], k["part"]) for k in m.get("kits") or [])
                   + (" at %d MT/s" % m["running_mts"] if m.get("running_mts") else ""),
                   "verdict": verdict, "notes": notes}


def board_entry(scan, families, research_board=None, cpu_profile=None):
    b = scan.get("board") or {}
    name = (b.get("model") or "Motherboard").strip()
    if research_board:
        return name, dict(research_board, _source="research")
    bi = find_builtin("board", name)
    if bi:
        return name, dict(bi["profile"], _source="builtin")
    fam = (families.get("board") or {}).get(b.get("vendor") or "other") or families["board"]["other"]
    boost = ((cpu_profile or {}).get("typical") or {}).get("boost_override_mhz", 200)
    plat = ((cpu_profile or {}).get("safety") or {}).get("platform_thermal_limit_c", 85)
    rows = [["Precision Boost Overdrive", "Advanced"], ["PBO Limits", "Auto (do NOT raise)"],
            ["Precision Boost Overdrive Scalar", "Auto"]]
    if boost:
        rows += [["CPU Boost Clock Override", "Enabled (Positive)"], ["Max CPU Boost Clock Override", "+%d MHz" % boost]]
    rows += [[fam["thermal_limit_name"], "Manual -> %d" % plat], ["Curve Optimizer", "Per Core"]]
    return name, {"_source": "family", "vendor": fam["label"], "bios_key": fam["bios_key"], "pbo_path": fam["pbo_path"],
                  "settings_to_enter": rows,
                  "also": "Menu names differ between BIOS versions. 'Research my hardware' in Setup finds the exact names for your board."}


def build(scan, research=None, cooling=None):
    """research: {"gpu": {...}, "cpu": {...}, "board": {...}, "sources": [...]} from research.py (optional)."""
    families = read_json(FAMILIES_FILE, {}) or {}
    research = research or {}
    out = {"_about": "Active Headroom profile for this PC, built %s from the hardware scan." % time.strftime("%Y-%m-%d %H:%M"),
           "_sources": {}}

    g = scan.get("gpu") or {}
    gname = g.get("name") or "GPU"
    if research.get("gpu"):
        gp, src = dict(research["gpu"]), "research"
    elif find_builtin("gpu", gname):
        gp, src = dict(find_builtin("gpu", gname)["profile"]), "builtin"
    else:
        fam = (families.get("gpu") or {}).get(g.get("family") or "unknown") or families["gpu"]["unknown"]
        gp, src = dict(fam), "family"
    gp["family"] = g.get("family")
    gp["tuning_supported"] = bool((scan.get("caps") or {}).get("gpu_tuning")) and \
        bool((gp.get("safety") or {}).get("max_core_offset", 0) or (gp.get("safety") or {}).get("max_mem_offset_reported_mhz", 0))
    out["gpu"] = {gname: clamp_gpu(gp)}
    out["_sources"]["gpu"] = src

    c = scan.get("cpu") or {}
    cname = re.sub(r"\s+\d+-Core Processor$", "", (c.get("name") or "CPU").strip())
    if research.get("cpu"):
        cp, src = dict(research["cpu"]), "research"
    elif find_builtin("cpu", cname):
        cp, src = dict(find_builtin("cpu", cname)["profile"]), "builtin"
    else:
        fam = (families.get("cpu") or {}).get(c.get("family")) or families["cpu"]["other"]
        cp, src = dict(fam), "family"
    cp["family"] = c.get("family")
    cp["co_supported"] = bool((scan.get("caps") or {}).get("cpu_co"))
    out["cpu"] = {cname: clamp_cpu(cp, c.get("vendor"))}
    out["_sources"]["cpu"] = src

    bname, bp = board_entry(scan, families, research.get("board"), cp)
    out["board"] = {bname: bp}
    out["_sources"]["board"] = bp.get("_source")

    mlabel, mp = memory_advice(scan)
    rm = research.get("memory") or {}
    if rm.get("verdict"):
        mp = dict(mp, verdict=rm["verdict"], notes=(rm.get("notes") or []) + mp["notes"], sources=rm.get("sources") or [])
    out["memory"] = {mlabel: mp}

    cool = cooling or {}
    out["cooler"] = {cool.get("name") or "CPU cooler": {
        "pump_sensor": cool.get("pump_sensor"), "radiator_sensor": cool.get("radiator_sensor"),
        "preflight": {"pump_min_rpm": int(cool.get("pump_min_rpm") or 1000),
                      "radiator_min_rpm_under_load": int(cool.get("radiator_min_rpm") or 300)},
        "note": "Pick the pump / CPU fan headers in Setup so the CPU stress test stops if they fail."}}
    if research.get("sources"):
        out["_research"] = {"when": research.get("when"), "model": research.get("model"), "sources": research["sources"]}
    return out


def save(profile):
    profile = clamp_all(profile)
    write_json(PROFILE_FILE, profile)
    return profile


def _sanitize(v, key=""):
    """Profile text can come from web research: no markup, and links must be plain https."""
    if isinstance(v, str):
        if key in ("url", "source_url") or key.endswith("_url"):
            return v if re.match(r"^https://[^\s\"'<>]+$", v) else ""
        return v.replace("<", "\u2039").replace(">", "\u203a")
    if isinstance(v, list):
        if key == "sources":
            return [u for u in v if isinstance(u, str) and re.match(r"^https://[^\s\"'<>]+$", u)]
        return [_sanitize(x) for x in v]
    if isinstance(v, dict):
        return {(_sanitize(k) if isinstance(k, str) else k): _sanitize(x, k if isinstance(k, str) else "") for k, x in v.items()}
    return v


def clamp_all(prof):
    """Re-apply the hard caps to a profile read from disk (profile.json is user-writable)."""
    if not isinstance(prof, dict):
        return {}
    out = _sanitize(prof)
    if isinstance(out.get("gpu"), dict):
        out["gpu"] = {k: clamp_gpu(v) for k, v in out["gpu"].items() if isinstance(v, dict)}
    if isinstance(out.get("cpu"), dict):
        out["cpu"] = {k: clamp_cpu(v, v.get("vendor") or ("intel" if "intel" in (k + str(v.get("family") or "")).lower() else "amd"))
                      for k, v in out["cpu"].items() if isinstance(v, dict)}
    return out
