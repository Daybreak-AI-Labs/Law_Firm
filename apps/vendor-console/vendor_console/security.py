"""Auth crypto for the vendor console — stdlib only (no bcrypt/pyotp/itsdangerous).

Three primitives:
- **Passwords**: PBKDF2-HMAC-SHA256 with a per-user salt, stored as a self-
  describing ``pbkdf2_sha256$iters$salt$hash`` string; verified in constant time.
- **TOTP** (RFC 6238): 6-digit, 30s step, SHA-1 — compatible with Google/Microsoft
  Authenticator, 1Password, etc. Verified with a ±1 step window for clock skew.
- **Signed sessions**: an HMAC-SHA256-signed, base64url token carrying a small
  JSON payload + expiry, so the session cookie is tamper-evident with no server
  store and no extra dependency.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time

_PBKDF2_ITERS = 210_000
_ALGO = "pbkdf2_sha256"


# ---- passwords -------------------------------------------------------------

def hash_password(password: str, *, iters: int = _PBKDF2_ITERS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return f"{_ALGO}${iters}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ---- TOTP (RFC 6238) -------------------------------------------------------

def new_totp_secret(nbytes: int = 20) -> str:
    """A fresh base32 TOTP secret (20 bytes = 160 bits, the RFC-recommended size)."""
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii").rstrip("=")


def _totp_at(secret_b32: str, for_time: float, *, step: int = 30, digits: int = 6) -> str:
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter = struct.pack(">Q", int(for_time // step))
    mac = hmac.new(key, counter, hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFF_FFFF
    return str(code % (10 ** digits)).zfill(digits)


def totp_now(secret_b32: str, *, at: float | None = None) -> str:
    return _totp_at(secret_b32, at if at is not None else time.time())


def totp_step(at: float | None = None, *, step: int = 30) -> int:
    return int((at if at is not None else time.time()) // step)


def totp_match_step(secret_b32: str, code: str, *, at: float | None = None,
                    window: int = 1, after_step: int = -1) -> int | None:
    """Return the **time-step** a valid ``code`` matches (current or ±``window``
    for clock skew), or ``None``. ``after_step`` enforces **single use** (RFC 6238
    §5.2): a code whose step is ``<= after_step`` — already consumed — is rejected.
    Callers persist the returned step so the same code can't be replayed."""
    if not code or not code.strip().isdigit():
        return None
    now = at if at is not None else time.time()
    code = code.strip()
    base = totp_step(now)
    for drift in range(-window, window + 1):
        step = base + drift
        if step > after_step and hmac.compare_digest(
                _totp_at(secret_b32, now + drift * 30), code):
            return step
    return None


def totp_verify(secret_b32: str, code: str, *, at: float | None = None,
                window: int = 1) -> bool:
    """True if ``code`` matches the current step, or ±``window`` steps (clock skew).
    Stateless (no single-use guard) — use :func:`totp_match_step` for login."""
    return totp_match_step(secret_b32, code, at=at, window=window) is not None


def totp_uri(secret_b32: str, account: str, *, issuer: str = "Daybreak Console") -> str:
    """otpauth:// URI to paste into (or QR for) an authenticator app."""
    from urllib.parse import quote
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")


# ---- signed session token --------------------------------------------------

def _b64u(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _b64u_decode(data: bytes) -> bytes:
    return base64.urlsafe_b64decode(data + b"=" * (-len(data) % 4))


def sign_session(payload: dict, key: bytes, *, ttl: int = 12 * 3600) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl
    b = _b64u(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64u(hmac.new(key, b, hashlib.sha256).digest())
    return (b + b"." + sig).decode("ascii")


def verify_session(token: str, key: bytes) -> dict | None:
    """Return the payload if the token is authentic and unexpired, else None."""
    try:
        b, sig = token.encode("ascii").split(b".")
    except (ValueError, AttributeError):
        return None
    expected = _b64u(hmac.new(key, b, hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64u_decode(b))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def session_key() -> bytes:
    """The HMAC key for session cookies. From ``VENDOR_CONSOLE_SECRET`` in prod;
    a random per-process key in dev (which logs everyone out on restart, and
    differs per worker under multi-process servers — fine for local use, never
    acceptable in prod, hence the env var). A set-but-too-short secret is a
    misconfiguration we refuse rather than silently weaken."""
    env = os.environ.get("VENDOR_CONSOLE_SECRET")
    if env:
        if len(env) < 16:
            raise RuntimeError("VENDOR_CONSOLE_SECRET must be at least 16 characters")
        return hashlib.sha256(env.encode("utf-8")).digest()
    return _DEV_KEY


_DEV_KEY = secrets.token_bytes(32)
