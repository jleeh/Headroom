# ruff: noqa: E501
"""'Research with Claude' through the user's own Claude Code login (`claude -p`) instead of an API key.

Headroom's backend is ELEVATED; claude must not be, since a prompt-injected agent with admin rights would be
dangerous. So the elevated side (run) only prepares a run folder, starts a one-shot scheduled task that runs
headroom_research_cli.pyw as the logged-on user with a LIMITED token, and then polls files that the unelevated side
writes. Everything read back from that folder is untrusted: parsed defensively, size-capped, and the source URLs are
recomputed here from the raw claude stream, never taken from a summary the other side wrote.

Folder (DATA_DIR/research_run/<timestamp>): request.json (in), status.json, stream1.jsonl, structure.json, done.json or
error.json (out), cancel (flag written by us; the runner then kills claude).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from . import research
from .common import APP_DIR, DATA_DIR, NO_WINDOW
from .research import ResearchError

TASK = "Headroom Research"
STEP_TIMEOUT = 20 * 60          # per claude invocation
START_TIMEOUT = 90              # runner must write status.json by then
MAX_SEARCHES, MAX_FETCHES = 40, 30   # claude -p has no --max-turns in current builds: the runner stops runaway research itself
READ_CAP = 2_000_000            # bytes read back from any file in the run folder
STREAM_CAP = 40_000_000
NOTE = ("Uses your Claude Code login. Counts against your Claude plan's usage limits. Anthropic's terms restrict using "
        "subscription logins to power third-party products; this option drives your own local Claude Code install, "
        "check the current terms before relying on it.")
RUN_ROOT = os.path.join(DATA_DIR, "research_run")


# ---------------------------------------------------------------- finding claude
def _default_env():
    return {k: os.environ.get(k) or "" for k in ("USERPROFILE", "APPDATA", "LOCALAPPDATA")}


def find_claude(env=None, which=shutil.which, exists=os.path.isfile):
    """First existing claude launcher: the native installer's, npm's, a per-user program install, then PATH."""
    env = env if env is not None else _default_env()
    cands = []
    if env.get("USERPROFILE"):
        cands.append(os.path.join(env["USERPROFILE"], ".local", "bin", "claude.exe"))
    if env.get("APPDATA"):
        cands.append(os.path.join(env["APPDATA"], "npm", "claude.cmd"))
    if env.get("LOCALAPPDATA"):
        cands.append(os.path.join(env["LOCALAPPDATA"], "Programs", "claude", "claude.exe"))
    for c in cands:
        if exists(c):
            return c
    return which("claude")


def available(env=None, which=shutil.which, exists=os.path.isfile):
    p = find_claude(env, which, exists)
    return {"installed": bool(p), "path": p, "note": NOTE}


def _argv(path, args):
    """argv for claude without a shell. A .cmd shim is run through cmd.exe, which would interpret & | ^ % in our
    arguments, so use the exe the npm package carries, or refuse when an argument has such characters."""
    if path.lower().endswith((".cmd", ".bat")):
        base = os.path.join(os.path.dirname(path), "node_modules", "@anthropic-ai", "claude-code")
        for rel in (("bin", "claude.exe"), ("cli.js",)):
            c = os.path.join(base, *rel)
            if os.path.isfile(c):
                return ([shutil.which("node") or "node", c] if c.endswith(".js") else [c]) + list(args)
        if any(re.search(r'[&|<>^%!"\r\n]', a) for a in args):
            raise ResearchError("Claude Code is installed as an npm .cmd launcher, which can't be started safely with these "
                                "arguments. Install the native version (see claude.com/download) and try again.")
    return [path] + list(args)


# ---------------------------------------------------------------- stream-json parsing (pure)
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\`]+")


def _norm(name):
    return re.sub(r"[^a-z]", "", str(name).lower())


def new_state():
    return {"pending": {}, "urls": {}, "searches": 0, "fetches": 0, "result": None, "text": []}


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return ""


def _urls_from_search(text, out):
    """WebSearch result text: 'Links: [{"title":..,"url":..}, ...]' plus prose; prefer the JSON, fall back to scanning."""
    found = []
    i = text.find("Links:")
    if i >= 0:
        j = text.find("[", i)
        if j >= 0:
            try:
                arr, _ = json.JSONDecoder().raw_decode(text[j:])
                found = [(d.get("url"), d.get("title")) for d in arr if isinstance(d, dict)]
            except ValueError:
                found = []
    if not found:
        found = [(u.rstrip(".,;:)]}"), "") for u in _URL_IN_TEXT.findall(text)]
    for u, t in found:
        if research.safe_url(u):
            out.setdefault(u, research.clean(t, 120))


def feed(state, line):
    """Update state from one stream-json line (one JSON object per line: system | assistant | user | result).
    Counts searches/fetches, collects retrieved URLs, keeps the final result. Unknown/garbled lines are ignored."""
    try:
        ev = json.loads(line)
    except ValueError:
        return state
    if not isinstance(ev, dict):
        return state
    t = ev.get("type")
    content = (ev.get("message") or {}).get("content") if isinstance(ev.get("message"), dict) else None
    if t == "assistant" and isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and _norm(b.get("name")) in ("websearch", "webfetch"):
                state["pending"][str(b.get("id"))] = (_norm(b["name"]), b.get("input") if isinstance(b.get("input"), dict) else {})
            elif b.get("type") == "text" and isinstance(b.get("text"), str):
                state["text"].append(b["text"])
    elif t == "user" and isinstance(content, list):
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            call = state["pending"].pop(str(b.get("tool_use_id")), None)
            if not call or b.get("is_error"):
                continue
            kind, inp = call
            if kind == "websearch":
                state["searches"] += 1
                _urls_from_search(_result_text(b.get("content")), state["urls"])
            else:
                state["fetches"] += 1
                # only the page asked for: links inside a fetched page were not retrieved themselves
                if research.safe_url(inp.get("url")):
                    state["urls"].setdefault(inp["url"], "")
    elif t == "result":
        state["result"] = ev
    return state


def parse_stream(lines):
    st = new_state()
    for ln in lines:
        feed(st, ln)
    return st


def extract_json(text):
    """JSON object from a result text: plain, in a ``` fence, or the first {...} in prose."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    for cand in ([m.group(1)] if m else []) + [text]:
        try:
            j = json.loads(cand)
            return j if isinstance(j, dict) else None
        except ValueError:
            i = cand.find("{")
            if i >= 0:
                try:
                    j, _ = json.JSONDecoder().raw_decode(cand[i:])
                    return j if isinstance(j, dict) else None
                except ValueError:
                    pass
    return None


def structured_from(result):
    """Profile JSON from a final result object: structured_output when --json-schema produced it, else JSON in the text."""
    if not isinstance(result, dict):
        return None
    so = result.get("structured_output")
    if isinstance(so, dict):
        return so
    if isinstance(so, str):
        return extract_json(so)
    return extract_json(result.get("result"))


def load_output_json(text):
    """`--output-format json` prints one result object (an array of events when verbose): return the result object."""
    try:
        j = json.loads(text)
    except ValueError:
        # last complete line that parses, in case something else printed first
        j = None
        for ln in reversed((text or "").splitlines()):
            try:
                j = json.loads(ln)
                break
            except ValueError:
                continue
    if isinstance(j, list):
        j = next((x for x in reversed(j) if isinstance(x, dict) and x.get("type") == "result"), None)
    return j if isinstance(j, dict) else None


def friendly_error(text, default="Claude Code failed"):
    t = (text or "").lower()
    if re.search(r"invalid api key|/login|not logged in|please run .?login|authentication|oauth|unauthorized|401", t):
        return "Claude Code isn't logged in. Open a terminal, run `claude`, then `/login`, and try again."
    if re.search(r"usage limit|limit reached|rate limit|resets? (at|in)|quota|overloaded|429", t):
        return "Your Claude plan's usage limit has been reached. Try again after it resets (%s)." % research.clean(text, 120)
    if "unknown option" in t or "unknown argument" in t:
        return "This version of Claude Code rejected an option Headroom uses (%s). Update Claude Code and try again." % research.clean(text, 160)
    return "%s: %s" % (default, research.clean(text, 300) or "no details")


# ---------------------------------------------------------------- request / args
def research_args(model, system):
    return ["-p", "--model", model, "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
            "--permission-mode", "dontAsk", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
            "--no-session-persistence", "--output-format", "stream-json", "--verbose", "--append-system-prompt", system]


def structure_args(model, system, schema):
    return ["-p", "--model", model, "--tools", "", "--permission-mode", "dontAsk", "--strict-mcp-config", "--setting-sources", "",
            "--disable-slash-commands", "--no-session-persistence", "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")), "--append-system-prompt", system]


def build_request(scan):
    summary = json.dumps(research.scan_summary(scan), indent=1)
    return {"model": research.MODEL, "system": research.SYSTEM, "schema": research.SCHEMA,
            "research_prompt": research.RESEARCH_TASK.format(scan=summary),
            "structure_prompt": research.STRUCTURE_TASK.format(scan=summary, notes="@@NOTES@@", sources="@@SOURCES@@")}


# ---------------------------------------------------------------- unelevated runner (headroom_research_cli.pyw)
def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW, timeout=15)
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _clean_env():
    """Never let an API key in the environment silently switch claude from the user's login to billed API use."""
    return {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}


def _invoke(argv, prompt, run_dir, stream_file, on_line, timeout, popen=subprocess.Popen, stop=lambda: False):
    """Run claude; stdout goes to stream_file line by line (and on_line). Returns (stderr_text, reason, returncode)
    where reason is None, 'timeout', 'cancelled' or 'limit' (stop() said so)."""
    work = os.path.join(run_dir, "work")
    os.makedirs(work, exist_ok=True)
    err_path = os.path.join(run_dir, os.path.basename(stream_file) + ".err")
    with open(err_path, "w", encoding="utf-8", errors="replace") as ef, open(stream_file, "w", encoding="utf-8") as out:
        proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=ef, cwd=work, env=_clean_env(), text=True,
                     encoding="utf-8", errors="replace", creationflags=NO_WINDOW if os.name == "nt" else 0)

        def pump():
            for ln in proc.stdout:
                out.write(ln)
                out.flush()
                on_line(ln)

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass
        t0, reason = time.time(), None
        while th.is_alive():
            th.join(0.5)
            if os.path.exists(os.path.join(run_dir, "cancel")):
                reason = "cancelled"
            elif time.time() - t0 > timeout:
                reason = "timeout"
            elif stop():
                reason = "limit"
            if reason:
                _kill_tree(proc)
                th.join(5)
                break
        proc.wait(timeout=30)
        if reason is None and stop():   # it finished (or was fast) but had already gone over the limit
            reason = "limit"
    with open(err_path, encoding="utf-8", errors="replace") as f:
        return f.read(4000), reason, proc.returncode


def runner_main(run_dir, popen=subprocess.Popen, claude_path=None):
    """Runs as the logged-on user (limited token). Never raises: failure goes to error.json."""
    def status(**kw):
        _write(os.path.join(run_dir, "status.json"), dict(kw, t=time.time()))

    def fail(msg, kind="failed"):
        _write(os.path.join(run_dir, "error.json"), {"error": str(msg)[:2000], "kind": kind})

    try:
        with open(os.path.join(run_dir, "request.json"), encoding="utf-8") as f:
            req = json.load(f)
        path = claude_path or find_claude()
        if not path:
            return fail("Claude Code (claude) was not found for this Windows user.", "missing")
        status(phase="research", searches=0, fetches=0)
        st, last = new_state(), [0.0]

        def on_line(ln):
            feed(st, ln)
            if time.time() - last[0] > 1.0:
                last[0] = time.time()
                status(phase="research", searches=st["searches"], fetches=st["fetches"], urls=len(st["urls"]))

        # step 1: research
        argv = _argv(path, research_args(req["model"], req["system"]))
        err, reason, rc = _invoke(argv, req["research_prompt"], run_dir, os.path.join(run_dir, "stream1.jsonl"), on_line, STEP_TIMEOUT, popen,
                                stop=lambda: st["searches"] > MAX_SEARCHES or st["fetches"] > MAX_FETCHES)
        if reason == "limit":
            return fail("Claude used more than %d searches / %d page reads; stopped." % (MAX_SEARCHES, MAX_FETCHES))
        if reason == "cancelled":
            return fail("Cancelled", "cancelled")
        if reason == "timeout":
            return fail("Research took longer than %d minutes." % (STEP_TIMEOUT // 60), "timeout")
        res = st["result"]
        if not res or res.get("is_error"):
            return fail((res or {}).get("result") or err or "claude exited with code %s and no result" % rc)
        notes = res.get("result") if isinstance(res.get("result"), str) and res.get("result").strip() else "\n\n".join(st["text"])
        if not notes.strip():
            return fail("The research step returned no findings.")
        status(phase="structure", searches=st["searches"], fetches=st["fetches"], urls=len(st["urls"]))

        # step 2: notes -> JSON
        src = "\n".join("- %s  (%s)" % (u, t) for u, t in st["urls"].items()) or "(none)"
        prompt = req["structure_prompt"].replace("@@NOTES@@", notes).replace("@@SOURCES@@", src)
        out2 = []
        argv = _argv(path, structure_args(req["model"], req["system"], req["schema"]))
        err, reason, rc = _invoke(argv, prompt, run_dir, os.path.join(run_dir, "structure.json"), out2.append, STEP_TIMEOUT, popen)
        if reason == "cancelled":
            return fail("Cancelled", "cancelled")
        if reason == "timeout":
            return fail("Building the profile took longer than %d minutes." % (STEP_TIMEOUT // 60), "timeout")
        res2 = load_output_json("".join(out2))
        if not res2 or res2.get("is_error"):
            return fail((res2 or {}).get("result") or err or "claude exited with code %s and no result" % rc)
        j = structured_from(res2)
        if j is None:
            return fail("The profile answer wasn't valid JSON.")
        cost = sum(float(r.get("total_cost_usd") or 0) for r in (res, res2) if isinstance(r.get("total_cost_usd", 0), (int, float)))
        _write(os.path.join(run_dir, "done.json"), {"notes": notes, "structured": j, "cli_cost_usd": cost,
                                                    "usage": [res.get("usage"), res2.get("usage")]})
        status(phase="done", searches=st["searches"], fetches=st["fetches"], urls=len(st["urls"]))
    except Exception as e:  # noqa: BLE001
        try:
            fail("%s: %s" % (type(e).__name__, e))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- elevated side
def _schtasks(args):
    """(returncode, output). Windows only; tests replace this."""
    try:
        r = subprocess.run(["schtasks.exe"] + list(args), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def _pythonw():
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return w if os.path.isfile(w) else exe


def task_create_args(pythonw, script, run_dir):
    """One-shot task for the logged-on user, least privilege (/rl limited), only when that user is logged on (/it)."""
    return ["/create", "/f", "/tn", TASK, "/sc", "once", "/st", "00:00", "/rl", "limited", "/it",
            "/tr", '"%s" "%s" "%s"' % (pythonw, script, run_dir)]


def _read_capped(path, cap=READ_CAP):
    try:
        with open(path, "rb") as f:
            return f.read(cap).decode("utf-8", "replace")
    except OSError:
        return None


def _read_obj(path):
    t = _read_capped(path)
    try:
        j = json.loads(t) if t else None
    except ValueError:
        return None
    return j if isinstance(j, dict) else None


def _int(v, hi=10_000):
    return min(max(v, 0), hi) if isinstance(v, int) and not isinstance(v, bool) else 0


def _stream_state(path):
    """Recompute provenance from the raw stream (capped); what the runner says about URLs isn't trusted."""
    st = new_state()
    n = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                n += len(ln)
                if n > STREAM_CAP:
                    break
                feed(st, ln)
    except OSError:
        pass
    return st


def _run_name():
    return "%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid())


def run(scan, progress=lambda msg: None, cancel=None, *, timeout=2 * STEP_TIMEOUT + 300, sleep=time.sleep, now=time.time):
    """Same return shape as research.run. Raises ResearchError."""
    claude = find_claude()
    if not claude:
        raise ResearchError("Claude Code isn't installed for this Windows user. Install it from claude.com/download, run `claude` once and /login.")
    run_dir = os.path.join(RUN_ROOT, _run_name())
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "request.json"), "w", encoding="utf-8") as f:
        json.dump(build_request(scan), f)
    script = os.path.join(APP_DIR, "headroom_research_cli.pyw")
    rc, out = _schtasks(task_create_args(_pythonw(), script, run_dir))
    if rc != 0:
        raise ResearchError("Couldn't start the research task as your user account (%s)." % research.clean(out, 200))
    done = None
    try:
        rc, out = _schtasks(["/run", "/tn", TASK])
        if rc != 0:
            raise ResearchError("Couldn't start the research task (%s)." % research.clean(out, 200))
        progress("Starting Claude Code (your login)")
        t0 = now()
        started = False
        while True:
            if cancel is not None and cancel.is_set():
                open(os.path.join(run_dir, "cancel"), "w").close()
                _schtasks(["/end", "/tn", TASK])
                raise ResearchError("Cancelled")
            err = _read_obj(os.path.join(run_dir, "error.json"))
            if err is not None:
                if err.get("kind") == "cancelled":
                    raise ResearchError("Cancelled")
                if err.get("kind") == "timeout":
                    raise ResearchError(research.clean(err.get("error"), 200))
                raise ResearchError(friendly_error(str(err.get("error", ""))))
            done = _read_obj(os.path.join(run_dir, "done.json"))
            if done is not None:
                break
            s = _read_obj(os.path.join(run_dir, "status.json"))
            if s:
                started = True
                if s.get("phase") == "structure":
                    progress("Building your profile from %d sources" % _int(s.get("urls")))
                else:
                    progress("Researching: %d searches, %d pages read" % (_int(s.get("searches")), _int(s.get("fetches"))))
            if not started and now() - t0 > START_TIMEOUT:
                raise ResearchError("Claude Code didn't start. It needs your Windows user to be signed in (the task runs as that user).")
            if now() - t0 > timeout:
                raise ResearchError("Research took too long and was stopped.")
            sleep(1.0)
        st = _stream_state(os.path.join(run_dir, "stream1.jsonl"))
    finally:
        _schtasks(["/end", "/tn", TASK])
        _schtasks(["/delete", "/f", "/tn", TASK])
        if done is not None:
            shutil.rmtree(run_dir, ignore_errors=True)
    j = done.get("structured")
    notes = done.get("notes")
    if not isinstance(j, dict) or not isinstance(notes, str):
        raise ResearchError("Claude Code's answer wasn't in the expected format.")
    allowed = set(st["urls"])
    out = research.to_profiles(j, allowed)
    cost = done.get("cli_cost_usd")
    usage = {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0, "web_searches": st["searches"],
             "web_fetches": st["fetches"], "cost_usd": 0.0,   # covered by the plan, not billed per token
             "plan_equivalent_usd": round(cost, 4) if isinstance(cost, (int, float)) and cost == cost else 0.0}
    out.update(sources=sorted(allowed), when=time.strftime("%Y-%m-%d %H:%M"), model=research.MODEL, usage=usage,
               notes_text=research.clean_text(notes, 20000), backend="claude_cli")
    return out
