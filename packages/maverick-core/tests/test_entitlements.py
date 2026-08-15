"""Signed-license entitlements: fail-open core, fail-closed paid add-ons,
offline verification, grace window. See maverick/entitlements.py."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from maverick import entitlements as E

pytest.importorskip("cryptography")  # licenses are Ed25519-signed

NOW = datetime(2026, 7, 2, tzinfo=timezone.utc)


def _lic(**kw):
    base = dict(
        customer="Cedar Valley Bank", edition="enterprise", tier="gold",
        suites=["fleet"], features=[], seats=50,
        issued_at="2026-01-01T00:00:00Z", expires_at="2027-01-01T00:00:00Z",
        grace_days=14, license_id="lic_001",
    )
    base.update(kw)
    return base


@pytest.fixture
def keys():
    priv, pub = E.new_keypair()
    return priv, pub, [pub]


def test_valid_gold_license(keys):
    priv, _pub, trust = keys
    e = E.resolve(E.sign_license(_lic(), priv), trusted_pubkeys=trust, now=NOW)
    assert e.status == E.LICENSED and e.tier == "gold"
    assert e.allows("fleet_governance") and e.suite_enabled("fleet")
    assert e.allows("any_core_feature")            # core => fail-open
    assert not e.allows("advanced_evolve")         # platinum feature under gold
    assert not e.tier_at_least("platinum")


def test_unlicensed_runs_core_only(keys):
    _priv, _pub, trust = keys
    e = E.resolve(None, trusted_pubkeys=trust, now=NOW)
    assert e.status == E.UNLICENSED
    assert e.allows("any_core_feature")
    assert not e.allows("fleet_governance") and not e.suite_enabled("fleet")


def test_tamper_is_caught(keys):
    priv, _pub, trust = keys
    doc = E.sign_license(_lic(), priv)
    doc["tier"] = "platinum"  # flip a field after signing
    e = E.resolve(doc, trusted_pubkeys=trust, now=NOW)
    assert e.status == E.INVALID
    assert not e.allows("fleet_governance") and e.allows("core")


def test_untrusted_key_grants_nothing(keys):
    _priv, _pub, trust = keys
    rogue_priv, _rogue_pub = E.new_keypair()
    e = E.resolve(E.sign_license(_lic(), rogue_priv), trusted_pubkeys=trust, now=NOW)
    assert e.status == E.INVALID and e.reason == "untrusted_key"
    assert not e.allows("fleet_governance")


def test_no_trust_anchor_is_unverified(keys):
    priv, _pub, _trust = keys
    e = E.resolve(E.sign_license(_lic(), priv), trusted_pubkeys=[], now=NOW)
    assert e.status == E.UNVERIFIED
    assert e.allows("core") and not e.allows("fleet_governance")


def test_grace_window_keeps_paid_live(keys):
    priv, _pub, trust = keys
    e = E.resolve(E.sign_license(_lic(expires_at="2026-06-25T00:00:00Z"), priv),
                  trusted_pubkeys=trust, now=NOW)  # 7d expired, 14d grace
    assert e.status == E.GRACE and e.allows("fleet_governance")


def test_expired_beyond_grace_gates_paid_not_core(keys):
    priv, _pub, trust = keys
    e = E.resolve(E.sign_license(_lic(expires_at="2026-06-01T00:00:00Z"), priv),
                  trusted_pubkeys=trust, now=NOW)  # 31d expired > grace
    assert e.status == E.EXPIRED
    assert not e.allows("fleet_governance") and e.allows("core")


def test_platinum_unlocks_platinum(keys):
    priv, _pub, trust = keys
    e = E.resolve(E.sign_license(_lic(tier="platinum"), priv), trusted_pubkeys=trust, now=NOW)
    assert e.tier_at_least("platinum") and e.allows("advanced_evolve")
    assert e.allows("fleet_governance")  # higher tier includes lower


def test_file_roundtrip(tmp_path, keys):
    priv, _pub, trust = keys
    f = tmp_path / "license.json"
    f.write_text(json.dumps(E.sign_license(_lic(), priv)))
    e = E.load_entitlements(f, trusted_pubkeys=trust, now=NOW)
    assert e.status == E.LICENSED and e.summary().startswith("Cedar Valley Bank")


def test_missing_file_is_unlicensed(tmp_path, keys):
    _priv, _pub, trust = keys
    e = E.load_entitlements(tmp_path / "nope.json", trusted_pubkeys=trust, now=NOW)
    assert e.status == E.UNLICENSED and e.allows("core")


# ---- checkbox semantics: à-la-carte grants + explicit denials ---------------

def test_a_la_carte_grant_unlocks_gated_feature_below_tier(keys):
    priv, _pub, trust = keys
    doc = E.sign_license(_lic(tier="gold", features=["advanced_evolve"]), priv)
    e = E.resolve(doc, trusted_pubkeys=trust, now=NOW)
    assert e.allows("advanced_evolve")           # platinum feature, gold + grant
    assert not e.allows("custom_pack_factory")   # other platinum stays off
    assert not e.tier_at_least("platinum")       # the tier itself is unchanged


def test_explicit_denial_wins_over_tier_and_grant(keys):
    priv, _pub, trust = keys
    doc = E.sign_license(_lic(tier="gold", features=["siem_export"],
                              features_denied=["siem_export", "fleet_memory"]), priv)
    e = E.resolve(doc, trusted_pubkeys=trust, now=NOW)
    assert not e.allows("siem_export")     # denied despite tier AND grant
    assert not e.allows("fleet_memory")    # denied despite tier
    assert e.allows("fleet_governance")    # untouched gold feature stays on
    assert e.allows("any_core_feature")    # denial never reaches core fail-open


def test_denial_does_not_outlive_the_license(keys):
    priv, _pub, trust = keys
    doc = E.sign_license(_lic(expires_at="2026-06-01T00:00:00Z",
                              features_denied=["siem_export"]), priv)
    e = E.resolve(doc, trusted_pubkeys=trust, now=NOW)  # 31d expired > grace
    assert e.status == E.EXPIRED
    assert e.features_denied == ()          # dropped with the rest of the paid state
    assert e.allows("core")                 # fail-open unchanged


def test_denial_of_unregistered_feature_binds_while_live(keys):
    priv, _pub, trust = keys
    doc = E.sign_license(_lic(features_denied=["bespoke_export"]), priv)
    e = E.resolve(doc, trusted_pubkeys=trust, now=NOW)
    assert not e.allows("bespoke_export")   # a signed denial gates any chokepoint
    assert e.allows("other_core_feature")


# ---- background auto-refresh -------------------------------------------------

def test_refresh_interval_disabled_without_api(monkeypatch):
    monkeypatch.delenv("MAVERICK_LICENSE_API", raising=False)
    monkeypatch.setattr(E, "_license_cfg", dict)
    assert E.refresh_interval_seconds() == 0.0


def test_refresh_interval_defaults_and_clamps(monkeypatch):
    monkeypatch.setenv("MAVERICK_LICENSE_API", "https://console.example/api/v1/license")
    monkeypatch.delenv("MAVERICK_LICENSE_REFRESH_INTERVAL", raising=False)
    monkeypatch.setattr(E, "_license_cfg", dict)
    assert E.refresh_interval_seconds() == E.DEFAULT_REFRESH_INTERVAL
    monkeypatch.setenv("MAVERICK_LICENSE_REFRESH_INTERVAL", "5")   # too hot → floor
    assert E.refresh_interval_seconds() == 30.0
    monkeypatch.setenv("MAVERICK_LICENSE_REFRESH_INTERVAL", "0")   # explicit off
    assert E.refresh_interval_seconds() == 0.0
    monkeypatch.setenv("MAVERICK_LICENSE_REFRESH_INTERVAL", "junk")
    assert E.refresh_interval_seconds() == E.DEFAULT_REFRESH_INTERVAL


def test_refresher_polls_and_survives_errors():
    import threading
    calls = []
    seen_two = threading.Event()

    class _Res:
        changed = False

    def fake_refresh():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("network blip")   # must not kill the loop
        seen_two.set()
        return _Res()

    t = E.start_refresher(0.01, refresh=fake_refresh)
    try:
        assert t is not None
        assert seen_two.wait(timeout=5.0)        # kept polling after the error
    finally:
        E.stop_refresher()
    assert len(calls) >= 2


def test_refresher_is_singleton_and_disabled_at_zero():
    assert E.start_refresher(0) is None          # interval 0 → off
    t = E.start_refresher(60)
    try:
        assert t is not None
        assert E.start_refresher(60) is None     # second start is a no-op
    finally:
        E.stop_refresher()
