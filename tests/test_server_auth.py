"""The elevated API must refuse anything that isn't Headroom's own window: no token,
wrong Host (DNS rebinding), cross-site 'simple' requests (text/plain / form posts)."""
import http.client
import json
import os
import socket
import threading

import pytest


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Any:
    def __init__(self, name, calls):
        self.name, self.calls = name, calls

    def __call__(self, *a, **k):
        self.calls.append(self.name)

    def __getattr__(self, attr):
        return Any(self.name + "." + attr, self.calls)


@pytest.fixture(scope="module")
def srv():
    port = _free_port()
    os.environ["HEADROOM_PORT"] = str(port)
    os.environ["HEADROOM_TOKEN"] = "test-token-123"
    import importlib

    import headroom.server as server
    server = importlib.reload(server)      # pick up HEADROOM_PORT for ALLOWED_HOSTS
    server.Handler.token = "test-token-123"

    calls = []

    class FakeCpu:
        watch = None

        def reset(self):
            calls.append("cpu_reset")

    class FakeCtl:
        cpu = FakeCpu()
        profiles = {"gpu": {}}

        def status(self, since):
            return {"ok": True, "since": since}

        def gpu_apply(self, core, mem):
            calls.append(("apply", core, mem))

        def shutdown(self):
            calls.append("shutdown")

        def __getattr__(self, name):          # every other route target (also c.setup.cancel etc.)
            return Any(name, calls)

    httpd = server.Exclusive(("127.0.0.1", port), server.Handler)
    server.Handler.ctl = FakeCtl()
    server.Handler.server_ref = httpd
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port, calls, server
    httpd.shutdown()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": "127.0.0.1:%d" % port}
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    return r.status, data, r


TOK = {"X-Headroom-Token": "test-token-123"}
JSON = {"Content-Type": "application/json"}


def test_ping_is_open_but_harmless(srv):
    port, _, _ = srv
    st, data, _ = req(port, "GET", "/api/ping")
    d = json.loads(data)
    assert st == 200 and d["ok"] is True and set(d) == {"ok", "pid"}   # nothing but liveness


def test_status_needs_token(srv):
    port, _, _ = srv
    assert req(port, "GET", "/api/status")[0] == 403
    assert req(port, "GET", "/api/status", headers={"X-Headroom-Token": "wrong"})[0] == 403
    assert req(port, "GET", "/api/status?since=3", headers=TOK)[0] == 200


def test_cross_site_simple_post_refused(srv):
    port, calls, _ = srv
    # what a malicious web page can send without a CORS preflight
    st, _, _ = req(port, "POST", "/api/gpu/apply", body='{"core":2000}', headers={"Content-Type": "text/plain"})
    assert st == 403
    st, _, _ = req(port, "POST", "/api/gpu/apply", body='{"core":2000}', headers={**TOK, "Content-Type": "text/plain"})
    assert st == 400
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "apply"]


def test_dns_rebinding_host_refused(srv):
    port, _, _ = srv
    st, _, _ = req(port, "GET", "/api/status", headers={**TOK, "Host": "evil.example:%d" % port})
    assert st == 403
    st, _, _ = req(port, "GET", "/", headers={"Host": "evil.example:%d" % port})
    assert st == 403


def test_authed_json_post_works(srv):
    port, calls, _ = srv
    st, _, _ = req(port, "POST", "/api/gpu/apply", body=json.dumps({"core": 50, "mem": 100}), headers={**TOK, **JSON})
    assert st == 200 and ("apply", 50, 100) in calls


def test_body_limits(srv):
    port, _, server = srv
    big = "x" * (server.MAX_BODY + 1)
    try:   # refused without reading the body: Windows may reset the connection instead of showing the 400
        assert req(port, "POST", "/api/bench", body=big, headers={**TOK, **JSON})[0] == 400
    except ConnectionError:
        pass
    assert req(port, "POST", "/api/bench", body="[1,2]", headers={**TOK, **JSON})[0] == 400


def test_page_has_csp_and_no_traversal(srv):
    port, _, _ = srv
    st, _, r = req(port, "GET", "/")
    assert st == 200 and "script-src 'self'" in r.getheader("Content-Security-Policy")
    assert req(port, "GET", "/../headroom/server.py")[0] == 404
    assert req(port, "GET", "/%2e%2e/headroom/server.py")[0] == 404


def test_page_has_no_inline_script():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "web", "index.html"), encoding="utf-8").read()
    import re
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script> would be blocked by the CSP"
    assert not re.search(r"\son[a-z]+\s*=\s*\"", html), "inline event handlers would be blocked by the CSP"


def test_foreign_origin_refused(srv):
    port, _, _ = srv
    st, _, _ = req(port, "POST", "/api/gpu/stock", body="{}", headers={**TOK, **JSON, "Origin": "https://evil.example"})
    assert st == 403
