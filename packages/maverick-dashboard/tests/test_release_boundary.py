"""Adversarial tests for the law-firm deliverable release boundary."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

_TABLE = "| Item | Value |\n| --- | --- |\n| Strategy | privileged |\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    from maverick import world_model

    db = tmp_path / "world.db"
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    return world_model.WorldModel(db)


@pytest.fixture
def client(world, monkeypatch):
    from maverick_dashboard import api as api_mod
    from maverick_dashboard import app as app_mod

    monkeypatch.setattr(api_mod, "_world", lambda: world)
    monkeypatch.setattr(app_mod, "_world", lambda: world)
    monkeypatch.setattr(
        "maverick_dashboard.public_origin.canonical_url",
        lambda path: "https://firm.example/" + str(path or "").lstrip("/"),
    )
    return TestClient(app_mod.app, headers={"Origin": "http://testserver"})


def _release_candidate(
    world,
    *,
    domain="legal_obligations",
    with_matter=True,
    decision="approved",
    decided_by="user:counsel",
):
    goal_id = world.create_goal("Privileged client strategy", "", domain=domain)
    if with_matter:
        reviewer = decided_by or "user:counsel"
        project_id = world.create_client_matter(
            "Client v. Counterparty",
            principal=reviewer,
            domain="legal_obligations",
            matter_number="2026-REL-001",
            jurisdiction="Tennessee",
            client_name="Release Boundary Client",
            adverse_parties=("Release Boundary Counterparty",),
        )
        world.set_goal_project(goal_id, project_id)
    world.set_goal_status(goal_id, "done", result=_TABLE)
    if decision is not None:
        world.record_signoff(
            goal_id,
            decision,
            decided_by=decided_by,
            expected_updated_at=world.get_goal(goal_id).updated_at,
        )
    return goal_id


@pytest.mark.parametrize(
    ("domain", "with_matter", "expected_fragment"),
    [
        ("legal_obligations", False, "belong to a matter"),
        ("", True, "approved legal profile"),
        ("removed_legal_profile", True, "domain policy"),
        ("finance_cashflow", True, "domain policy"),
    ],
)
def test_share_and_csv_refuse_missing_release_policy_facts(
    client, world, domain, with_matter, expected_fragment,
):
    goal_id = _release_candidate(
        world,
        domain=domain,
        with_matter=with_matter,
    )

    shared = client.post(f"/api/v1/goals/{goal_id}/share")
    exported = client.get(f"/api/v1/goals/{goal_id}/deliverable.csv")

    assert shared.status_code == 409
    assert exported.status_code == 409
    assert expected_fragment in shared.json()["detail"]
    assert expected_fragment in exported.json()["detail"]
    assert world.share_links_for_goal(goal_id) == []


def test_known_but_ungated_profile_cannot_release(
    client, world, tmp_path, monkeypatch,
):
    domains = tmp_path / "domains"
    domains.mkdir()
    (domains / "legal_ungated.toml").write_text(
        'name = "legal_ungated"\n'
        'description = "Draft-only legal specialist"\n'
        f'persona = "{"x" * 240}"\n'
        'allow_tools = ["read_file"]\n'
        'deny_tools = ["shell", "write_file"]\n'
        'max_risk = "low"\n'
        '[output]\n'
        'shape = "table"\n'
        'deliverable = "draft advice"\n'
        'consumers = ["attorney"]\n'
        '[[workflow]]\n'
        'name = "Draft"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MAVERICK_DOMAINS_DIR", str(domains))
    goal_id = _release_candidate(world, domain="legal_ungated")

    shared = client.post(f"/api/v1/goals/{goal_id}/share")
    exported = client.get(f"/api/v1/goals/{goal_id}/deliverable.csv")

    assert shared.status_code == 409
    assert exported.status_code == 409
    assert "terminal review gate" in shared.json()["detail"]
    assert world.share_links_for_goal(goal_id) == []


@pytest.mark.parametrize(
    ("decision", "decided_by"),
    [(None, ""), ("rejected", "user:counsel"), ("approved", "")],
)
def test_release_requires_current_named_approval(
    client, world, decision, decided_by,
):
    goal_id = _release_candidate(
        world,
        decision=decision,
        decided_by=decided_by,
    )

    assert client.post(f"/api/v1/goals/{goal_id}/share").status_code == 403
    assert client.get(f"/api/v1/goals/{goal_id}/deliverable.csv").status_code == 403
    assert world.share_links_for_goal(goal_id) == []


def test_valid_matter_gated_approved_release_is_explicit(client, world):
    goal_id = _release_candidate(world)

    exported = client.get(f"/api/v1/goals/{goal_id}/deliverable.csv")
    shared = client.post(f"/api/v1/goals/{goal_id}/share")

    assert exported.status_code == 200
    assert shared.status_code == 201
    assert len(world.share_links_for_goal(goal_id)) == 1


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("client_id", None),
        ("matter_number", ""),
        ("jurisdiction", ""),
    ],
)
def test_release_opaque_when_matter_intake_metadata_is_incomplete(
    client, world, column, value,
):
    goal_id = _release_candidate(world)
    project_id = world.get_goal(goal_id).project_id
    world.conn.execute(
        f"UPDATE projects SET {column} = ? WHERE id = ?",  # noqa: S608 - fixed test cases
        (value, project_id),
    )
    world.conn.commit()

    shared = client.post(f"/api/v1/goals/{goal_id}/share")
    exported = client.get(f"/api/v1/goals/{goal_id}/deliverable.csv")

    expected = "the matter intake record is incomplete and cannot authorize release"
    assert shared.status_code == 409
    assert exported.status_code == 409
    assert shared.json()["detail"] == expected
    assert exported.json()["detail"] == expected
    assert column not in shared.text
    assert "Release Boundary Client" not in shared.text
    assert world.share_links_for_goal(goal_id) == []


def test_copied_public_token_rechecks_matter_and_policy(client, world):
    goal_id = _release_candidate(world)
    created = client.post(f"/api/v1/goals/{goal_id}/share")
    token = created.json()["url"].split("/share/", 1)[1]
    public = TestClient(client.app)
    assert public.get(f"/share/{token}").status_code == 200

    # Filing changes after minting cannot leave a copied public credential live.
    world.set_goal_project(goal_id, None)

    denied = public.get(f"/share/{token}")
    assert denied.status_code == 404
    assert "privileged" not in denied.text.lower()


def test_old_share_never_serves_a_newly_reapproved_revision(client, world):
    goal_id = _release_candidate(world)
    created = client.post(f"/api/v1/goals/{goal_id}/share")
    assert created.status_code == 201, created.text
    token = created.json()["url"].split("/share/", 1)[1]

    world.set_goal_status(goal_id, "done", result=_TABLE.replace("privileged", "new"))
    current = world.get_goal(goal_id)
    world.record_signoff(
        goal_id,
        "approved",
        decided_by="user:counsel",
        expected_updated_at=current.updated_at,
    )

    response = TestClient(client.app).get(f"/share/{token}")
    assert response.status_code == 404
    assert "| Strategy | new |" not in response.text


@pytest.mark.parametrize("endpoint", ["share", "deliverable.csv"])
def test_actual_release_audit_refusal_returns_no_content_or_token(
    client, world, monkeypatch, endpoint,
):
    from maverick_dashboard.release_policy import authorize_goal_release

    goal_id = _release_candidate(world)
    # Finish the separate sign-off outbox first so the refusal below exercises
    # the actual bytes/token release event rather than the review decision.
    authorize_goal_release(world, world.get_goal(goal_id))
    monkeypatch.setattr("maverick.audit.audit_event", lambda *a, **k: False)

    if endpoint == "share":
        response = client.post(f"/api/v1/goals/{goal_id}/share")
    else:
        response = client.get(f"/api/v1/goals/{goal_id}/deliverable.csv")

    assert response.status_code == 503
    assert "privileged" not in response.text.lower()
    assert "/share/" not in response.text
    assert not any(row["active"] for row in world.share_links_for_goal(goal_id))


@pytest.mark.parametrize(
    ("domain", "with_matter", "decision"),
    [
        ("legal_obligations", False, "approved"),
        ("", True, "approved"),
        ("removed_legal_profile", True, "approved"),
        ("legal_obligations", True, None),
    ],
)
def test_manually_minted_legacy_token_cannot_bypass_release_contract(
    client, world, domain, with_matter, decision,
):
    goal_id = _release_candidate(
        world,
        domain=domain,
        with_matter=with_matter,
        decision=decision,
    )
    _, token = world.create_share_link(goal_id, created_by="legacy")

    response = TestClient(client.app).get(f"/share/{token}")

    assert response.status_code == 404
    assert "privileged" not in response.text.lower()
