"""Signed license keys: mint, verify, tamper, expiry."""
from __future__ import annotations

import pytest
from maverick import licensing


def _pair():
    return licensing.generate_keypair()


def test_roundtrip_preserves_payload():
    private_pem, public_pem = _pair()
    token = licensing.make_license(
        customer="Acme Corp", sku="pia_standalone", private_pem=private_pem,
        days=30, capabilities=["vendor_memory", "speed_story"])
    assert token.startswith("LW1.")
    payload = licensing.verify_license(token, public_pem)
    assert payload["customer"] == "Acme Corp"
    assert payload["sku"] == "pia_standalone"
    assert payload["capabilities"] == ["speed_story", "vendor_memory"]
    assert payload["expires_at"] > payload["issued_at"]


def test_tampered_payload_fails_signature():
    private_pem, public_pem = _pair()
    token = licensing.make_license(customer="Acme", sku="x",
                                   private_pem=private_pem)
    head, body, sig = token.split(".")
    # Flip a character inside the payload segment.
    forged = f"{head}.{body[:-2] + ('AA' if body[-2:] != 'AA' else 'BB')}.{sig}"
    with pytest.raises(licensing.LicenseError):
        licensing.verify_license(forged, public_pem)


def test_wrong_key_and_bad_format_fail():
    private_pem, _ = _pair()
    _, other_pub = _pair()
    token = licensing.make_license(customer="Acme", sku="x",
                                   private_pem=private_pem)
    with pytest.raises(licensing.LicenseError):
        licensing.verify_license(token, other_pub)
    with pytest.raises(licensing.LicenseError):
        licensing.verify_license("garbage", other_pub)
    with pytest.raises(licensing.LicenseError):
        licensing.verify_license("XX1.a.b", other_pub)


def test_expired_license_fails(monkeypatch):
    private_pem, public_pem = _pair()
    token = licensing.make_license(customer="Acme", sku="x",
                                   private_pem=private_pem, days=1)
    real_time = licensing.time.time
    monkeypatch.setattr(licensing.time, "time",
                        lambda: real_time() + 2 * 86400)
    with pytest.raises(licensing.LicenseError, match="expired"):
        licensing.verify_license(token, public_pem)
