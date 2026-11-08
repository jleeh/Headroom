"""Headless UI smoke test: every tab rendered against seeded data, no console errors,
no dialogs (the seed has XSS payloads in research text), expected content present.

    python dev/ui_seed.py <datadir>
    HEADROOM_DATA=<datadir> HEADROOM_TOKEN=tok123 HEADROOM_PORT=7899 python bin/headroom_server.pyw &
    uv run --with playwright==1.42.0 python dev/ui_smoke.py <screenshot dir>
"""
import json
import sys

from playwright.sync_api import sync_playwright

out = sys.argv[1]
errs, dialogs, blocked = [], [], []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 1420, "height": 900}, extra_http_headers={"X-Headroom-Token": "tok123"})
    pg = ctx.new_page()
    pg.on("console", lambda m: (errs if m.type == "error" else []).append(m.text))
    pg.on("pageerror", lambda e: errs.append("PAGEERROR " + str(e)))
    pg.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    pg.goto("http://127.0.0.1:7899/")
    pg.wait_for_timeout(2500)
    res = {}
    for tab in ["dash", "gpu", "fans", "cpu", "prof", "bench", "log", "setup"]:
        pg.click('#tabs span[data-tab="%s"]' % tab)
        pg.wait_for_timeout(1300)
        pg.screenshot(path="%s/tab-%s.png" % (out, tab))
        res[tab] = pg.inner_text("#v-" + tab).lower()
    checks = {
        "recheck banner": "re-check recommended" in res["gpu"],
        "crash table": "gpu driver reset" in res["gpu"] and "core +180 -> +165" in res["gpu"],
        "cpu core hint": "core 3" in res["gpu"],
        "game result": "eldenring.exe" in res["bench"] and "+5.5%" in res["bench"],
        "rejected rows": "left off the bios sheet" in res["prof"] and "vsoc" in res["prof"],
        "intel advisory": "0x129" in res["cpu"],
        "research estimate": "per run" in res["setup"],
        "update section": "check for updates" in res["setup"],
        "xss text shown inert": "img src=x" in res["prof"],
        "no javascript: links": pg.evaluate("[...document.querySelectorAll('a')].every(a=>!/^javascript:/i.test(a.getAttribute('href')||''))"),
        "evil source dropped": pg.evaluate("![...document.querySelectorAll('a')].some(a=>(a.getAttribute('href')||'').includes('evil'))"),
    }
    print(json.dumps({"console_errors": errs, "dialogs": dialogs, "checks": checks}, indent=1))
    b.close()
