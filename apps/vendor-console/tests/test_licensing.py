"""License lifecycle: issue (signs with the product's own code), upsell, revoke,
serve-resolution, and display status."""
from __future__ import annotations

from maverick import entitlements
from vendor_console import licensing, store


def _customer(conn, name="Cedar Valley Bank"):
    return store.create_customer(conn, name=name, contact_email="ciso@cvb.example")


def test_issue_signs_a_verifiable_license(conn):
    cid = _customer(conn)
    lic = licensing.issue_license(conn, customer_id=cid, tier="gold",
                                  suites=["fleet"], expires="2027-01-01",
                                  actor="owner@daybreak.co")
    # Verifies with the SAME code a customer runs, under our published public key
    # (public_key_hex() reads the signing key from the env the conftest pins).
    pub = licensing.public_key_hex()
    ok, why = entitlements.verify_license(lic.doc, [pub])
    assert ok and why == "ok"
    assert lic.doc["customer"] == "Cedar Valley Bank" and lic.doc["tier"] == "gold"
    # and it resolves to a live Gold entitlement
    ent = entitlements.resolve(lic.doc, trusted_pubkeys=[pub])
    assert ent.tier == "gold" and ent.suite_enabled("fleet")


def test_upsell_serves_the_newest_license(conn):
    cid = _customer(conn)
    licensing.issue_license(conn, customer_id=cid, tier="gold", suites=["fleet"],
                            actor="a")
    licensing.issue_license(conn, customer_id=cid, tier="platinum", suites=["fleet"],
                            actor="a")
    doc = licensing.active_license_doc(conn, cid)
    assert doc["tier"] == "platinum"          # upsell: newest non-revoked wins


def test_revoke_falls_back_to_prior_active(conn):
    cid = _customer(conn)
    licensing.issue_license(conn, customer_id=cid, tier="gold", actor="a")
    plat = licensing.issue_license(conn, customer_id=cid, tier="platinum", actor="a")
    assert licensing.revoke_license(conn, plat.license_id, actor="a") is True
    assert licensing.active_license_doc(conn, cid)["tier"] == "gold"  # prior one serves


def test_display_status_paths(conn):
    cid = _customer(conn)
    import datetime as dt

    def at(y, m, d):
        return dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp()

    lic = licensing.issue_license(conn, customer_id=cid, tier="gold",
                                  expires="2026-06-01", grace_days=14, actor="a")
    assert licensing.display_status(lic, now=at(2026, 5, 1)) == "active"
    assert licensing.display_status(lic, now=at(2026, 6, 10)) == "grace"
    assert licensing.display_status(lic, now=at(2026, 7, 1)) == "expired"
    perp = licensing.issue_license(conn, customer_id=cid, tier="basic", actor="a")
    assert licensing.display_status(perp) == "perpetual"
    licensing.revoke_license(conn, perp.license_id, actor="a")
    assert licensing.display_status(store.get_license(conn, perp.license_id)) == "revoked"


def test_issue_rejects_unknown_customer_and_tier(conn):
    import pytest
    with pytest.raises(ValueError):
        licensing.issue_license(conn, customer_id=999, tier="gold", actor="a")
    cid = _customer(conn)
    with pytest.raises(ValueError):
        licensing.issue_license(conn, customer_id=cid, tier="diamond", actor="a")
