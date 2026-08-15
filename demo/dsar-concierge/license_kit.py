"""License verification for the standalone agent — the seam's gatekeeper.

The agent ships with the vendor's PUBLIC key only; the token arrives via
``LIGHTWORK_LICENSE`` (the key itself or a file path) and the key via
``LIGHTWORK_LICENSE_PUBKEY`` (PEM text or a file path). Verification is
Ed25519 over the exact token format ``maverick.licensing`` mints.

No license, a bad license, or an expired one all mean EVALUATION MODE:
the agent stays fully functional but caps concurrent open cases — bounded
and honest, never crippled mid-flight, and the banner says exactly why.
An upsell or renewal is a key swap, not a reinstall.

Standalone-safe: cryptography + stdlib only (cryptography is already in
requirements-standalone.txt).
"""
from __future__ import annotations

import base64
import json
import os
import time

EVAL_OPEN_CASE_CAP = 5


def _read_maybe_file(value: str) -> str:
    v = (value or "").strip()
    if v and "\n" not in v and os.path.isfile(v):
        try:
            with open(v, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""
    return v


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_license() -> dict:
    """Resolve the running license. Always returns a dict:

    ``{"mode": "licensed"|"evaluation", "customer", "sku", "expires_at",
    "capabilities", "reason"}`` — ``reason`` explains evaluation mode
    ("no license configured", "license has expired", ...)."""
    out = {"mode": "evaluation", "customer": "", "sku": "",
           "expires_at": None, "capabilities": [],
           "reason": "no license configured",
           "open_case_cap": EVAL_OPEN_CASE_CAP}
    token = _read_maybe_file(os.environ.get("LIGHTWORK_LICENSE", ""))
    pubkey = _read_maybe_file(os.environ.get("LIGHTWORK_LICENSE_PUBKEY", ""))
    if not token:
        return out
    if not pubkey:
        out["reason"] = "license present but no public key configured"
        return out
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != "LW1":
        out["reason"] = "not a Lightwork license token"
        return out
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        body = _b64d(parts[1])
        sig = _b64d(parts[2])
        pub = serialization.load_pem_public_key(pubkey.encode())
        try:
            pub.verify(sig, body)
        except InvalidSignature:
            out["reason"] = "license signature is invalid"
            return out
        payload = json.loads(body)
    except Exception as exc:
        out["reason"] = f"license could not be verified: {exc}"[:160]
        return out
    expires = float(payload.get("expires_at") or 0)
    if expires <= time.time():
        out["reason"] = "license has expired"
        out["customer"] = str(payload.get("customer", ""))
        out["sku"] = str(payload.get("sku", ""))
        out["expires_at"] = expires
        return out
    return {"mode": "licensed",
            "customer": str(payload.get("customer", "")),
            "sku": str(payload.get("sku", "")),
            "expires_at": expires,
            "capabilities": list(payload.get("capabilities") or []),
            "reason": "", "open_case_cap": None}
