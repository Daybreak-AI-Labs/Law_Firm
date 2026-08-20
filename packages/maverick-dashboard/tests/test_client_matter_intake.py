"""Authenticated client intake and conflict-oracle adversarial coverage."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick.oidc import VerifiedPrincipal


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from maverick import world_model
    from maverick_dashboard import api as api_mod
    from maverick_dashboard import app as app_mod
    from maverick_dashboard import auth

    db = tmp_path / "world.db"
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    world = world_model.WorldModel(db)
    monkeypatch.setattr(api_mod, "_world", lambda: world)
    monkeypatch.setattr(app_mod, "_world", lambda: world)
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)

    def verify(token, **_kwargs):
        return VerifiedPrincipal(
            sub=token,
            issuer="https://issuer.example",
            audience="maverick",
            claims={"sub": token},
        )

    roles = {
        "user:alice": "attorney",
        "user:bob": "attorney",
        "user:mallory": "attorney",
        "user:operator": "operator",
        "user:root": "admin",
        "user:dashboard-static-bearer": "attorney",
    }
    monkeypatch.setattr(auth, "verify_oidc_token", verify)
    monkeypatch.setattr(
        auth,
        "qualified_attorney_policy",
        lambda: (
            True,
            frozenset({"user:alice", "user:bob", "user:mallory"}),
        ),
    )
    monkeypatch.setattr(
        auth, "is_dashboard_admin", lambda principal: principal == "user:root",
    )
    monkeypatch.setattr(
        auth, "global_role_for_principal", lambda principal: roles.get(principal, "viewer"),
    )
    audits: list[tuple[str, dict]] = []

    def audit(kind, **payload):
        audits.append((kind, payload))
        return True

    monkeypatch.setattr("maverick.audit.audit_event", audit)
    return TestClient(app_mod.app), world, audits


def _headers(user: str, *, post: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {user}"}
    if post:
        headers["Origin"] = "http://testserver"
    return headers


def _intake(**overrides) -> dict[str, str]:
    data = {
        "name": "Acme v. Jones",
        "client_name": "Acme, Inc.",
        "client_id": "",
        "matter_number": "2026-001",
        "jurisdiction": "Tennessee",
        "domain": "legal_litigation_mgmt",
        "adverse_parties": "Jones LLC\nAlex Jones",
        "description": "Defend the commercial action.",
    }
    data.update(overrides)
    return data


def _seed(world):
    matter_id = world.create_client_matter(
        "Wall Client v. Secret Opponent",
        principal="user:alice",
        domain="legal_litigation_mgmt",
        matter_number="WALL-001",
        jurisdiction="Tennessee",
        client_name="Wall Client LLC",
        adverse_parties=("Secret Opponent LLC",),
    )
    return matter_id, world.get_project(matter_id)["client_id"]


def test_named_attorney_intake_is_atomic_and_never_uses_legacy_create(
    setup, monkeypatch,
):
    client, world, audits = setup

    def legacy_forbidden(*_args, **_kwargs):
        raise AssertionError("authenticated intake used legacy create_project")

    monkeypatch.setattr(world, "create_project", legacy_forbidden)
    response = client.post(
        "/projects",
        data=_intake(),
        headers=_headers("alice", post=True),
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    matter_id = int(response.headers["location"].rsplit("/", 1)[1])
    matter = world.get_project(matter_id)
    assert matter["owner"] == "user:alice"
    assert matter["client_name"] == "Acme, Inc."
    assert matter["matter_number"] == "2026-001"
    assert matter["jurisdiction"] == "Tennessee"
    assert world.project_member_role(matter_id, "user:alice") == "responsible_attorney"
    intake_audit = next(payload for _kind, payload in audits
                        if payload.get("operation") == "open_matter")
    assert intake_audit["actor"] == "user:alice"
    assert intake_audit["candidate_count"] == 3
    assert "Acme" not in repr(intake_audit)
    assert "Jones" not in repr(intake_audit)


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_name": "", "client_id": ""},
        {"matter_number": ""},
        {"jurisdiction": ""},
        {"domain": ""},
        {"domain": "removed_legal_profile"},
        {"domain": "finance_cashflow"},
    ],
)
def test_authenticated_intake_requires_complete_gated_legal_metadata(setup, overrides):
    client, world, _audits = setup
    response = client.post(
        "/projects",
        data=_intake(**overrides),
        headers=_headers("alice", post=True),
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert world.list_projects() == []


def test_operator_and_static_bearer_cannot_open_active_client_matter(
    setup, monkeypatch,
):
    client, world, _audits = setup
    operator = client.post(
        "/projects",
        data=_intake(),
        headers=_headers("operator", post=True),
    )
    assert operator.status_code == 403

    monkeypatch.setenv("MAVERICK_DASHBOARD_TOKEN", "shared-secret")
    static = client.post(
        "/projects",
        data=_intake(),
        headers={
            "Authorization": "Bearer shared-secret",
            "Origin": "http://testserver",
        },
    )
    assert static.status_code == 403
    assert static.json()["detail"] in {
        "insufficient role for this action",
        "qualified counsel authorization required",
    }
    assert world.list_projects() == []


@pytest.mark.parametrize(("user", "expected"), [("mallory", 409), ("root", 403)])
def test_existing_client_reuse_has_no_attorney_or_admin_cross_wall_bypass(
    setup, user, expected,
):
    client, world, audits = setup
    matter_id, client_id = _seed(world)
    before = len(world.list_projects())

    response = client.post(
        "/projects",
        data=_intake(
            name="Hidden client second matter",
            client_name="",
            client_id=str(client_id),
            matter_number="HIDDEN-002",
            adverse_parties="Different Party",
        ),
        headers=_headers(user, post=True),
        follow_redirects=False,
    )

    assert response.status_code == expected
    if expected == 409:
        assert response.json() == {
            "detail": "intake could not be cleared; conflicts-counsel review required",
        }
    else:
        assert response.json() == {
            "detail": "qualified counsel authorization required",
        }
    assert "Wall Client" not in response.text
    assert "Hidden client" not in response.text
    assert str(matter_id) not in response.text
    assert len(world.list_projects()) == before
    if expected == 409:
        intent = audits[-1][1]
        assert intent["operation"] == "open_matter"
        assert intent["client_id"] == client_id
        assert "Wall Client" not in repr(intent)
    else:
        assert audits == []


def test_metadata_and_parties_render_only_after_matter_acl(setup):
    client, world, _audits = setup
    matter_id, _client_id = _seed(world)

    denied = client.get(f"/projects/{matter_id}", headers=_headers("mallory"))
    allowed = client.get(f"/projects/{matter_id}", headers=_headers("alice"))

    assert denied.status_code == 404
    assert "Wall Client" not in denied.text
    assert "Secret Opponent" not in denied.text
    assert allowed.status_code == 200
    assert "Wall Client LLC" in allowed.text
    assert "WALL-001" in allowed.text
    assert "Tennessee" in allowed.text
    assert "Secret Opponent LLC" in allowed.text


def test_party_add_rechecks_responsible_attorney_and_stays_opaque(setup):
    client, world, audits = setup
    matter_id, _client_id = _seed(world)
    world.add_project_member(matter_id, "user:bob", "attorney", added_by="user:alice")

    denied = client.post(
        f"/projects/{matter_id}/parties",
        data={"name": "New Witness", "role": "witness"},
        headers=_headers("bob", post=True),
    )
    conflict = client.post(
        f"/projects/{matter_id}/parties",
        data={"name": "secret-opponent llc", "role": "related"},
        headers=_headers("alice", post=True),
    )
    added = client.post(
        f"/projects/{matter_id}/parties",
        data={"name": "New Witness", "role": "witness"},
        headers=_headers("alice", post=True),
        follow_redirects=False,
    )

    assert denied.status_code == 404
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == (
        "intake could not be cleared; conflicts-counsel review required"
    )
    assert "Secret Opponent" not in conflict.text
    assert added.status_code == 303
    assert any(p["name"] == "New Witness" for p in world.list_matter_parties(matter_id))
    party_audits = [payload for _kind, payload in audits
                    if payload.get("operation") == "add_party"]
    assert len(party_audits) == 2
    assert all(payload["candidate_count"] == 1 for payload in party_audits)
    assert all("Witness" not in repr(payload) for payload in party_audits)


def test_static_bearer_cannot_be_added_to_a_matter_roster(setup):
    client, world, _audits = setup
    matter_id, _client_id = _seed(world)

    response = client.post(
        f"/projects/{matter_id}/members",
        data={
            "principal": "user:dashboard-static-bearer",
            "role": "viewer",
        },
        headers=_headers("alice", post=True),
    )

    assert response.status_code == 422
    assert world.project_member_role(
        matter_id, "user:dashboard-static-bearer",
    ) is None


def test_preflight_is_named_attorney_only_audited_and_opaque(setup, monkeypatch):
    client, world, audits = setup
    _seed(world)

    denied = client.post(
        "/api/v1/conflicts/preflight",
        json={"client_name": "Wall Client LLC"},
        headers=_headers("operator", post=True),
    )
    conflict = client.post(
        "/api/v1/conflicts/preflight",
        json={"client_name": " wall-client llc "},
        headers=_headers("alice", post=True),
    )
    clear = client.post(
        "/api/v1/conflicts/preflight",
        json={"client_name": "Entirely New Client"},
        headers=_headers("alice", post=True),
    )
    monkeypatch.setenv("MAVERICK_DASHBOARD_TOKEN", "shared-secret")
    static = client.post(
        "/api/v1/conflicts/preflight",
        json={"client_name": "Wall Client LLC"},
        headers={
            "Authorization": "Bearer shared-secret",
            "Origin": "http://testserver",
        },
    )

    assert denied.status_code == 403
    assert conflict.status_code == 409
    assert conflict.json() == {
        "detail": "intake could not be cleared; conflicts-counsel review required",
    }
    assert "Wall Client" not in conflict.text
    assert clear.status_code == 200 and clear.json() == {"clear": True}
    assert static.status_code == 403
    preflights = [payload for _kind, payload in audits
                  if payload.get("operation") == "preflight"]
    assert len(preflights) == 2
    assert all(set(payload) <= {"agent", "actor", "operation", "candidate_count"}
               for payload in preflights)
    assert all("Client" not in repr(payload) for payload in preflights)


def test_audit_refusal_blocks_preflight_matter_open_and_party_add(
    setup, monkeypatch,
):
    client, world, _audits = setup
    matter_id, _client_id = _seed(world)
    before_parties = world.list_matter_parties(matter_id)
    monkeypatch.setattr("maverick.audit.audit_event", lambda *_a, **_k: False)

    preflight = client.post(
        "/api/v1/conflicts/preflight",
        json={"client_name": "New Client"},
        headers=_headers("alice", post=True),
    )
    opened = client.post(
        "/projects",
        data=_intake(client_name="Another New Client"),
        headers=_headers("alice", post=True),
    )
    party = client.post(
        f"/projects/{matter_id}/parties",
        data={"name": "New Party", "role": "other"},
        headers=_headers("alice", post=True),
    )

    assert preflight.status_code == 503
    assert opened.status_code == 503
    assert party.status_code == 503
    assert len(world.list_projects()) == 1
    assert world.list_matter_parties(matter_id) == before_parties
