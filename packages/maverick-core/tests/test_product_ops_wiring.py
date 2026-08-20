"""Enforcement wiring for the product-operations layer: the opt-in gate
(default off), the require() chokepoint, and the support-bundle export.
See maverick/entitlements.py."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from maverick import entitlements as E

pytest.importorskip("cryptography")

NOW = datetime(2026, 7, 2, tzinfo=timezone.utc)


def _gold(trust_priv):
    payload = dict(customer="Cedar Valley Bank", edition="enterprise", tier="gold",
                   suites=["fleet"], features=[], issued_at="2026-01-01T00:00:00Z",
                   expires_at="2027-01-01T00:00:00Z", grace_days=14, license_id="l1")
    return E.sign_license(payload, trust_priv)


def test_enforcement_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MAVERICK_LICENSE_ENFORCE", raising=False)
    monkeypatch.setattr(E, "_license_cfg", dict)
    assert E.enforcing() is False
    # fail-open: even an unlicensed deployment runs paid features when not enforcing
    monkeypatch.setattr(E, "current", lambda refresh=False: E.Entitlements(status=E.UNLICENSED))
    assert E.require("fleet_memory") is True
    assert E.require_suite("fleet") is True


def test_require_gates_only_paid_under_enforcement(monkeypatch):
    monkeypatch.setenv("MAVERICK_LICENSE_ENFORCE", "1")
    assert E.enforcing() is True
    monkeypatch.setattr(E, "current", lambda refresh=False: E.Entitlements(status=E.UNLICENSED))
    # Nothing is gated in the firm's fork (GATED_FEATURES/GATED_SUITES are empty
    # on purpose), so even with enforcement switched ON and no licence at all,
    # every capability is a core capability and runs. This is the assertion that
    # stops a future edit from quietly reintroducing a paid tier.
    assert E.require("fleet_memory") is True
    assert E.require("some_core_feature") is True
    assert E.require_suite("fleet") is True


def test_enforcement_honours_a_valid_license(monkeypatch):
    priv, pub = E.new_keypair()
    monkeypatch.setenv("MAVERICK_LICENSE_ENFORCE", "1")
    ent = E.resolve(_gold(priv), trusted_pubkeys=[pub], now=NOW)
    monkeypatch.setattr(E, "current", lambda refresh=False: ent)
    assert E.require("fleet_memory") is True
    assert E.require_suite("fleet") is True
    assert E.require("advanced_evolve") is True    # ungated: no tier withholds it


def test_config_knob_supplies_trust_and_enforce(monkeypatch):
    priv, pub = E.new_keypair()
    monkeypatch.delenv("MAVERICK_LICENSE_ENFORCE", raising=False)
    monkeypatch.delenv("MAVERICK_LICENSE_PUBKEYS", raising=False)
    monkeypatch.setattr(E, "_license_cfg",
                        lambda: {"enforce": True, "publisher_pubkeys": [pub]})
    assert E.enforcing() is True
    ent = E.resolve(_gold(priv), now=NOW)   # trust comes from config, not explicit
    assert ent.status == E.LICENSED and ent.allows("fleet_governance")


def test_support_export_writes_redacted_bundle(tmp_path):
    from maverick import support_bundle as S
    out, bundle = S.export(tmp_path / "support.json")
    assert out.exists()
    on_disk = json.loads(out.read_text())
    assert on_disk["correlation_id"].startswith("sup_")
    assert "entitlement" in on_disk and "status" in on_disk["entitlement"]


def test_verify_license_rejects_non_dict_without_crashing():
    # A hostile API / corrupt file returning valid-but-non-object JSON must fail
    # open, not AttributeError at the chokepoint.
    ok, why = E.verify_license(["not", "a", "dict"], ["aa" * 32])
    assert ok is False and why == "malformed"
    ent = E.resolve(["hostile", "array"], trusted_pubkeys=["aa" * 32])
    assert ent.status == E.UNVERIFIED and ent.tier == E.BASE_TIER


def test_resolve_fails_open_on_signed_but_malformed_expiry():
    priv, pub = E.new_keypair()
    doc = E.sign_license(dict(customer="X", tier="gold", suites=[], features=[],
                              issued_at="2026-01-01T00:00:00Z",
                              expires_at="not-a-date", grace_days=14), priv)
    ent = E.resolve(doc, trusted_pubkeys=[pub], now=NOW)
    assert ent.status == E.INVALID and ent.tier == E.BASE_TIER   # signed but unusable


def test_advanced_evolve_respects_entitlement(monkeypatch):
    from maverick import self_improvement as SI
    monkeypatch.setenv("MAVERICK_SELF_IMPROVEMENT", "1")   # base-enabled
    monkeypatch.setattr("maverick.entitlements.require", lambda f: False)
    assert SI.enabled() is False                            # gated off (Platinum)
    monkeypatch.setattr("maverick.entitlements.require", lambda f: True)
    assert SI.enabled() is True                             # granted


def test_custom_pack_factory_respects_entitlement(monkeypatch, tmp_path):
    from maverick import intake
    # gate is BEFORE the approved/write logic, so an un-entitled deployment can't
    # persist a custom pack; drafting/previewing is unaffected (not tested here).
    monkeypatch.setattr("maverick.entitlements.require",
                        lambda f: f != "custom_pack_factory")
    with pytest.raises(PermissionError, match="Platinum"):
        intake.save_profile(object(), approved=True, dest_dir=tmp_path)


def test_multi_tenant_gates_named_tenant_provisioning_only(monkeypatch):
    from maverick import paths
    from maverick.tenant import registry
    # Per-user tenancy is a security isolation control. Once an operator enables
    # it, license state must not silently downgrade channel users into the shared
    # world/memory/audit scope.
    monkeypatch.setenv("MAVERICK_TENANT_BY_USER", "1")
    monkeypatch.setattr("maverick.entitlements.require", lambda f: False)
    assert paths.tenant_by_user_enabled() is True
    # Provisioning a named tenant is still refused under enforcement w/o Platinum.
    monkeypatch.setattr("maverick.entitlements.require",
                        lambda f: f != "multi_tenant")
    with pytest.raises(PermissionError, match="Platinum"):
        registry.create_tenant("acme")




def test_ticket_summary_is_redacted_and_routable():
    from maverick import support_bundle as S
    bundle = {
        "correlation_id": "sup_abc123", "generated_at": "2026-07-02T00:00:00Z",
        "entitlement": {"customer": "Cedar Valley Bank", "tier": "gold",
                        "status": "licensed", "suites": ["fleet"]},
        "versions": {"maverick-agent": "0.1.7"},
        "readiness": {"client_binding": "ok", "shield": "fail: required but unavailable"},
        "recent_failures": {"failure_modes": {"timeout": 3},
                            "failed_jobs": [{"id": 1}, {"id": 2}]},
    }
    s = S.ticket_summary(bundle)
    assert s["correlation_id"] == "sup_abc123" and s["tier"] == "gold"
    assert s["readiness_failing"] == ["shield"] and s["healthy"] is False
    assert s["agent_version"] == "0.1.7" and s["failed_job_count"] == 2
