"""Enforcement wiring for the product-operations layer: the opt-in gate
(default off), the require() chokepoint, the fleet_memory integration, and the
support-bundle export. See maverick/entitlements.py + fleet_memory.py."""
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


def _license(trust_priv, *, customer="Cedar Valley Bank",
             issued="2026-06-01T00:00:00Z", tier="gold"):
    payload = dict(customer=customer, edition="enterprise", tier=tier,
                   suites=["fleet"], features=[], issued_at=issued,
                   expires_at="2027-01-01T00:00:00Z", grace_days=14,
                   license_id="lic_" + issued.replace(":", ""))
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
    assert E.require("fleet_memory") is False      # paid gated off
    assert E.require("some_core_feature") is True  # core still runs
    assert E.require_suite("fleet") is False


def test_enforcement_honours_a_valid_license(monkeypatch):
    priv, pub = E.new_keypair()
    monkeypatch.setenv("MAVERICK_LICENSE_ENFORCE", "1")
    ent = E.resolve(_gold(priv), trusted_pubkeys=[pub], now=NOW)
    monkeypatch.setattr(E, "current", lambda refresh=False: ent)
    assert E.require("fleet_memory") is True
    assert E.require_suite("fleet") is True
    assert E.require("advanced_evolve") is False   # platinum feature, gold license


def test_config_knob_supplies_trust_and_enforce(monkeypatch):
    priv, pub = E.new_keypair()
    monkeypatch.delenv("MAVERICK_LICENSE_ENFORCE", raising=False)
    monkeypatch.delenv("MAVERICK_LICENSE_PUBKEYS", raising=False)
    monkeypatch.setattr(E, "_license_cfg",
                        lambda: {"enforce": True, "publisher_pubkeys": [pub]})
    assert E.enforcing() is True
    ent = E.resolve(_gold(priv), now=NOW)   # trust comes from config, not explicit
    assert ent.status == E.LICENSED and ent.allows("fleet_governance")


def test_fleet_memory_respects_entitlement(monkeypatch):
    from maverick import fleet_memory as FM
    monkeypatch.setenv("MAVERICK_FLEET_MEMORY", "1")   # base-enabled
    monkeypatch.setattr("maverick.entitlements.require", lambda f: False)
    assert FM.enabled() is False                        # gated off
    monkeypatch.setattr("maverick.entitlements.require", lambda f: True)
    assert FM.enabled() is True                         # granted


def test_support_export_writes_redacted_bundle(tmp_path):
    from maverick import support_bundle as S
    out, bundle = S.export(tmp_path / "support.json")
    assert out.exists()
    on_disk = json.loads(out.read_text())
    assert on_disk["correlation_id"].startswith("sup_")
    assert "entitlement" in on_disk and "status" in on_disk["entitlement"]


def test_refresh_no_api_is_a_noop(monkeypatch):
    monkeypatch.delenv("MAVERICK_LICENSE_API", raising=False)
    monkeypatch.setattr(E, "_license_cfg", dict)
    monkeypatch.setattr(E, "current", lambda refresh=False: E.Entitlements(status=E.UNLICENSED))
    res = E.refresh_from_server()
    assert res.ok is False and "no entitlement API" in res.reason


def test_refresh_network_error_is_fail_open(monkeypatch):
    monkeypatch.setattr(E, "current", lambda refresh=False: E.Entitlements(status=E.LICENSED))

    def _boom():
        raise OSError("connection refused")

    res = E.refresh_from_server("https://api.example/license", fetch=_boom)
    assert res.ok is False and "fetch failed" in res.reason
    assert res.status == E.LICENSED           # running entitlement untouched


def test_refresh_rejects_an_unverified_server_reply(monkeypatch, tmp_path):
    priv, _pub = E.new_keypair()             # signed, but by an UNTRUSTED key
    monkeypatch.setenv("MAVERICK_LICENSE_FILE", str(tmp_path / "license.json"))
    forged = _gold(priv)
    res = E.refresh_from_server("https://api.example/license",
                                trusted_pubkeys=["00" * 32], fetch=lambda: forged)
    assert res.ok is False and res.changed is False
    assert not (tmp_path / "license.json").exists()   # never persisted


def test_refresh_persists_a_verified_license(monkeypatch, tmp_path):
    priv, pub = E.new_keypair()
    monkeypatch.setenv("MAVERICK_LICENSE_FILE", str(tmp_path / "license.json"))
    doc = _gold(priv)
    res = E.refresh_from_server("https://api.example/license",
                                trusted_pubkeys=[pub], fetch=lambda: doc)
    assert res.ok and res.changed
    saved = json.loads((tmp_path / "license.json").read_text())
    assert saved["customer"] == "Cedar Valley Bank" and saved["tier"] == "gold"


def test_siem_forward_gated_off_under_enforcement(monkeypatch):
    from maverick.audit import forwarder as F
    monkeypatch.setattr("maverick.entitlements.require",
                        lambda f: f != "siem_export")
    with pytest.raises(PermissionError, match="siem_export"):
        F.forward(["evt"], "https://siem.example/collector")


def test_siem_forward_open_when_licensed(monkeypatch):
    from maverick.audit import forwarder as F
    monkeypatch.setattr("maverick.entitlements.require", lambda f: True)
    sent = {}
    monkeypatch.setattr(F, "_send_http", lambda url, lines, timeout: sent.setdefault(
        "n", len(list(lines))))
    n = F.forward(["a", "b"], "https://siem.example/collector")
    assert n == 2 and sent["n"] == 2         # passed the gate, reached transport


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


def test_refresh_refuses_a_replayed_older_license(monkeypatch, tmp_path):
    priv, pub = E.new_keypair()
    monkeypatch.setenv("MAVERICK_LICENSE_FILE", str(tmp_path / "license.json"))
    (tmp_path / "license.json").write_text(json.dumps(
        _license(priv, issued="2026-06-01T00:00:00Z")))          # installed floor
    older = _license(priv, issued="2026-01-01T00:00:00Z")        # replayed downgrade
    res = E.refresh_from_server("https://api.example/license",
                                trusted_pubkeys=[pub], fetch=lambda: older)
    assert res.ok is False and "rollback" in res.reason
    kept = json.loads((tmp_path / "license.json").read_text())
    assert kept["issued_at"] == "2026-06-01T00:00:00Z"           # floor untouched


def test_refresh_refuses_a_different_customer(monkeypatch, tmp_path):
    priv, pub = E.new_keypair()
    monkeypatch.setenv("MAVERICK_LICENSE_FILE", str(tmp_path / "license.json"))
    (tmp_path / "license.json").write_text(json.dumps(
        _license(priv, customer="Cedar Valley Bank")))
    other = _license(priv, customer="Someone Else", issued="2027-06-01T00:00:00Z")
    res = E.refresh_from_server("https://api.example/license",
                                trusted_pubkeys=[pub], fetch=lambda: other)
    assert res.ok is False and "different customer" in res.reason


def test_refresh_allows_a_genuine_upgrade(monkeypatch, tmp_path):
    priv, pub = E.new_keypair()
    monkeypatch.setenv("MAVERICK_LICENSE_FILE", str(tmp_path / "license.json"))
    (tmp_path / "license.json").write_text(json.dumps(
        _license(priv, tier="gold", issued="2026-01-01T00:00:00Z")))
    newer = _license(priv, tier="platinum", issued="2026-06-01T00:00:00Z")
    res = E.refresh_from_server("https://api.example/license",
                                trusted_pubkeys=[pub], fetch=lambda: newer)
    assert res.ok
    assert json.loads((tmp_path / "license.json").read_text())["tier"] == "platinum"


def test_refresh_refuses_a_file_scheme_api_url(monkeypatch, tmp_path):
    # a file:// api_url must not become a local-file read (SSRF/LFI); refresh
    # reports a fetch failure and persists nothing.
    monkeypatch.setenv("MAVERICK_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.delenv("MAVERICK_LICENSE_API_TOKEN", raising=False)
    res = E.refresh_from_server("file:///etc/passwd", trusted_pubkeys=["aa" * 32])
    assert res.ok is False and "fetch failed" in res.reason
    assert not (tmp_path / "license.json").exists()


def test_fleet_governance_respects_entitlement(monkeypatch):
    from maverick import fleet as F
    monkeypatch.setattr("maverick.entitlements.require", lambda f: f != "fleet_governance")
    assert F.governance_enabled() is False       # gated off (Gold feature)
    monkeypatch.setattr("maverick.entitlements.require", lambda f: True)
    assert F.governance_enabled() is True         # granted


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


def test_support_export_emits_a_redacted_audit_event(monkeypatch, tmp_path):
    import maverick.cli as C
    from click.testing import CliRunner
    calls = []
    monkeypatch.setattr("maverick.audit.record",
                        lambda kind, **kw: calls.append((kind, kw)) or True)
    out = tmp_path / "bundle.json"
    res = CliRunner().invoke(C.main, ["support", "-o", str(out)])
    assert res.exit_code == 0, res.output
    assert out.exists()
    ev = [kw for kind, kw in calls if kind == "support_bundle_exported"]
    assert len(ev) == 1                                    # exactly one export event
    assert ev[0].get("correlation_id", "").startswith("sup_")
    assert ev[0].get("filename") == "bundle.json"          # basename only, no path


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
