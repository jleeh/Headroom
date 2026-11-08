"""'Research my hardware': uses Claude with web search to find reviews, community
overclocking results, safety limits and exact BIOS menu names for the parts in
THIS PC, the same way Headroom's built-in profiles were researched.

Needs the user's own Anthropic API key (stored encrypted with Windows DPAPI, or
ANTHROPIC_API_KEY). Costs a little API credit per run; nothing is sent except
the hardware scan summary below (no serials, user names or files).

Two steps:
  1. research  - Claude searches/reads the web and writes notes. Every URL it
                 actually saw is collected from the search/fetch results.
  2. structure - the notes become a profile via structured JSON output. Sources
                 not seen in step 1 are dropped, so links can't be made up.
Safety limits from research are clamped by profiles.py like any other source.

Web text is untrusted (prompt injection), and the BIOS sheet is typed in by hand,
so to_profiles() sanitises every string and only lets allowlisted BIOS rows
through. A monthly spend cap guards the API backend. A second backend that uses
the user's own Claude Code login lives in research_cli.py; run_research() picks.
"""
# ruff: noqa: E501  (long prompt text and the BIOS allowlist table)
import base64
import ctypes
import ctypes.wintypes as W
import json
import math
import os
import re
import time
import unicodedata

from .common import SETTINGS_FILE, read_json, write_json

MODEL = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]   # fallbacks="default": a declined request is retried on another model
TOOLS = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 20},
         {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 12}]


# claude-opus-5-5 list prices, USD per million tokens / per 1,000 searches. Web fetch has no fee beyond tokens.
PRICE = {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0, "per_search": 10.0 / 1000}
MAX_TURNS = 6            # research turns incl. pause_turn resumes
MAX_TOKENS_RESEARCH, MAX_TOKENS_STRUCTURE = 64000, 16000
DEFAULT_CAP_USD = 10.0


class ResearchError(RuntimeError):
    pass


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise ResearchError("Cancelled")


# ---------------------------------------------------------------- cost, estimate, monthly cap
def cost_usd(u):
    """Dollar cost of a usage dict (input / cache_read / cache_write / output tokens kept separate)."""
    return round((u.get("input_tokens", 0) * PRICE["input"] + u.get("output_tokens", 0) * PRICE["output"]
                  + u.get("cache_read_tokens", 0) * PRICE["cache_read"] + u.get("cache_write_tokens", 0) * PRICE["cache_write"]) / 1e6
                 + u.get("web_searches", 0) * PRICE["per_search"], 4)


def estimate():
    """Cost guess shown before a run. typical = assumed token use of a normal run;
    max = every bound hit: all searches, MAX_TURNS full-length research turns."""
    searches = TOOLS[0]["max_uses"]
    typ = {"input_tokens": 250_000, "cache_read_tokens": 400_000, "output_tokens": 20_000, "web_searches": 12}
    # worst case: one research turn at its max_tokens (continuations resume the same work), the profile step at its
    # max_tokens, and three full 200k-token contexts billed uncached
    mx = {"input_tokens": 600_000, "output_tokens": MAX_TOKENS_RESEARCH + MAX_TOKENS_STRUCTURE, "web_searches": searches}
    return {"typical_usd": round(cost_usd(typ), 2), "max_usd": round(cost_usd(mx), 2),
            "assumptions": ("Typical: about 12 web searches, 250k fresh input tokens, 400k cached input tokens and 20k output tokens "
                            "at $4 / $0.20 / $20 per million tokens plus $10 per 1,000 searches. Maximum: %d searches, "
                            "%dk output tokens for the research plus %dk for the profile, and 600k uncached input tokens. "
                            "Web page reads cost only their tokens."
                            % (searches, MAX_TOKENS_RESEARCH // 1000, MAX_TOKENS_STRUCTURE // 1000))}


def _month():
    return time.strftime("%Y-%m")


def get_settings():
    s = read_json(SETTINGS_FILE, {}) or {}
    spend = s.get("research_spend") if isinstance(s.get("research_spend"), dict) else {}
    cap = s.get("research_cap_usd_month", DEFAULT_CAP_USD)
    cap = float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) and math.isfinite(cap) and cap >= 0 else DEFAULT_CAP_USD
    backend = s.get("research_backend") if s.get("research_backend") in ("api", "claude_cli") else "api"
    month = spend.get(_month())
    return {"research_backend": backend, "research_cap_usd_month": cap, "research_spend": spend,
            "research_spend_month_usd": round(float(month), 4) if isinstance(month, (int, float)) else 0.0}


def set_research_settings(d):
    """Validate and save research_backend / research_cap_usd_month (0 = no cap). Other keys are ignored."""
    s = read_json(SETTINGS_FILE, {}) or {}
    if "research_backend" in d:
        if d["research_backend"] not in ("api", "claude_cli"):
            raise ResearchError("research_backend must be 'api' or 'claude_cli'")
        s["research_backend"] = d["research_backend"]
    if "research_cap_usd_month" in d:
        v = d["research_cap_usd_month"]
        try:
            v = float(v)
        except (TypeError, ValueError) as e:
            raise ResearchError("The monthly cap must be a number of dollars (0 = no cap)") from e
        if isinstance(d["research_cap_usd_month"], bool) or not math.isfinite(v) or v < 0 or v > 10000:
            raise ResearchError("The monthly cap must be between 0 and 10000 dollars (0 = no cap)")
        s["research_cap_usd_month"] = round(v, 2)
    write_json(SETTINGS_FILE, s)
    return get_settings()


def check_budget():
    """Refuse to start a run that could take this month's spend over the cap."""
    g = get_settings()
    cap = g["research_cap_usd_month"]
    if not cap:
        return
    spent, worst = g["research_spend_month_usd"], estimate()["max_usd"]
    if spent + worst > cap:
        raise ResearchError("Monthly research budget: $%.2f spent this month, and a run can cost up to $%.2f, which would pass your "
                            "$%.2f cap. Raise 'research_cap_usd_month' (Setup > Research budget, or settings.json; 0 = no cap)."
                            % (spent, worst, cap))


def record_spend(usd):
    if not usd or usd <= 0:
        return
    s = read_json(SETTINGS_FILE, {}) or {}
    spend = s.get("research_spend") if isinstance(s.get("research_spend"), dict) else {}
    spend[_month()] = round(float(spend.get(_month(), 0) or 0) + usd, 4)
    s["research_spend"] = spend
    write_json(SETTINGS_FILE, s)


# ---------------------------------------------------------------- API key (DPAPI)
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", W.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    crypt = ctypes.windll.crypt32
    src = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    dst = _Blob()
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):   # 0x1 = UI forbidden
        raise ResearchError("Windows couldn't %s the API key" % ("encrypt" if protect else "decrypt"))
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def save_key(key):
    s = read_json(SETTINGS_FILE, {}) or {}
    if key:
        s["anthropic_key"] = base64.b64encode(_dpapi(key.strip().encode(), True)).decode()
    else:
        s.pop("anthropic_key", None)
    write_json(SETTINGS_FILE, s)


def load_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    blob = (read_json(SETTINGS_FILE, {}) or {}).get("anthropic_key")
    if not blob:
        return None
    try:
        return _dpapi(base64.b64decode(blob), False).decode()
    except Exception:  # noqa: BLE001
        return None


def has_key():
    return bool(load_key())


# ---------------------------------------------------------------- prompts
def scan_summary(scan):
    """Only what research needs: model names, IDs, versions. No serials or user data."""
    c, b, g, m = scan.get("cpu") or {}, scan.get("board") or {}, scan.get("gpu") or {}, scan.get("memory") or {}
    nv = g.get("nvidia") or {}
    return {
        "cpu": c.get("name"), "cpu_family": c.get("family"),
        "motherboard": "%s %s" % (b.get("maker") or "", b.get("model") or ""), "bios_version": b.get("bios"),
        "bios_date": b.get("bios_date"),
        "gpu": g.get("name"), "gpu_board_partner": g.get("board_vendor") or None,
        "gpu_pci_subsystem_vendor": g.get("board_vendor_id"), "gpu_vbios": nv.get("vbios"),
        "gpu_driver": nv.get("driver"), "gpu_power_limits_w": nv.get("power"), "gpu_max_clocks_mhz": nv.get("max_clock_mhz"),
        "gpu_vram_gb": nv.get("vram_gb"),
        "memory": {"kits": m.get("kits"), "sticks": m.get("sticks"), "running_mts": m.get("running_mts"), "ddr": m.get("ddr")},
        "os": (scan.get("os") or {}).get("name"),
    }


SYSTEM = """You research PC hardware for Headroom, a safety-first overclocking tool. Headroom only does:
- NVIDIA GPUs: core and memory clock offsets (no voltage changes, power limit left as the card allows), found by its own stress test.
- AMD Ryzen CPUs: PBO with stock power limits, a Max Boost Clock Override, and per-core negative Curve Optimizer found by CoreCycler. The user enters BIOS settings by hand from a sheet Headroom shows.
It never raises voltages or power limits and doesn't tune RAM.

Your findings set the starting points, expectations and safety limits Headroom uses, so accuracy matters more than completeness:
- Prefer professional reviews (TechPowerUp, Tom's Hardware, Guru3D, KitGuru, Hardware Unboxed, Gamers Nexus, ComputerBase, Igor's Lab), manufacturer spec pages and manuals, and well-documented community results (Overclock.net, r/overclocking).
- Match the exact model. A GPU's board partner and cooler (from the PCI subsystem vendor) matter: power limits and memory cooling differ between partner cards of the same GPU.
- When something isn't found, say so plainly. Never estimate a number and present it as a finding."""

RESEARCH_TASK = """Research overclocking and safety information for the parts in this PC:

{scan}

Find and report, with the source for each fact:
1. GPU: stock boost clock, memory type/speed, power limit (default and maximum) for THIS partner card, stable core / memory offsets that reviewers and owners reached (in MSI Afterburner units), the gains they measured in games, and safe GPU core and memory junction temperatures.
2. CPU: stock clocks and power limits (PPT/TDC/EDC or PL1/PL2), Tjmax, whether PBO / Curve Optimizer / boost override are available on this exact CPU, typical stable per-core Curve Optimizer values, measured gains, and known safety issues (e.g. SoC voltage limits, microcode or BIOS advisories).
3. Motherboard: the exact BIOS menu path and setting names for PBO, boost override, Curve Optimizer (per core) and the platform thermal limit on this board's current BIOS family; any BIOS versions to avoid or install.
4. Memory: whether this kit combination at the current speed is within the CPU maker's rating, and known issues.
Keep the notes factual and compact."""

STRUCTURE_TASK = """Turn these research notes into Headroom's profile JSON for this PC.

Hardware scan:
{scan}

Research notes:
{notes}

Sources that were actually retrieved (use ONLY these URLs, copied exactly; leave a url empty if none fits):
{sources}

Rules: use null for anything the notes don't support. Temperatures in C. GPU memory offsets in MSI Afterburner units. Curve Optimizer values are negative numbers. Expected uplift as percentages."""

_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_RANGE = {"type": "array", "items": {"type": "number"}}
_RESULTS = {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                       "required": ["source", "settings", "result", "url"],
                                       "properties": {"source": {"type": "string"}, "settings": {"type": "string"},
                                                      "result": {"type": "string"}, "url": {"type": "string"}}}}
_STRS = {"type": "array", "items": {"type": "string"}}


def _obj(props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


SCHEMA = _obj({
    "gpu": _obj({
        "board": _STR, "chip": _STR, "summary": {"type": "string"},
        "stock": _obj({"boost_mhz": _NUM, "mem_gbps": _NUM, "power_w": _NUM, "power_max_w": _NUM}),
        "community_results": _RESULTS,
        "core_offset_mhz": _RANGE, "mem_offset_afterburner": _RANGE,
        "games_uplift_pct": _RANGE, "games_uplift_typical_pct": _NUM,
        "max_safe_core_c": _NUM, "max_safe_mem_junction_c": _NUM,
        "notes": _STRS, "sources": _STRS}),
    "cpu": _obj({
        "arch": _STR, "summary": {"type": "string"},
        "stock": _obj({"base_mhz": _NUM, "boost_mhz": _NUM, "tdp_w": _NUM, "ppt_w": _NUM, "tjmax_c": _NUM}),
        "pbo_available": {"type": "boolean"}, "curve_optimizer_available": {"type": "boolean"},
        "boost_override_available": {"type": "boolean"},
        "community_results": _RESULTS, "co_per_core": _RANGE, "boost_override_mhz": _NUM,
        "single_thread_uplift_pct": _RANGE, "multi_thread_uplift_pct": _RANGE, "games_uplift_pct": _RANGE,
        "max_safe_temp_c": _NUM, "notes": _STRS, "sources": _STRS}),
    "board": _obj({
        "bios_key": _STR, "pbo_path": _STR,
        "settings_to_enter": {"type": "array", "items": _obj({"setting": {"type": "string"}, "value": {"type": "string"}})},
        "bios_advice": _STRS, "sources": _STRS}),
    "memory": _obj({"verdict": {"type": "string"}, "notes": _STRS, "sources": _STRS}),
})


# ---------------------------------------------------------------- helpers
def _dump(block):
    try:
        return block.to_dict()
    except AttributeError:
        return block if isinstance(block, dict) else {}


_SAFE_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]+\Z")


def safe_url(u):
    """Only plain URL characters: no quotes, brackets-in-HTML, spaces or backslashes that could break out of a page or shell."""
    return isinstance(u, str) and len(u) <= 500 and bool(_SAFE_URL.match(u))


def _collect_urls(obj, out):
    if isinstance(obj, dict):
        u = obj.get("url")
        if safe_url(u):
            out.setdefault(u, clean(obj.get("title"), 120))
        for v in obj.values():
            _collect_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_urls(v, out)


def _check_stop(msg, step):
    if msg.stop_reason == "refusal":
        cat = getattr(getattr(msg, "stop_details", None), "category", None)
        raise ResearchError("Claude declined the %s step%s." % (step, " (%s)" % cat if cat else ""))


def _rng(v, default=None):
    if isinstance(v, list) and len(v) >= 2 and all(isinstance(x, (int, float)) for x in v[:2]):
        return [v[0], v[1]]
    return default


def _keep(urls, allowed):
    return [u for u in (urls or []) if u in allowed and safe_url(u)]


# ---------------------------------------------------------------- sanitising untrusted research text
def clean(v, n=300):
    """Plain one-line text: control/format characters (incl. newlines, bidi tricks) -> space, whitespace collapsed, capped."""
    if not isinstance(v, str):
        return "" if v is None else clean(str(v), n)
    v = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Zl", "Zp") else c for c in v)
    return " ".join(v.split())[:n]


def clean_text(v, n=20000):
    """Like clean() but keeps line breaks (for the notes shown to the user)."""
    return "\n".join(clean(line, 2000) for line in str(v or "").splitlines())[:n]


def _clean_list(v, n, count=12):
    return [t for t in (clean(x, n) for x in (v if isinstance(v, list) else [])[:count]) if t]


# BIOS rows. Research text came from web pages and ends up on a sheet the user types into the BIOS, so a row is kept only
# if its name matches something Headroom itself tunes AND its value fits that setting's safe shape. Else it is shown as rejected.
_VOLT = re.compile(r"\b(v|mv)\b|\d\s*m?v\b|volt|\bv(soc|ddcr|ddg|ddp|dd|core|cc|dimm|pp|tt|id)(?![a-z])|\bsvi\d?\b|\bllc\b|load.?line|vsoc|vddcr", re.I)
_POWER = re.compile(r"\b(ppt|tdc|edc|pl1|pl2|tau)\b|package power|socket power", re.I)
_MEM = re.compile(r"\b(ram|dram|memory|xmp|expo|docp|timings?|trcd|trp|tras|tcl|trfc|fclk|uclk|mclk|ddr\d?|mt/s)\b|\bcl\d", re.I)
_SAFE_VAL = re.compile(r"[A-Za-z0-9 +\-./()%>,:]{1,200}\Z")
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")


def _num_le(v, hi):
    m = _NUMBER.search(v)
    return bool(m) and 0 <= abs(float(m.group())) <= hi


def _lead(v, words):
    return re.match(r"(%s)\b" % "|".join(words), v.strip(), re.I) is not None


# (name pattern, value check, what the value may be). Checked in order; first name match decides.
_BIOS_RULES = [
    (r"curve optimi[sz]er (sign|direction)", lambda v: _lead(v, ["negative", "auto"]), "Negative"),
    (r"curve optimi[sz]er|\bco\b (magnitude|value)|core \d+ curve|all cores? curve|per cores? curve",
     lambda v: not re.search(r"\+\s*\d|positive", v, re.I) and (_lead(v, ["per core", "all cores?", "auto", "disabled", "negative", "enabled"])
                                                                    or (bool(_NUMBER.match(v.strip())) and _num_le(v, 60))), "Per Core / Negative n"),
    (r"pbo limits?\b|\bpbo (power )?limits?\b", lambda v: _lead(v, ["auto", "disabled", "stock"]), "Auto (do not raise)"),
    (r"(precision boost overdrive|\bpbo\b) scalar|\bpbo scalar", lambda v: _lead(v, ["auto"]) or re.match(r"1\s*x\b", v.strip(), re.I) is not None, "Auto / 1x"),
    (r"max(imum)? cpu boost clock override|cpu boost clock override\s*\(\+\)|boost clock override (value|frequency)",
     lambda v: _num_le(v, 200), "0-200 MHz"),
    (r"cpu boost clock override|boost clock override", lambda v: _lead(v, ["enabled", "disabled", "auto", "positive"]), "Enabled (Positive)"),
    (r"precision boost overdrive|\bpbo\b", lambda v: _lead(v, ["enabled", "disabled", "auto", "advanced", "manual"]), "Advanced / Enabled / Auto"),
    (r"platform thermal throttle limit|thermal throttle limit|(cpu |platform |package )?thermal limit|\btctl\b.*limit|thermal control limit",
     lambda v: _lead(v, ["auto", "manual", "disabled"]) and (not _NUMBER.search(v) or _num_le(v, 90)) or (_NUMBER.match(v.strip()) and _num_le(v, 90)),
     "Auto / Manual -> up to 90 C"),
    (r"amd overclocking", lambda v: _lead(v, ["accept", "accepted", "ok", "enter", "enabled", "agree", "yes"]), "Accept"),
]
_BIOS_RULES = [(re.compile(a, re.I), f, d) for a, f, d in _BIOS_RULES]


def vet_bios_row(setting, value):
    """None if the row may go on the BIOS sheet, else the reason it was rejected."""
    both = "%s %s" % (setting, value)
    if not setting or not value:
        return "empty row"
    if _VOLT.search(both):
        return "mentions voltage (Headroom never changes voltages)"
    if _POWER.search(both) and re.search(r"\d", value):
        return "sets a power limit (Headroom leaves power limits stock)"
    if re.search(r"motherboard|\bmanual\b", value, re.I) and re.search(r"limit|ppt|tdc|edc", setting, re.I) and "thermal" not in setting.lower():
        return "raises power limits"
    if _MEM.search(both):
        return "memory setting (Headroom doesn't tune RAM)"
    if re.search(r"curve|\bco\b", setting, re.I) and re.search(r"positive|\+\s*\d", value, re.I):
        return "positive Curve Optimizer value"
    if not _SAFE_VAL.match(value) or not _SAFE_VAL.match(setting):
        return "unusual characters"
    for pat, ok, want in _BIOS_RULES:
        if pat.search(setting):
            return None if ok(value.strip()) else "value isn't a safe one for this setting (expected %s)" % want
    return "not a setting Headroom uses"


def _bios_rows(rows):
    ok, bad = [], []
    for r in (rows if isinstance(rows, list) else [])[:40]:
        r = r if isinstance(r, dict) else {}
        st, val = clean(r.get("setting"), 80), clean(r.get("value"), 60)
        why = vet_bios_row(st, val)
        (bad if why else ok).append([st, val, why] if why else [st, val])
    return ok, bad


def to_profiles(j, allowed):
    """Structured answer -> Headroom's profile schema (same shape as profiles/builtin)."""
    g, c, b, m = j.get("gpu") or {}, j.get("cpu") or {}, j.get("board") or {}, j.get("memory") or {}
    def res(rows):
        out = []
        for r in (rows if isinstance(rows, list) else [])[:12]:
            r = r if isinstance(r, dict) else {}
            u = r.get("url")
            out.append({"source": clean(r.get("source"), 120), "settings": clean(r.get("settings"), 300),
                        "result": clean(r.get("result"), 300), "url": u if u in allowed and safe_url(u) else ""})
        return out
    gpu = {
        "board": clean(g.get("board"), 120) or None, "chip": clean(g.get("chip"), 120) or None, "summary": clean(g.get("summary"), 1000),
        "stock": {k: v for k, v in (g.get("stock") or {}).items() if v is not None},
        "tuning_method": "Core + memory clock offsets via NVML (no voltage change; the card keeps its own power limit).",
        "community_results": res(g.get("community_results")),
        "typical": {"core_offset_mhz": _rng(g.get("core_offset_mhz"), [0, 0]),
                    "mem_offset_afterburner": _rng(g.get("mem_offset_afterburner"), [0, 0])},
        "expected_uplift_pct": {"games": _rng(g.get("games_uplift_pct"), [0, 0]),
                                "games_typical": g.get("games_uplift_typical_pct") or 0},
        "safety": {
            "abort_core_c": min(83, (g.get("max_safe_core_c") or 88) - 5),
            "abort_mem_junction_c": min(100, (g.get("max_safe_mem_junction_c") or 105) - 9),
            # the search may go a bit past what owners reached, never past the hard caps (profiles.CAP)
            "max_core_offset": int(max(_rng(g.get("core_offset_mhz"), [0, 150])[1] * 1.15, 50)),
            "max_mem_offset_reported_mhz": int(max(_rng(g.get("mem_offset_afterburner"), [0, 500])[1] * 1.1, 0)),
            "notes": _clean_list(g.get("notes"), 400)},
        "sources": _keep(g.get("sources"), allowed),
    }
    tj = (c.get("stock") or {}).get("tjmax_c") or c.get("max_safe_temp_c")
    cpu = {
        "arch": clean(c.get("arch"), 120) or None, "summary": clean(c.get("summary"), 1000),
        "stock": {k: v for k, v in (c.get("stock") or {}).items() if v is not None},
        "tuning_method": ("PBO + per-core negative Curve Optimizer (CoreCycler)" + (" + Max Boost Clock Override"
                          if c.get("boost_override_available") else "") + ". Power limits stay stock.")
        if c.get("curve_optimizer_available") else "No Curve Optimizer on this CPU: monitoring and benchmark only.",
        "community_results": res(c.get("community_results")),
        "typical": {"co_per_core": _rng(c.get("co_per_core"), [0, 0]),
                    "boost_override_mhz": int(c.get("boost_override_mhz") or 0) if c.get("boost_override_available") else 0},
        "expected_uplift_pct": {"single_thread": _rng(c.get("single_thread_uplift_pct"), [0, 0]),
                                "multi_thread": _rng(c.get("multi_thread_uplift_pct"), [0, 0]),
                                "games_cpu_bound": _rng(c.get("games_uplift_pct"), [0, 0]), "games_gpu_bound": [0, 1]},
        "safety": {"abort_tdie_c": (tj - 5) if tj else 90, "platform_thermal_limit_c": min(85, (tj - 10) if tj else 85),
                   "notes": _clean_list(c.get("notes"), 400)},
        "sources": _keep(c.get("sources"), allowed),
    }
    rows, rejected = _bios_rows(b.get("settings_to_enter"))
    advice = []
    for t in _clean_list(b.get("bios_advice"), 400, 8):   # free text is shown too: nothing that tells the user to change voltages etc.
        why = "mentions voltage" if _VOLT.search(t) else "memory setting" if _MEM.search(t) else "power limit" if _POWER.search(t) else None
        (rejected.append(["BIOS advice", t[:60], why]) if why else advice.append(t))
    board = {
        "bios_key": clean(b.get("bios_key"), 120) or None, "pbo_path": clean(b.get("pbo_path"), 160) or None,
        "settings_to_enter": rows, "rejected": rejected,
        "also": " ".join(advice), "sources": _keep(b.get("sources"), allowed),
    }
    memory = {"verdict": clean(m.get("verdict"), 500), "notes": _clean_list(m.get("notes"), 400), "sources": _keep(m.get("sources"), allowed)}
    return {"gpu": gpu, "cpu": cpu, "board": board, "memory": memory}


# ---------------------------------------------------------------- main
def _new_usage():
    return {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0,
            "web_searches": 0, "web_fetches": 0, "cost_usd": 0.0}


def _add_usage(usage, msg):
    """input_tokens = uncached input only; cache reads/writes are priced differently so stay separate."""
    u = msg.usage
    usage["input_tokens"] += u.input_tokens or 0
    usage["cache_read_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
    usage["cache_write_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    usage["output_tokens"] += u.output_tokens or 0
    st = getattr(u, "server_tool_use", None)
    if st:
        usage["web_searches"] += getattr(st, "web_search_requests", 0) or 0
        usage["web_fetches"] += getattr(st, "web_fetch_requests", 0) or 0
    usage["cost_usd"] = cost_usd(usage)


def _research_step(client, summary, urls, notes, usage, progress, cancel):
    """Step 1. On pause_turn the server wants us to send its partial turn back as an assistant message and call again,
    so messages grows user, assistant, assistant... and nothing already found is dropped."""
    messages = [{"role": "user", "content": RESEARCH_TASK.format(scan=summary)}]
    searches = fetches = 0
    for _ in range(MAX_TURNS):
        _cancelled(cancel)
        with client.beta.messages.stream(
                model=MODEL, max_tokens=MAX_TOKENS_RESEARCH, betas=BETAS, fallbacks="default",
                system=SYSTEM, thinking={"type": "adaptive"}, output_config={"effort": "high"},
                tools=TOOLS, messages=messages) as stream:
            for ev in stream:
                _cancelled(cancel)   # leaving the with-block closes the HTTP stream
                if ev.type == "content_block_start":
                    t = ev.content_block.type
                    if t in ("web_search_tool_result", "web_fetch_tool_result"):
                        searches += t == "web_search_tool_result"
                        fetches += t == "web_fetch_tool_result"
                        progress("Researching: %d searches, %d pages read" % (searches, fetches))
            msg = stream.get_final_message()
        _add_usage(usage, msg)
        _check_stop(msg, "research")
        for bl in (_dump(b) for b in msg.content):
            if bl.get("type") in ("web_search_tool_result", "web_fetch_tool_result") or bl.get("citations"):
                _collect_urls(bl, urls)
            if bl.get("type") == "text" and bl.get("text"):
                notes.append(bl["text"])
        if msg.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": msg.content})


def run(scan, progress=lambda msg: None, key=None, cancel=None):
    """Returns {"gpu","cpu","board","memory","sources","when","model","usage"}. Raises ResearchError.
    cancel: threading.Event; set it to stop (raises ResearchError("Cancelled")). Spend is recorded even on failure."""
    try:
        import anthropic
    except ImportError as e:
        raise ResearchError("The Anthropic SDK is missing from this install (%s)." % e) from e
    key = key or load_key()
    if not key:
        raise ResearchError("No Anthropic API key. Add one in Setup (console.anthropic.com > API keys).")
    check_budget()
    client = anthropic.Anthropic(api_key=key, timeout=900.0, max_retries=3)
    summary = json.dumps(scan_summary(scan), indent=1)
    urls, notes, usage = {}, [], _new_usage()
    try:
        progress("Researching your parts on the web (takes a few minutes)")
        _research_step(client, summary, urls, notes, usage, progress, cancel)
        if not notes:
            raise ResearchError("The research step returned no findings.")
        _cancelled(cancel)

        # ---- step 2: notes -> profile JSON
        progress("Building your profile from %d sources" % len(urls))
        src_list = "\n".join("- %s  (%s)" % (u, t) for u, t in urls.items()) or "(none)"
        msg = client.beta.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS_STRUCTURE, betas=BETAS, fallbacks="default",
            system=SYSTEM, output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": STRUCTURE_TASK.format(scan=summary, notes="\n\n".join(notes), sources=src_list)}])
        _add_usage(usage, msg)
        _check_stop(msg, "profile")
        if msg.stop_reason == "max_tokens":
            raise ResearchError("The profile answer was cut off; try again.")
        text = next((b.text for b in msg.content if b.type == "text"), "")
        j = json.loads(text)
        if not isinstance(j, dict):
            raise ValueError("not an object")
    except ResearchError:
        raise
    except anthropic.AuthenticationError as e:
        raise ResearchError("The API key was not accepted.") from e
    except anthropic.PermissionDeniedError as e:
        raise ResearchError("This API key can't use %s or web search (%s)." % (MODEL, e.message)) from e
    except anthropic.RateLimitError as e:
        raise ResearchError("Rate limited by the Anthropic API; try again in a minute.") from e
    except anthropic.APIStatusError as e:
        raise ResearchError("Anthropic API error %s: %s" % (e.status_code, e.message)) from e
    except anthropic.APIConnectionError as e:
        raise ResearchError("Couldn't reach the Anthropic API (check the internet connection).") from e
    except ValueError as e:
        raise ResearchError("The profile answer wasn't valid JSON (%s)." % e) from e
    finally:
        record_spend(usage["cost_usd"])   # cancelled/failed runs were still billed for the turns that completed

    allowed = set(urls)
    out = to_profiles(j, allowed)
    out.update(sources=sorted(allowed), when=time.strftime("%Y-%m-%d %H:%M"), model=MODEL, usage=usage,
               notes_text="\n\n".join(clean_text(n, 20000) for n in notes), backend="api")
    return out


def run_research(scan, progress=lambda msg: None, cancel=None):
    """Backend picked by settings "research_backend": "api" (default; capped by the monthly budget) or "claude_cli"."""
    if get_settings()["research_backend"] == "claude_cli":
        from . import research_cli
        return research_cli.run(scan, progress, cancel)
    return run(scan, progress, cancel=cancel)
