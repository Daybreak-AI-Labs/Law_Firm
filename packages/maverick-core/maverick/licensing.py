"""Signed license keys for the sellable agent SKUs.

The capability seam (each standalone agent's ``capabilities.py``) is the
enforcement point; this module is the platform-side mint. A license is a
compact token::

    LW1.<base64url(payload JSON)>.<base64url(Ed25519 signature)>

with payload ``{customer, sku, capabilities, issued_at, expires_at}``. The
agent ships only the PUBLIC key (env ``LIGHTWORK_LICENSE_PUBKEY``) and the
token (``LIGHTWORK_LICENSE``); the private key never leaves the vendor. An
upsell is a key swap, not a reinstall. No license -> the agent runs in
evaluation mode (bounded, honest, never crippled mid-flight).

CLI::

    python -m maverick.licensing keygen --out-dir keys/
    python -m maverick.licensing make --key keys/license_signing.pem \
        --customer "Acme" --sku pia_standalone --days 365
    python -m maverick.licensing show --pubkey keys/license_pubkey.pem \
        --token LW1....

Uses the ``cryptography`` package already required by the platform and the
standalone requirements — no new dependencies.
"""
from __future__ import annotations

import base64
import json
import time

PREFIX = "LW1"


class LicenseError(ValueError):
    """Bad format, bad signature, or expired."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def generate_keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem) for an Ed25519 signing pair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private_pem, public_pem


def make_license(*, customer: str, sku: str, private_pem: str,
                 days: int = 365,
                 capabilities: tuple[str, ...] | list[str] = ()) -> str:
    """Mint a signed license token."""
    from cryptography.hazmat.primitives import serialization
    if not customer.strip() or not sku.strip():
        raise LicenseError("customer and sku are required")
    payload = {
        "customer": customer.strip(),
        "sku": sku.strip(),
        "capabilities": sorted(set(capabilities)),
        "issued_at": int(time.time()),
        "expires_at": int(time.time() + max(1, days) * 86400),
    }
    body = json.dumps(payload, sort_keys=True,
                      separators=(",", ":")).encode()
    key = serialization.load_pem_private_key(private_pem.encode(),
                                             password=None)
    sig = key.sign(body)
    return f"{PREFIX}.{_b64e(body)}.{_b64e(sig)}"


def verify_license(token: str, public_pem: str) -> dict:
    """Verify signature + expiry; return the payload. Raises LicenseError
    on any problem — callers decide what evaluation mode means."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise LicenseError("not a Lightwork license token")
    try:
        body = _b64d(parts[1])
        sig = _b64d(parts[2])
    except (ValueError, TypeError) as exc:
        raise LicenseError("malformed license encoding") from exc
    try:
        pub = serialization.load_pem_public_key(public_pem.encode())
        pub.verify(sig, body)
    except InvalidSignature as exc:
        raise LicenseError("license signature is invalid") from exc
    except (ValueError, TypeError) as exc:
        raise LicenseError("bad public key") from exc
    try:
        payload = json.loads(body)
    except ValueError as exc:  # pragma: no cover - signed bytes are json
        raise LicenseError("license payload is not JSON") from exc
    if float(payload.get("expires_at") or 0) <= time.time():
        raise LicenseError("license has expired")
    return payload


def _cli() -> int:
    import argparse
    from pathlib import Path
    ap = argparse.ArgumentParser(prog="maverick.licensing",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    kg = sub.add_parser("keygen", help="mint an Ed25519 signing pair")
    kg.add_argument("--out-dir", default=".")
    mk = sub.add_parser("make", help="mint a signed license token")
    mk.add_argument("--key", required=True,
                    help="path to the private PEM from keygen")
    mk.add_argument("--customer", required=True)
    mk.add_argument("--sku", required=True)
    mk.add_argument("--days", type=int, default=365)
    mk.add_argument("--capability", action="append", default=[])
    sh = sub.add_parser("show", help="verify + print a token's payload")
    sh.add_argument("--pubkey", required=True)
    sh.add_argument("--token", required=True)
    args = ap.parse_args()
    if args.cmd == "keygen":
        private_pem, public_pem = generate_keypair()
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "license_signing.pem").write_text(private_pem)
        (out / "license_pubkey.pem").write_text(public_pem)
        print(f"wrote {out / 'license_signing.pem'} (KEEP PRIVATE) and "
              f"{out / 'license_pubkey.pem'} (ship with the agent)")
        return 0
    if args.cmd == "make":
        token = make_license(
            customer=args.customer, sku=args.sku, days=args.days,
            capabilities=args.capability,
            private_pem=Path(args.key).read_text())
        print(token)
        return 0
    payload = verify_license(args.token, Path(args.pubkey).read_text())
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(_cli())
