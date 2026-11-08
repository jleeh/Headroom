"""Seed a HEADROOM_DATA folder for dev/ui_smoke.py: a tuned GPU, a re-check, crashes, a game result,
and research text containing markup/JS (must render inert)."""
import json
import os
import sys
import time

d = sys.argv[1]
now = time.time()
state = {"gpu": {"result": {"core": 165, "mem": 1500, "mem_ratio": 0.5, "mem_afterburner": 750, "when": "2026-11-01 20:10",
                            "tuned": {"eff_clk": 2950, "gbps": 960},
                            "uplift": {"measured": {"core_clock_pct": 5.1, "compute_pct": 4.8, "mem_bandwidth_pct": 6.2, "power_w": [280, 295], "temp_c": [64, 67]},
                                       "estimated_games_pct": 5.5, "estimated_range_pct": [3.5, 7.0]}, "driver": "581.12"},
                 "baseline": {"eff_clk": 2810, "gbps": 904}, "applied": {"core": 165, "mem": 1500},
                 "recheck": {"due": True, "reason": "driver changed from 580.97 to 581.12"}},
         "games": [{"process": "eldenring.exe", "when": "2026-11-07 21:00", "rounds": 3, "seconds": 20, "noise_pct": 1.1,
                    "stock": {"frames": 3500, "avg_fps": 58.2, "low1": 47.0, "low01": 40.0}, "tuned": {"frames": 3600, "avg_fps": 61.4, "low1": 49.5, "low01": 41},
                    "uplift_pct": {"avg_fps": 5.5, "low1": 5.3}, "segments": [], "verdict": "+5.5% average FPS, above the 1.1% noise"}],
         "cpu": {"phase": "baseline"}}
json.dump(state, open(os.path.join(d, "state.json"), "w"))
stab = {"config": {"auto_backoff": True, "max_crashes": 3, "window_days": 7, "monitor_games_only": False}, "seen": [],
        "events": [{"t": now - 3600, "kind": "gpu_tdr", "suspect": "gpu", "detail": "nvlddmkm 153", "bugcheck": None, "core": None,
                    "applied": {"core": 180, "mem": 1500}, "action": "core +180 -> +165", "counts": True},
                   {"t": now - 7200, "kind": "whea_corrected", "suspect": "cpu", "detail": "WHEA 19", "core": 3, "action": "recorded (CPU: BIOS)"}]}
json.dump(stab, open(os.path.join(d, "stability.json"), "w"))
prof = {"_about": "test", "_sources": {"gpu": "research", "cpu": "family", "board": "research"},
        "gpu": {"NVIDIA GeForce RTX 5070 Ti": {"safety": {"abort_core_c": 83, "max_core_offset": 300, "max_mem_offset_reported_mhz": 1500, "notes": ["<b>x</b>"]},
                                               "community_results": [{"source": "TPU <img src=x onerror=alert(1)>", "settings": "+300", "result": "+6%", "url": "javascript:alert(1)"}],
                                               "sources": ["https://www.techpowerup.com/review/x", "http://evil\"onmouseover=alert(1)"]}},
        "cpu": {"13th Gen Intel(R) Core(TM) i9-13900K": {"family": "intel-raptorlake", "tuning_method": "No automatic tuning.",
                                                         "advisory": {"level": "bad", "text": "This CPU runs microcode 0x129, older than 0x12B"}, "safety": {"notes": []}}},
        "board": {"ROG STRIX B650E-F": {"settings_to_enter": [["Precision Boost Overdrive", "Advanced"]],
                                         "rejected": [["VSOC", "1.35V", "voltage settings are never changed"]], "also": "<script>alert(1)</script>"}},
        "memory": {"32 GB DDR5": {"verdict": "fine"}}, "cooler": {"CPU cooler": {}}}
json.dump(prof, open(os.path.join(d, "profile.json"), "w"))
json.dump({"cpu": {"name": "13th Gen Intel(R) Core(TM) i9-13900K", "family": "intel-raptorlake"}, "board": {"model": "ROG STRIX B650E-F"},
           "memory": {"total_gb": 32, "ddr": 5, "running_mts": 6000, "sticks": 2}, "caps": {"gpu_tuning": True, "gpu_fans": True}, "when": "2026-11-01"},
          open(os.path.join(d, "scan.json"), "w"))
