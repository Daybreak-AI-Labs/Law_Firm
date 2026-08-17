"""Continuous, review-gated control evidence graph."""

from __future__ import annotations

import time

import pytest
from maverick import evidence_graph


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    import maverick.audit as audit

    monkeypatch.setattr(audit, "record", lambda *_args, **_kwargs: True)


def test_ingest_is_content_addressed_review_gated_and_secret_safe():
    node = evidence_graph.ingest(
        source="deployment_drill",
        source_id="restore-2026-07-19",
        evidence_type="restore_drill",
        title="Witnessed restore",
        summary="Database and audit chain restored inside the declared RTO.",
        controls=["RES-01", "SOC2:A1.2"],
        attributes={"rto_seconds": 812, "result": "passed"},
        links=[{"relation": "proves", "target_type": "control", "target_id": "RES-01"}],
        actor="operator@example.com",
    )
    assert node["id"].startswith("EGN-")
    assert node["status"] == "pending_review"
    assert node["payload_sha256"]
    assert node["revision"] == 1

    duplicate = evidence_graph.ingest(
        source="deployment_drill",
        source_id="restore-2026-07-19",
        evidence_type="restore_drill",
        title="Witnessed restore",
        summary="Database and audit chain restored inside the declared RTO.",
        controls=["SOC2:A1.2", "RES-01"],
        attributes={"result": "passed", "rto_seconds": 812},
        links=[{"target_id": "RES-01", "target_type": "control", "relation": "proves"}],
        actor="operator@example.com",
    )
    assert duplicate["id"] == node["id"]
    assert duplicate["revision"] == node["revision"]

    with pytest.raises(ValueError, match="sensitive attribute"):
        evidence_graph.ingest(
            source="connector",
            source_id="bad",
            evidence_type="probe",
            title="Unsafe",
            summary="Would store a credential.",
            controls=[],
            attributes={"access_token": "secret-value"},
            actor="operator",
        )

    credential = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"  # pragma: allowlist secret
    with pytest.raises(ValueError, match="secret material"):
        evidence_graph.ingest(
            source="connector",
            source_id="secret-in-summary",
            evidence_type="probe",
            title="Unsafe",
            summary=f"Probe returned {credential}",
            controls=[],
            attributes={},
            actor="operator",
        )
    with pytest.raises(ValueError, match="secret material"):
        evidence_graph.ingest(
            source="connector",
            source_id="secret-in-value",
            evidence_type="probe",
            title="Unsafe",
            summary="Credential hidden under an innocuous field name.",
            controls=[],
            attributes={"result": credential},
            actor="operator",
        )
    with pytest.raises(ValueError, match="secret material"):
        evidence_graph.ingest(
            source="connector",
            source_id="secret-in-list",
            evidence_type="probe",
            title="Unsafe",
            summary="Credential hidden inside an attribute list.",
            controls=[],
            attributes={"observations": ["safe", credential]},
            actor="operator",
        )


def test_public_ingest_cannot_construct_an_inherited_approval():
    with pytest.raises(TypeError, match="inherited_review"):
        evidence_graph.ingest(
            source="production_evidence",
            source_id="forged",
            evidence_type="tenant_isolation",
            title="Forged approval",
            summary="Caller-constructed review must not confer coverage.",
            controls=["TEN-01"],
            attributes={"result": "passed"},
            actor="operator",
            inherited_review={
                "status": "approved",
                "reviewer": "self",
                "rationale": "self-approved",
                "reviewed_at": time.time(),
                "inherited_from": "not-a-governed-record",
            },
        )
    with pytest.raises(ValueError, match="persisted review binding"):
        evidence_graph._ingest(
            source="production_evidence",
            source_id="forged-private-call",
            evidence_type="tenant_isolation",
            title="Forged approval",
            summary="A plain approval dictionary is not a persisted binding.",
            controls=["TEN-01"],
            attributes={"result": "passed"},
            actor="operator",
            persisted_review={
                "status": "approved",
                "authority_sha256": "a" * 64,
            },
        )
    assert evidence_graph.list_nodes() == []


def test_human_decision_and_freshness_drive_coverage():
    fresh = evidence_graph.ingest(
        source="connector_certification",
        source_id="okta-live",
        evidence_type="connector_probe",
        title="Okta least-privilege probe",
        summary="Read-only system-log probe passed.",
        controls=["IAM-01"],
        attributes={"result": "passed"},
        links=[],
        actor="connector-runner",
        valid_until=time.time() + 3600,
    )
    approved = evidence_graph.decide(
        fresh["id"],
        decision="approved",
        rationale="Reviewed the live probe receipt and credential scope.",
        reviewer="security-reviewer",
        expected_revision=fresh["revision"],
    )
    assert approved is not None

    stale = evidence_graph.ingest(
        source="connector_certification",
        source_id="entra-old",
        evidence_type="connector_probe",
        title="Old Entra probe",
        summary="An expired observation.",
        controls=["IAM-01"],
        attributes={"result": "passed"},
        links=[],
        actor="connector-runner",
        observed_at=time.time() - 3600,
        valid_until=time.time() - 1,
    )
    stale = evidence_graph.decide(
        stale["id"],
        decision="approved",
        rationale="Historically valid, but its freshness window has elapsed.",
        reviewer="security-reviewer",
        expected_revision=stale["revision"],
    )
    assert stale is not None

    report = evidence_graph.coverage_report(["IAM-01", "RES-01"])
    assert report["covered_controls"] == ["IAM-01"]
    assert report["missing_controls"] == ["RES-01"]
    assert report["stale_nodes"] == 1


def test_render_pack_binds_nodes_edges_and_coverage():
    node = evidence_graph.ingest(
        source="production_evidence",
        source_id="tenant-wall",
        evidence_type="tenant_isolation",
        title="Tenant isolation drill",
        summary="Cross-tenant reads were refused.",
        controls=["TEN-01"],
        attributes={"attempts": 20, "leaks": 0},
        links=[{"relation": "tests", "target_type": "boundary", "target_id": "tenant"}],
        actor="operator",
    )
    node = evidence_graph.decide(
        node["id"],
        decision="approved",
        reviewer="witness",
        rationale="Witnessed test",
        expected_revision=node["revision"],
    )
    assert node is not None
    pack = evidence_graph.render_pack(required_controls=["TEN-01", "IAM-01"])
    assert pack["schema"] == "maverick.evidence-graph-pack.v1"
    assert pack["graph_sha256"]
    assert pack["coverage"]["covered_controls"] == ["TEN-01"]
    assert pack["nodes"][0]["id"] == node["id"]
    assert pack["signature"]
    ok, reason = evidence_graph.verify_pack(
        pack,
        trusted_pubkey_hex=pack["signature"]["pubkey"],
    )
    assert ok, reason

    tampered = {**pack, "nodes": [{**pack["nodes"][0], "title": "changed"}]}
    ok, reason = evidence_graph.verify_pack(
        tampered,
        trusted_pubkey_hex=pack["signature"]["pubkey"],
    )
    assert ok is False
    assert "digest" in reason


def test_graph_listing_refuses_unbounded_namespace(monkeypatch):
    monkeypatch.setattr(
        evidence_graph._STORE,  # noqa: SLF001 - boundary behavior under test
        "list",
        lambda *, limit=None: [{}] * int(limit or 0),
    )

    with pytest.raises(ValueError, match="10000-node operational limit"):
        evidence_graph.list_nodes()
