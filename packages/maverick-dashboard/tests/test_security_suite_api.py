"""Security/GRC and defensive-hunter dashboard integration contracts.

The tests keep every durable store under a fresh ``MAVERICK_HOME``. They
exercise the HTTP boundary (including CSRF, RBAC, CAS, and governed approval
binding) rather than duplicating the deterministic core-engine unit tests.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


@pytest.fixture(autouse=True)
def _isolated_security_suite(tmp_path, monkeypatch):
    from maverick import audit, config, world_model
    from maverick.audit import writer as audit_writer
    from maverick_dashboard import api

    home = tmp_path / "maverick-home"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """[security_ops]
enable = true

[threat_hunt]
enable = true

[env_hunt]
enable = true
response_execution = false

[env_hunt.connectors.cloudtrail]
enable = true
push_enable = true
pivot_enable = false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MAVERICK_HOME", str(home))
    monkeypatch.setenv("MAVERICK_CONFIG", str(config_path))
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    for name in (
        "MAVERICK_DASHBOARD_TOKEN",
        "MAVERICK_DASHBOARD_REQUIRE_AUTH",
        "MAVERICK_OIDC_ENABLED",
        "MAVERICK_PROXY_AUTH",
        "MAVERICK_DASHBOARD_INVITES",
        "MAVERICK_TENANT",
        "MAVERICK_TENANT_BY_USER",
        "MAVERICK_APPROVER_KEYS",
        "MAVERICK_APPROVER_KEYS_DIR",
        "MAVERICK_REQUIRE_SIGNED_APPROVAL",
    ):
        monkeypatch.delenv(name, raising=False)

    # Core mutations must be auditable, but these endpoint tests do not need to
    # couple themselves to the audit-writer file format. Dedicated audit tests
    # cover that format; accepting here makes proposal creation deterministic.
    monkeypatch.setattr(audit, "record", lambda *args, **kwargs: True)
    config.reset_config_cache()
    audit_writer._default = None
    audit_writer._defaults.clear()
    api._world_cache.clear()
    from maverick import shield_policy

    monkeypatch.setattr(shield_policy, "shield_available", lambda: True)
    monkeypatch.setattr(shield_policy, "scan_block", lambda _text: None)
    yield {"home": home, "config": config_path}
    config.reset_config_cache()
    audit_writer._default = None
    audit_writer._defaults.clear()
    api._world_cache.clear()


def _assert_ok(response, expected: int = 200) -> dict:
    assert response.status_code == expected, response.text
    return response.json()


def _create_control() -> dict:
    return _assert_ok(
        client.post(
            "/api/v1/security/controls",
            json={
                "framework": "internal",
                "control_id": "SEC-001",
                "title": "Administrative access review",
                "implementation_status": "not_started",
                "owner": "security-owner",
                "applicability_rationale": "Privileged identities are in scope.",
                "crosswalk": ["soc2:CC6.2", "iso27001:A.5.18"],
            },
        ),
        201,
    )


def _create_approved_evidence(control_id: str) -> dict:
    evidence = _assert_ok(
        client.post(
            "/api/v1/security/evidence",
            json={
                "title": "Reviewed security artifact",
                "text": (
                    "SEC-001 requires administrative identities to use MFA and "
                    "undergo a quarterly access review."
                ),
                "source": "test-fixture",
                "control_ids": [control_id],
            },
        ),
        201,
    )
    return _assert_ok(
        client.post(
            f"/api/v1/security/evidence/{evidence['id']}/decision",
            json={
                "decision": "approved",
                "rationale": "Auditor verified the source artifact.",
                "expected_revision": evidence["revision"],
            },
        )
    )


def test_connected_document_evidence_is_scoped_untrusted_and_review_gated(monkeypatch):
    from maverick import doc_discovery

    control = _create_control()
    connector_calls = []
    monkeypatch.setattr(doc_discovery, "enabled", lambda: True)

    def configured_sources(**kwargs):
        connector_calls.append(("configured", kwargs))
        return ["msgraph"]

    def discover(subject, **kwargs):
        connector_calls.append(("discover", {"subject": subject, **kwargs}))
        return [
            doc_discovery.DocHit(
                source="msgraph",
                doc_id="doc-1",
                name="quarterly-access-review.txt",
                ref={"drive_id": "drive-1"},
            )
        ]

    def fetch(source, doc_id, ref=None, **kwargs):
        connector_calls.append(
            ("fetch", {"source": source, "doc_id": doc_id, "ref": ref, **kwargs})
        )
        return (
            b"SEC-001 requires administrative identities to use MFA and "
            b"undergo a quarterly access review.",
            "text/plain",
        )

    monkeypatch.setattr(doc_discovery, "configured_sources", configured_sources)
    monkeypatch.setattr(doc_discovery, "discover", discover)
    monkeypatch.setattr(doc_discovery, "fetch", fetch)

    searched = _assert_ok(
        client.get(
            "/api/v1/security/evidence-documents",
            params={"subject": "administrative access"},
        )
    )
    assert searched["enabled"] is True
    assert searched["hits"][0]["doc_id"] == "doc-1"
    mapped = _assert_ok(
        client.post(
            "/api/v1/security/evidence/from-document",
            json={
                "title": "quarterly-access-review.txt",
                "source": "msgraph",
                "doc_id": "doc-1",
                "ref": {"drive_id": "drive-1"},
                "control_ids": ["SEC-001"],
            },
        ),
        201,
    )
    assert mapped["status"] == "pending_review"
    assert mapped["extraction_confidence"] == "untrusted"
    assert mapped["review_required"] is True
    assert mapped["verdicts"][0]["evidence_quote"]
    assert mapped["document_evidence"]["confidence"] == "untrusted"
    assert mapped["document_evidence"]["review_required"] is True
    assert len(mapped["document_evidence"]["binding_sha256"]) == 64
    assert connector_calls[0] == (
        "configured",
        {"principal": None, "allow_ambient_credentials": True},
    )
    assert connector_calls[-1][1]["max_bytes"] == 16 * 1024 * 1024
    assert connector_calls[-1][1]["allow_ambient_credentials"] is True
    assert control["id"] in {row["control_id"] for row in mapped["verdicts"]}


def test_connected_document_evidence_fails_honestly(monkeypatch):
    from maverick import doc_discovery

    monkeypatch.setattr(doc_discovery, "enabled", lambda: True)
    monkeypatch.setattr(doc_discovery, "configured_sources", lambda **kwargs: [])
    unavailable = _assert_ok(
        client.get(
            "/api/v1/security/evidence-documents",
            params={"subject": "access review"},
        )
    )
    assert unavailable == {"enabled": False, "hits": []}
    assert client.get("/api/v1/security/evidence-documents").status_code == 422

    def failed_fetch(*args, **kwargs):
        raise RuntimeError("connector leaked detail")

    monkeypatch.setattr(doc_discovery, "fetch", failed_fetch)
    failed = client.post(
        "/api/v1/security/evidence/from-document",
        json={"title": "evidence.txt", "source": "msgraph", "doc_id": "doc-1"},
    )
    assert failed.status_code == 502
    assert failed.json() == {"detail": "document source request failed"}
    assert "leaked" not in failed.text


def _environment_attack_body(*, marker: str = "RAW-CREDENTIAL-MARKER-7391") -> dict:
    customer_rule = {
        "title": "Customer console-login watch",
        "id": "customer-console-login",
        "logsource": {"product": "aws", "service": "cloudtrail"},
        "detection": {
            "selection": {"action": "ConsoleLogin"},
            "condition": "selection",
        },
        "tags": ["attack.t1078"],
        "level": "high",
    }
    return {
        "connector": "cloudtrail",
        "events": [
            {
                "eventID": "cloud-login-1",
                "eventTime": "2026-07-19T12:00:00Z",
                "eventName": "ConsoleLogin",
                "userIdentity": {"arn": "arn:aws:iam::123:user/alice"},
                "additionalEventData": {"MFAUsed": "No"},
                "sourceIPAddress": "203.0.113.8",
                "credential_blob": marker,
            },
            {
                "eventID": "cloud-key-1",
                "eventTime": "2026-07-19T12:01:00Z",
                "eventName": "CreateAccessKey",
                "userIdentity": {"arn": "arn:aws:iam::123:user/alice"},
            },
        ],
        "sigma_rules": [json.dumps(customer_rule)],
    }


def _ingest_environment_attack() -> tuple[dict, dict, dict]:
    ingest = _assert_ok(
        client.post("/api/v1/security/soc/ingest", json=_environment_attack_body())
    )
    summary = _assert_ok(client.get("/api/v1/security/soc/summary"))
    investigation = next(
        row for row in summary["investigations"]
        if row.get("response_proposal") and row.get("approval_id")
    )
    proposal = investigation["response_proposal"]
    return ingest, investigation, proposal


def test_security_pages_and_report_render_seeded_program():
    control = _create_control()
    evidence = _create_approved_evidence(control["id"])
    _assert_ok(
        client.post(
            "/api/v1/security/risks",
            json={
                "title": "Privileged credential theft",
                "likelihood": 4,
                "impact": 5,
                "likelihood_rationale": "Privileged identities are routinely targeted.",
                "impact_rationale": "Compromise grants production administrative access.",
                "owner": "ciso",
                "control_ids": [control["id"]],
                "evidence_ids": [evidence["id"]],
            },
        ),
        201,
    )

    page = client.get("/security")
    assert page.status_code == 200, page.text
    assert "Security &amp; GRC" in page.text
    assert "Administrative access review" in page.text
    assert "Privileged credential theft" in page.text
    assert 'href="/security/report"' in page.text

    report = client.get("/security/report")
    assert report.status_code == 200, report.text
    assert "Security &amp; GRC" in report.text
    assert "Print / save PDF" in report.text

    for route, phrase in (
    ):
        response = client.get(route)
        assert response.status_code == 200, response.text
        assert phrase in response.text


def test_control_evidence_human_review_and_revision_cas():
    control = _create_control()
    direct_verdict = client.patch(
        f"/api/v1/security/controls/{control['id']}",
        json={
            "expected_revision": control["revision"],
            "implementation_status": "partial",
        },
    )
    assert direct_verdict.status_code == 422
    updated = _assert_ok(
        client.patch(
            f"/api/v1/security/controls/{control['id']}",
            json={
                "expected_revision": control["revision"],
                "implementation_status": "planned",
            },
        )
    )
    stale = client.patch(
        f"/api/v1/security/controls/{control['id']}",
        json={
            "expected_revision": control["revision"],
            "implementation_status": "not_applicable",
        },
    )
    assert stale.status_code == 409

    evidence = _assert_ok(
        client.post(
            "/api/v1/security/evidence",
            json={
                "title": "Quarterly access review",
                "text": (
                    "SEC-001 requires administrative accounts to be inspected "
                    "quarterly with unique identities and MFA."
                ),
                "source": "test-fixture",
                "control_ids": [control["id"]],
            },
        ),
        201,
    )
    assert evidence["review_required"] is True
    assert evidence["status"] == "pending_review"
    premature = client.post(
        f"/api/v1/security/controls/{control['id']}/evidence",
        json={
            "evidence_id": evidence["id"],
            "implementation_status": "implemented",
            "expected_revision": updated["revision"],
        },
    )
    assert premature.status_code == 422

    approved = _assert_ok(
        client.post(
            f"/api/v1/security/evidence/{evidence['id']}/decision",
            json={
                "decision": "approved",
                "rationale": "Auditor verified the source-system artifact.",
                "expected_revision": evidence["revision"],
            },
        )
    )
    assert approved["decision"]["rationale"] == (
        "Auditor verified the source-system artifact."
    )
    applied = _assert_ok(
        client.post(
            f"/api/v1/security/controls/{control['id']}/evidence",
            json={
                "evidence_id": evidence["id"],
                "implementation_status": "implemented",
                "expected_revision": updated["revision"],
            },
        )
    )
    assert applied["implementation_status"] == "implemented"
    assert applied["evidence_ids"] == [evidence["id"]]
    assert client.get("/api/v1/security/controls/CTL-missing").status_code == 404
    assert client.get("/api/v1/security/evidence/EVD-missing").status_code == 404


def test_risk_poam_policy_and_incident_human_lifecycles():
    now = time.time()
    control = _create_control()
    evidence = _create_approved_evidence(control["id"])
    risk = _assert_ok(
        client.post(
            "/api/v1/security/risks",
            json={
                "title": "Public object storage",
                "likelihood": 5,
                "impact": 5,
                "likelihood_rationale": "The bucket is exposed to anonymous callers.",
                "impact_rationale": "Sensitive customer data could be disclosed.",
                "owner": "ciso",
                "control_ids": [control["id"]],
                "evidence_ids": [evidence["id"]],
            },
        ),
        201,
    )
    treated = _assert_ok(
        client.post(
            f"/api/v1/security/risks/{risk['id']}/treatment",
            json={
                "treatment": "mitigate",
                "plan": "Restrict the bucket and add monitoring.",
                "residual_likelihood": 2,
                "residual_impact": 4,
                "residual_likelihood_rationale": (
                    "Organization policy prevents anonymous access."
                ),
                "residual_impact_rationale": (
                    "Encrypted replicas still require a recovery review."
                ),
                "evidence_ids": [evidence["id"]],
                "owner": "cloud-owner",
                "expected_revision": risk["revision"],
            },
        )
    )
    exception = _assert_ok(
        client.post(
            f"/api/v1/security/risks/{risk['id']}/exception",
            json={
                "owner": "cloud-owner",
                "rationale": "Migration window approved by the CISO.",
                "expires_at": now + 7 * 86400,
                "expected_revision": treated["revision"],
            },
        )
    )
    assert exception["exception"]["rationale"] == (
        "Migration window approved by the CISO."
    )

    poam = _assert_ok(
        client.post(
            "/api/v1/security/poams",
            json={
                "finding": "Restrict public object storage",
                "owner": "cloud-owner",
                "due_at": now + 14 * 86400,
                "milestones": [{"title": "Apply organization policy"}],
            },
        ),
        201,
    )
    in_progress = _assert_ok(
        client.patch(
            f"/api/v1/security/poams/{poam['id']}",
            json={
                "status": "in_progress",
                "note": "Remediation started.",
                "expected_revision": poam["revision"],
            },
        )
    )
    assert in_progress["status"] == "in_progress"

    vendor = _assert_ok(
        client.post(
            "/api/v1/security/vendors",
            json={
                "name": "CloudCo",
                "criticality": "high",
                "owner": "vendor-owner",
                "services": "Production object storage",
                "posture": {
                    "independent_assurance": True,
                    "encryption": True,
                    "mfa": False,
                    "incident_sla": True,
                    "subprocessors": True,
                    "vulnerability_management": True,
                    "business_continuity": True,
                    "data_deletion": True,
                },
            },
        ),
        201,
    )
    assert vendor["status"] == "pending_review"
    assert vendor["review_required"] is True
    vendor_decision = _assert_ok(
        client.post(
            f"/api/v1/security/vendors/{vendor['id']}/decision",
            json={
                "decision": "needs_work",
                "rationale": "Require MFA remediation before approval.",
                "expected_revision": vendor["revision"],
            },
        )
    )
    assert vendor_decision["status"] == "needs_work"

    policy = _assert_ok(
        client.post(
            "/api/v1/security/policies",
            json={"title": "Access policy", "owner": "policy-owner"},
        ),
        201,
    )
    review = _assert_ok(
        client.post(
            f"/api/v1/security/policies/{policy['id']}/transition",
            json={
                "target_status": "review",
                "note": "Ready for review.",
                "expected_revision": policy["revision"],
            },
        )
    )
    approved = _assert_ok(
        client.post(
            f"/api/v1/security/policies/{policy['id']}/transition",
            json={
                "target_status": "approved",
                "note": "CISO approved publication.",
                "expected_revision": review["revision"],
            },
        )
    )
    attested = _assert_ok(
        client.post(
            f"/api/v1/security/policies/{policy['id']}/attest",
            json={
                "subject": "employee:42",
                "statement": "I read and understand this policy.",
                "expected_revision": approved["revision"],
            },
        )
    )
    assert attested["status"] == "attested"

    incident = _assert_ok(
        client.post(
            "/api/v1/security/incidents",
            json={
                "title": "Material service compromise",
                "severity": "high",
                "clock_ids": ["nis2_early_24h"],
                "discovered_at": now,
            },
        ),
        201,
    )
    running = _assert_ok(
        client.post(
            f"/api/v1/security/incidents/{incident['id']}/clock",
            json={
                "clock_id": "nis2_early_24h",
                "anchor_at": now,
                "expected_revision": incident["revision"],
            },
        )
    )
    decided = _assert_ok(
        client.post(
            f"/api/v1/security/incidents/{incident['id']}/notification",
            json={
                "clock_id": "nis2_early_24h",
                "notifiable": True,
                "rationale": "Counsel determined the duty applies.",
                "expected_revision": running["revision"],
            },
        )
    )
    contained = _assert_ok(
        client.post(
            f"/api/v1/security/incidents/{incident['id']}/phase",
            json={
                "phase": "containment",
                "note": "Affected workloads isolated.",
                "expected_revision": decided["revision"],
            },
        )
    )
    eradicated = _assert_ok(
        client.post(
            f"/api/v1/security/incidents/{incident['id']}/phase",
            json={
                "phase": "eradication",
                "note": "Persistence removed and credentials rotated.",
                "expected_revision": contained["revision"],
            },
        )
    )
    recovered = _assert_ok(
        client.post(
            f"/api/v1/security/incidents/{incident['id']}/phase",
            json={
                "phase": "recovery",
                "note": "Service restored from a verified image.",
                "expected_revision": eradicated["revision"],
            },
        )
    )
    closed = _assert_ok(
        client.post(
            f"/api/v1/security/incidents/{incident['id']}/close",
            json={
                "summary": "Recovery validated; follow-up is tracked in POA&M.",
                "expected_revision": recovered["revision"],
            },
        )
    )
    assert closed["status"] == "closed"
    missing = client.post(
        "/api/v1/security/risks/RSK-missing/close",
        json={"rationale": "not present", "expected_revision": 1},
    )
    assert missing.status_code == 404


def test_audit_engagement_nested_records_and_conflicts():
    control = _create_control()
    evidence = _create_approved_evidence(control["id"])
    audit = _assert_ok(
        client.post(
            "/api/v1/security/audits",
            json={
                "name": "SOC 2 readiness",
                "framework": "soc2",
                "scope": "Production platform",
                "owner": "audit-owner",
            },
        ),
        201,
    )
    requested = _assert_ok(
        client.post(
            f"/api/v1/security/audits/{audit['id']}/evidence-requests",
            json={
                "description": "Provide quarterly access-review evidence.",
                "owner": "iam-owner",
                "due_at": time.time() + 30 * 86400,
                "control_ids": [control["id"]],
                "expected_revision": audit["revision"],
            },
        )
    )
    tested = _assert_ok(
        client.post(
            f"/api/v1/security/audits/{audit['id']}/control-tests",
            json={
                "control_id": control["id"],
                "procedure": "Inspect the quarterly access-review sample.",
                "result": "fail",
                "evidence_ids": [evidence["id"]],
                "expected_revision": requested["revision"],
            },
        )
    )
    finding = _assert_ok(
        client.post(
            f"/api/v1/security/audits/{audit['id']}/findings",
            json={
                "title": "Access-review evidence is incomplete",
                "severity": "high",
                "control_ids": [control["id"]],
                "owner": "iam-owner",
                "expected_revision": tested["revision"],
            },
        )
    )
    assert finding["findings"][0]["status"] == "open"
    cannot_complete = client.patch(
        f"/api/v1/security/audits/{audit['id']}/status",
        json={"status": "complete", "expected_revision": finding["revision"]},
    )
    assert cannot_complete.status_code == 422
    stale = client.post(
        f"/api/v1/security/audits/{audit['id']}/control-tests",
        json={
            "control_id": control["id"],
            "procedure": "Inspect another sample.",
            "result": "pass",
            "evidence_ids": [evidence["id"]],
            "expected_revision": audit["revision"],
        },
    )
    assert stale.status_code == 409
    assert client.get("/api/v1/security/audits/AUD-missing").status_code == 404


