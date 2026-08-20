"""Signed-license entitlements: fail-open core, fail-closed paid add-ons,
offline verification, grace window. See maverick/entitlements.py."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

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
    assert not e.tier_at_least("platinum")


def test_unlicensed_runs_core_only(keys):
    _priv, _pub, trust = keys
    e = E.resolve(None, trusted_pubkeys=trust, now=NOW)
    assert e.status == E.UNLICENSED
    assert e.allows("any_core_feature")
    assert e.suite_enabled("fleet")       # nothing is gated in the firm's fork


def test_tamper_is_caught(keys):
    priv, _pub, trust = keys
    doc = E.sign_license(_lic(), priv)
    doc["tier"] = "platinum"  # flip a field after signing
    e = E.resolve(doc, trusted_pubkeys=trust, now=NOW)
    assert e.status == E.INVALID
    assert e.allows("core")


def test_untrusted_key_grants_nothing(keys):
    _priv, _pub, trust = keys
    rogue_priv, _rogue_pub = E.new_keypair()
    e = E.resolve(E.sign_license(_lic(), rogue_priv), trusted_pubkeys=trust, now=NOW)
    assert e.status == E.INVALID and e.reason == "untrusted_key"
    assert e.status == E.INVALID


def test_no_trust_anchor_is_unverified(keys):
    priv, _pub, _trust = keys
    e = E.resolve(E.sign_license(_lic(), priv), trusted_pubkeys=[], now=NOW)
    assert e.status == E.UNVERIFIED
    assert e.allows("core")


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
    assert e.allows("core")


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
    assert e.allows("advanced_evolve")           # explicit grant still recorded
    assert not e.tier_at_least("platinum")       # the tier itself is unchanged


def test_explicit_denial_wins_over_tier_and_grant(keys):
    priv, _pub, trust = keys
    doc = E.sign_license(_lic(tier="gold", features=["legacy_export"],
                              features_denied=["legacy_export", "fleet_memory"]), priv)
    e = E.resolve(doc, trusted_pubkeys=trust, now=NOW)
    assert not e.allows("legacy_export")   # denied despite tier AND grant
    assert not e.allows("fleet_memory")    # denied despite tier
    assert e.allows("fleet_governance")    # untouched gold feature stays on
    assert e.allows("any_core_feature")    # denial never reaches core fail-open


def test_denial_does_not_outlive_the_license(keys):
    priv, _pub, trust = keys
    doc = E.sign_license(_lic(expires_at="2026-06-01T00:00:00Z",
                              features_denied=["legacy_export"]), priv)
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


def test_connected_entitlement_refresh_is_physically_absent(capsys):
    retired = {
        "RefreshResult",
        "_http_get_json",
        "_rollback_reason",
        "refresh_from_server",
        "refresh_interval_seconds",
        "start_refresher",
        "stop_refresher",
    }
    assert all(not hasattr(E, name) for name in retired)

    source = Path(E.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "urllib.request",
        "urlopen(",
        "MAVERICK_LICENSE_API",
        "MAVERICK_LICENSE_API_TOKEN",
        "MAVERICK_LICENSE_REFRESH_INTERVAL",
        'add_parser("refresh"',
    ):
        assert forbidden not in source

    with pytest.raises(SystemExit) as exc:
        E.main(["refresh"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
