"""Tests for the independent ledger auditor (``benchmarks/audit_ledger.py``).

Uses the importlib-load pattern (like ``test_dgm_live.py``) so the sibling
benchmark script imports cleanly outside a package. Covers the SIGNATURE half of
the audit end-to-end with a real Ed25519 keypair: a genuinely-signed record
verifies VALID; a one-byte-flipped signature is INVALID and exits nonzero; an
UNSIGNED (pre-signature) record is reported honestly and does NOT fail the audit.

The RE-GRADE half needs staged instance repos + venvs and is out of scope for a
unit test -- it is exercised by manual pod validation (``--forensics`` +
``--manifest``). Here we only assert re-grade stays SKIPPED when those inputs are
absent, so an unsigned/old ledger audits cleanly.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "packages" / "maverick-core"))

pytest.importorskip("cryptography")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _auditor():
    return _load("audit_ledger_mod", _HERE / "audit_ledger.py")


def _keypair(keys_dir: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    keys_dir.mkdir(parents=True, exist_ok=True)
    priv = ed25519.Ed25519PrivateKey.generate()
    priv_hex = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()).hex()
    (keys_dir / "operator.pub").write_bytes(priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    return priv_hex


def _signed_record(priv_hex: str, *, rid="calc__add-1", rung="code"):
    """A ledger record dict signed exactly as the controller would persist it."""
    from maverick import approval_signing as asig
    payload = "diff --git a/x b/x\n-old\n+new\n"
    digest = asig.payload_digest(payload)
    req = asig.ApprovalRequest(candidate_id=rid, rung=rung, payload_sha256=digest)
    sig = asig.sign_request(req, priv_hex)
    return {
        "id": rid, "rung": rung, "summary": "signed fix",
        "baseline_score": 0.0, "candidate_score": 1.0, "promoted_at": 1.0,
        "rolled_back": False, "rolled_back_at": None,
        # The recorded approver_id is informational; the auditor RE-derives the
        # verifying key-id from the signature, so a placeholder here is fine.
        "approver_id": digest[:16],
        "payload_sha256": digest, "approval_signature": sig,
    }


def _write_ledger(path: Path, records: list[dict]):
    path.write_text(json.dumps(records), encoding="utf-8")


def test_valid_signature_reports_valid_and_exits_zero(tmp_path):
    aud = _auditor()
    keys = tmp_path / "keys"
    priv = _keypair(keys)
    ledger = tmp_path / "ledger.json"
    _write_ledger(ledger, [_signed_record(priv)])

    report = aud.audit_ledger(ledger, keys_dir=keys)
    assert report.valid == 1
    assert report.invalid == 0
    assert not report.failed
    assert report.rows[0].signature == aud.VALID
    assert report.rows[0].approver_id  # the approver key-id that verified

    assert aud.main(["--ledger", str(ledger), "--keys", str(keys)]) == 0


def test_flipped_signature_is_invalid_and_exits_nonzero(tmp_path):
    aud = _auditor()
    keys = tmp_path / "keys"
    priv = _keypair(keys)
    rec = _signed_record(priv)
    # Flip one hex nibble of the signature -> no longer verifies.
    sig = rec["approval_signature"]
    flipped = ("f" if sig[0] != "f" else "0") + sig[1:]
    assert flipped != sig
    rec["approval_signature"] = flipped
    ledger = tmp_path / "ledger.json"
    _write_ledger(ledger, [rec])

    report = aud.audit_ledger(ledger, keys_dir=keys)
    assert report.invalid == 1
    assert report.valid == 0
    assert report.failed  # a real audit fails loudly on a bad signature

    assert aud.main(["--ledger", str(ledger), "--keys", str(keys)]) == 1


def test_unsigned_record_is_not_a_failure(tmp_path):
    """A record predating the signature fields (or with no crypto approval) is
    UNSIGNED -- honest, and NOT an audit failure (exit stays zero)."""
    aud = _auditor()
    keys = tmp_path / "keys"
    _keypair(keys)
    ledger = tmp_path / "ledger.json"
    _write_ledger(ledger, [{
        "id": "old-1", "rung": "config", "summary": "legacy",
        "baseline_score": 0.1, "candidate_score": 0.5, "promoted_at": 1.0,
        "rolled_back": False, "rolled_back_at": None,
    }])

    report = aud.audit_ledger(ledger, keys_dir=keys)
    assert report.unsigned == 1
    assert not report.failed
    assert report.rows[0].regrade == aud.SKIPPED  # no --forensics/--manifest
    assert aud.main(["--ledger", str(ledger), "--keys", str(keys)]) == 0


def test_malformed_record_is_a_discrepancy(tmp_path):
    aud = _auditor()
    keys = tmp_path / "keys"
    _keypair(keys)
    ledger = tmp_path / "ledger.json"
    _write_ledger(ledger, [{"no_id": True}, "not-a-dict"])

    report = aud.audit_ledger(ledger, keys_dir=keys)
    assert report.malformed == 2
    assert report.failed
    assert aud.main(["--ledger", str(ledger), "--keys", str(keys)]) == 1
