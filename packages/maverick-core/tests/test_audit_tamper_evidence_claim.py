"""The "tamper-evident audit ledger" claim must be honest about key custody.

By default the Ed25519 audit-signing key is co-located on the host, readable by
the same uid that runs the agent -- so the signed chain detects accidental /
non-privileged edits (integrity) but is NOT tamper-evidence against a same-uid
actor, who can read the key and cleanly re-sign the chain. So
:func:`maverick.audit.signing._load_or_create_keypair` warns (once) when it
generates a co-located key, and ``active_key_is_offhost`` tracks custody.
"""
from __future__ import annotations

import logging

import pytest


def _fresh_key_hex() -> str:
    """A raw 32-byte Ed25519 private key as hex (an off-host / KMS-shaped key)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv = ed25519.Ed25519PrivateKey.generate()
    return priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()


def test_active_key_predicate_tracks_custody(monkeypatch, tmp_path):
    pytest.importorskip("cryptography")
    from maverick.audit import signing

    monkeypatch.setattr(signing, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(
        signing, "_INJECTED_KEYPAIR_CACHE", signing._INJECTED_KEYPAIR_UNREAD
    )
    # No off-host source -> co-located.
    monkeypatch.delenv(signing._SIGNING_KEY_ENV, raising=False)
    monkeypatch.delenv(signing._KMS_WRAPPED_KEY_ENV, raising=False)
    assert signing.active_key_is_offhost() is False

    # Inject an off-host key -> predicate flips to True.
    monkeypatch.setattr(
        signing, "_INJECTED_KEYPAIR_CACHE", signing._INJECTED_KEYPAIR_UNREAD
    )
    monkeypatch.setenv(signing._SIGNING_KEY_ENV, _fresh_key_hex())
    assert signing.active_key_is_offhost() is True


def test_local_key_generation_warns_about_weak_custody(monkeypatch, tmp_path, caplog):
    pytest.importorskip("cryptography")
    from maverick.audit import signing

    monkeypatch.delenv(signing._SIGNING_KEY_ENV, raising=False)
    monkeypatch.delenv(signing._KMS_WRAPPED_KEY_ENV, raising=False)
    monkeypatch.setattr(
        signing, "_INJECTED_KEYPAIR_CACHE", signing._INJECTED_KEYPAIR_UNREAD
    )
    monkeypatch.setattr(signing, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(signing, "require_offhost_signing", lambda: False)
    # Reset the one-time guard so this test observes the warning regardless of
    # what ran earlier in the same worker process.
    monkeypatch.setattr(signing, "_LOCAL_KEY_WARNED", False)

    with caplog.at_level(logging.WARNING, logger="maverick.audit.signing"):
        _priv, _pub, key_id = signing._load_or_create_keypair()

    # A co-located private key was actually generated on disk...
    assert (tmp_path / "keys" / f"{key_id}.key").exists()
    # ...and the operator was warned that this is integrity-only, not
    # tamper-evidence against a same-uid actor, with the off-host remedy named.
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "co-located" in m and "tamper-evidence" in m.lower() and "off-host" in m.lower()
        for m in warnings
    ), warnings
    assert any(signing._SIGNING_KEY_ENV in m for m in warnings), warnings
