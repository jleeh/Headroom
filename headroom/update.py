"""Signed update checks. The installer is not code-signed, so trust comes from
an ECDSA P-256 signature over update.json, checked against a key pinned here.
Nothing is ever installed automatically: check() only reports, and the caller
launches the downloaded installer on an explicit user click."""
import base64
import binascii
import hashlib
import json
import os
import sys
import urllib.request

REPO = "eviljarv89-ctrl/Headroom"
LATEST_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
URL_PREFIX = "https://github.com/%s/releases/download/" % REPO
# Hex of the raw public key X||Y (64 bytes). Empty = update checks disabled.
# Create with installer/sign_update.ps1 -NewKey.
UPDATE_PUBKEY = ""
MAX_MANIFEST = 64 * 1024


def enabled(pubkey_hex=None):
    return bool(UPDATE_PUBKEY if pubkey_hex is None else pubkey_hex)


def _bcrypt_verify(data, sig, pub):
    """Windows CNG. pub/sig are raw bytes (X||Y, r||s)."""
    import ctypes
    from ctypes import POINTER, byref, c_long, c_ubyte, c_ulong, c_void_p, c_wchar_p
    bc = ctypes.WinDLL("bcrypt")
    bc.BCryptOpenAlgorithmProvider.argtypes = [POINTER(c_void_p), c_wchar_p, c_wchar_p, c_ulong]
    bc.BCryptOpenAlgorithmProvider.restype = c_long
    bc.BCryptImportKeyPair.argtypes = [c_void_p, c_void_p, c_wchar_p, POINTER(c_void_p), POINTER(c_ubyte), c_ulong, c_ulong]
    bc.BCryptImportKeyPair.restype = c_long
    bc.BCryptVerifySignature.argtypes = [c_void_p, c_void_p, POINTER(c_ubyte), c_ulong, POINTER(c_ubyte), c_ulong, c_ulong]
    bc.BCryptVerifySignature.restype = c_long
    bc.BCryptDestroyKey.argtypes = [c_void_p]
    bc.BCryptDestroyKey.restype = c_long
    bc.BCryptCloseAlgorithmProvider.argtypes = [c_void_p, c_ulong]
    bc.BCryptCloseAlgorithmProvider.restype = c_long
    # BCRYPT_ECCKEY_BLOB { ULONG dwMagic = 'ECS1' (ECDSA P-256 public); ULONG cbKey = 32 } + X + Y
    blob = (0x31534345).to_bytes(4, "little") + (32).to_bytes(4, "little") + pub
    blob_buf = (c_ubyte * len(blob)).from_buffer_copy(blob)
    h = hashlib.sha256(data).digest()
    h_buf = (c_ubyte * 32).from_buffer_copy(h)
    s_buf = (c_ubyte * 64).from_buffer_copy(sig)
    alg, key = c_void_p(), c_void_p()
    if bc.BCryptOpenAlgorithmProvider(byref(alg), "ECDSA_P256", None, 0) != 0:
        return False
    try:
        if bc.BCryptImportKeyPair(alg, None, "ECCPUBLICBLOB", byref(key), blob_buf, len(blob), 0) != 0:
            return False
        try:
            return bc.BCryptVerifySignature(key, None, h_buf, 32, s_buf, 64, 0) == 0
        finally:
            bc.BCryptDestroyKey(key)
    finally:
        bc.BCryptCloseAlgorithmProvider(alg, 0)


def _crypto_verify(data, sig, pub):
    """Fallback for non-Windows (tests, dev): the `cryptography` package, if present."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + pub)
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    try:
        key.verify(der, data, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False


def _default_backend():
    if sys.platform == "win32":
        return _bcrypt_verify
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return None
    return _crypto_verify


def verify(data, sig_b64, pubkey_hex, backend=None):
    """True only for a valid signature. Any malformed input or missing backend fails closed."""
    try:
        pub = bytes.fromhex((pubkey_hex or "").strip())
        sig = base64.b64decode((sig_b64 or "").strip(), validate=True)
        if len(pub) != 64 or len(sig) != 64 or not isinstance(data, bytes):
            return False
        backend = backend or _default_backend()
        return bool(backend and backend(data, sig, pub))
    except (ValueError, binascii.Error, OSError, ImportError, AttributeError):
        return False


def parse_version(v):
    """'1.2.3' or 'v1.2.3' -> (1, 2, 3). Anything else raises ValueError."""
    parts = str(v).strip().lstrip("v").split(".")
    if not 1 <= len(parts) <= 4 or not all(p.isascii() and p.isdigit() for p in parts):
        raise ValueError("bad version %r" % (v,))
    return tuple(int(p) for p in parts)


def _get(url, opener, limit):
    req = urllib.request.Request(url, headers={"User-Agent": "Headroom", "Accept": "application/octet-stream, application/vnd.github+json"})
    with (opener or urllib.request.urlopen)(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("response too large")
    return data


def validate_manifest(m):
    if not isinstance(m, dict):
        raise ValueError("manifest is not an object")
    parse_version(m.get("version"))
    url = m.get("url")
    if not isinstance(url, str) or not url.startswith(URL_PREFIX):
        raise ValueError("update URL is not a Headroom release download")
    sha = m.get("sha256")
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdefABCDEF" for c in sha):
        raise ValueError("bad sha256 in manifest")
    if not isinstance(m.get("size"), int) or m["size"] <= 0:
        raise ValueError("bad size in manifest")
    return m


def check(current_version, opener=None, pubkey_hex=None, backend=None):
    """Ask GitHub for the latest release and verify its signed manifest."""
    pub = UPDATE_PUBKEY if pubkey_hex is None else pubkey_hex
    r = {"enabled": bool(pub), "current": current_version, "latest": None, "available": False,
         "notes": "", "url": "", "error": None}
    if not pub:
        r["error"] = "Update checks are disabled in this build (no update signing key)."
        return r
    try:
        rel = json.loads(_get(LATEST_URL, opener, 1 << 20))
        assets = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets", [])}
        if "update.json" not in assets or "update.json.sig" not in assets:
            raise ValueError("release has no signed update manifest")
        for u in assets.values():
            if not str(u).startswith(URL_PREFIX):
                raise ValueError("unexpected asset URL")
        data = _get(assets["update.json"], opener, MAX_MANIFEST)
        sig = _get(assets["update.json.sig"], opener, 1024).decode("ascii", "replace")
        if not verify(data, sig, pub, backend):
            raise ValueError("update manifest signature is invalid. Ignoring this update.")
        m = validate_manifest(json.loads(data))
        r["latest"] = m["version"]
        r["notes"] = str(m.get("notes") or "")
        r["url"] = m["url"]
        r["available"] = parse_version(m["version"]) > parse_version(current_version)
        r["manifest"] = m
    except Exception as e:  # noqa: BLE001
        r["error"] = "Update check failed: %s" % e
        r["available"] = False
    return r


def download_and_verify(manifest, dest_dir, opener=None):
    """Fetch the installer named by a SIGNED manifest; size and SHA-256 must match."""
    validate_manifest(manifest)
    os.makedirs(dest_dir, exist_ok=True)
    name = "HeadroomSetup-%s.exe" % ".".join(str(x) for x in parse_version(manifest["version"]))
    dst = os.path.join(dest_dir, name)
    part = dst + ".part"
    h = hashlib.sha256()
    n = 0
    req = urllib.request.Request(manifest["url"], headers={"User-Agent": "Headroom"})
    with (opener or urllib.request.urlopen)(req, timeout=300) as r, open(part, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            n += len(chunk)
            if n > manifest["size"]:
                break
            h.update(chunk)
            f.write(chunk)
    if n != manifest["size"] or h.hexdigest() != manifest["sha256"].lower():
        os.remove(part)
        raise RuntimeError("Downloaded installer failed its integrity check (size or SHA-256). Not installed.")
    os.replace(part, dst)
    return dst
