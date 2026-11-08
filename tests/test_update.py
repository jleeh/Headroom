import base64
import hashlib
import io
import json

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402

from headroom import update  # noqa: E402

PFX = update.URL_PREFIX


def keypair():
    k = ec.generate_private_key(ec.SECP256R1())
    raw = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return k, raw[1:].hex()


def sign(k, data):
    r, s = decode_dss_signature(k.sign(data, ec.ECDSA(hashes.SHA256())))
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


def manifest(**kw):
    m = {"version": "1.1.0", "url": PFX + "v1.1.0/HeadroomSetup-1.1.0.exe", "sha256": "0" * 64, "size": 3, "notes": "n"}
    m.update(kw)
    return m


def test_verify_roundtrip_and_rejections():
    k, pub = keypair()
    data = b'{"version": "1.1.0"}'
    sig = sign(k, data)
    be = update._crypto_verify
    assert update.verify(data, sig, pub, be)
    assert update.verify(data, sig, pub)          # default backend on Linux = cryptography
    assert not update.verify(data + b" ", sig, pub, be)
    bad = bytearray(base64.b64decode(sig))
    bad[5] ^= 1
    assert not update.verify(data, base64.b64encode(bytes(bad)).decode(), pub, be)
    assert not update.verify(data, sig, keypair()[1], be)
    assert not update.verify(data, sig, "", be)
    assert not update.verify(data, "not base64!!", pub, be)
    assert not update.verify(data, base64.b64encode(b"x" * 63).decode(), pub, be)
    assert not update.verify(data, sig, pub[:-2], be)
    assert not update.verify(data, sig, pub, lambda *a: None)   # fails closed


def test_no_backend_fails_closed(monkeypatch):
    k, pub = keypair()
    monkeypatch.setattr(update, "_default_backend", lambda: None)
    assert not update.verify(b"x", sign(k, b"x"), pub)


def test_versions():
    assert update.parse_version("v1.10.0") > update.parse_version("1.9.9")
    assert update.parse_version("1.1") < update.parse_version("1.1.1")
    for bad in ("", "1.x", "1..2", "latest", "1.2.3.4.5", "-1.0"):
        with pytest.raises(ValueError):
            update.parse_version(bad)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def fake_opener(files):
    def op(req, timeout=0):
        return Resp(files[req.full_url])
    return op


def release(m_bytes, sig):
    api = {"assets": [{"name": "update.json", "browser_download_url": PFX + "v/update.json"},
                      {"name": "update.json.sig", "browser_download_url": PFX + "v/update.json.sig"}]}
    return {update.LATEST_URL: json.dumps(api).encode(), PFX + "v/update.json": m_bytes, PFX + "v/update.json.sig": sig.encode()}


def test_disabled_without_key():
    r = update.check("1.0.0", opener=lambda *a, **k: 1 / 0)
    assert not r["enabled"] and not r["available"] and "disabled" in r["error"]


def test_check_available_and_not():
    k, pub = keypair()
    data = json.dumps(manifest()).encode()
    op = fake_opener(release(data, sign(k, data)))
    r = update.check("1.0.0", op, pub)
    assert r["enabled"] and r["available"] and r["latest"] == "1.1.0" and r["error"] is None
    assert not update.check("1.1.0", op, pub)["available"]
    assert not update.check("1.2.0", op, pub)["available"]


def test_check_bad_signature_and_bad_url():
    k, pub = keypair()
    data = json.dumps(manifest()).encode()
    r = update.check("1.0.0", fake_opener(release(data + b" ", sign(k, data))), pub)
    assert not r["available"] and "signature" in r["error"]
    evil = json.dumps(manifest(url="https://evil.example/HeadroomSetup.exe")).encode()
    r = update.check("1.0.0", fake_opener(release(evil, sign(k, evil))), pub)
    assert not r["available"] and "URL" in r["error"]
    r = update.check("1.0.0", lambda *a, **k: 1 / 0, pub)
    assert not r["available"] and r["error"]


def test_download_and_verify(tmp_path):
    body = b"abc"
    m = manifest(sha256=hashlib.sha256(body).hexdigest(), size=3)
    p = update.download_and_verify(m, str(tmp_path), fake_opener({m["url"]: body}))
    assert open(p, "rb").read() == body
    bad = manifest(sha256="1" * 64, size=3)
    with pytest.raises(RuntimeError):
        update.download_and_verify(bad, str(tmp_path / "b"), fake_opener({bad["url"]: body}))
    short = manifest(sha256=hashlib.sha256(body).hexdigest(), size=4)
    with pytest.raises(RuntimeError):
        update.download_and_verify(short, str(tmp_path / "c"), fake_opener({short["url"]: body}))
    with pytest.raises(ValueError):
        update.download_and_verify(manifest(url="https://evil.example/x.exe"), str(tmp_path), None)
    assert not list((tmp_path / "b").glob("*"))
