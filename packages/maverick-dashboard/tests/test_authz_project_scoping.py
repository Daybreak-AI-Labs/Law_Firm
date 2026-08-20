"""Matter authorization: exact membership, no admin bypass, no cross-filing.

Three holes, all in the same surface:

1. ``projects_create`` stored the owner from ``goal_owner_filter``, which
   returns None for an admin -- so every admin-created project was stored
   OWNERLESS.
2. ``project_detail`` admitted ``owner in ("", caller)``, so those ownerless
   projects were readable by any authenticated user. ``list_projects`` filters
   on ``owner = ?`` and never showed them, which is the shape of an IDOR:
   invisible in the index, fetchable by id.
3. The goal list on that page was unfiltered, so it disclosed other users' goal
   titles and statuses; and ``goal_set_project`` authorized the goal but not the
   TARGET project, letting anyone file a goal into a stranger's project.

For a firm this is an ethical-wall question, not a cosmetic one. The project
row is now the physical matter and ``matter_memberships`` is its ACL.

Hermetic in the style of ``test_authz_owner_scoping.py`` -- the bearer token
string is the OIDC subject, no real JWT.
"""
from __future__ import annotations

import maverick_dashboard.auth as auth
import pytest
from fastapi.testclient import TestClient
from maverick.oidc import VerifiedPrincipal
from maverick_dashboard.app import app

client = TestClient(app)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from maverick import world_model

    db = tmp_path / "world.db"
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    from maverick_dashboard import api as api_mod
    from maverick_dashboard import app as app_mod

    api_mod._world_cache.clear()
    app_mod._world_cache.clear()
    return world_model.WorldModel(db)


@pytest.fixture
def oidc(monkeypatch):
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)

    def _verify(token, **_kw):
        return VerifiedPrincipal(
            sub=token, issuer="https://issuer.example", audience="maverick",
            claims={"sub": token},
        )

    monkeypatch.setattr(auth, "verify_oidc_token", _verify)
    # These cases isolate matter ownership and IDOR behavior. Authenticated
    # identities are viewers by default now, so grant the operator baseline
    # explicitly for the filing mutations below.
    from maverick_dashboard import rbac
    monkeypatch.setattr(rbac, "default_role", lambda: "operator")


def _as(user: str, *, post: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {user}"}
    if post:
        headers["Origin"] = "http://testserver"
    return headers


# --- 1. an admin's project is owned, not ownerless -------------------------


def test_global_admin_cannot_open_a_matter_without_counsel_qualification(
    world, oidc, monkeypatch,
):
    monkeypatch.setattr(auth, "is_dashboard_admin", lambda p: p == "user:root")
    monkeypatch.setattr("maverick.audit.audit_event", lambda *a, **k: True)
    r = client.post("/projects", data={
        "name": "Estate of Vance",
        "client_name": "Vance Estate",
        "matter_number": "2026-EST-001",
        "jurisdiction": "Tennessee",
        "domain": "legal",
    },
                    headers=_as("root", post=True), follow_redirects=False)
    assert r.status_code == 403
    assert world.list_projects() == []


# --- 2. an ownerless project is not world-readable -------------------------


def test_ownerless_project_is_not_readable_by_a_stranger(world, oidc):
    pid = world.create_project("Legacy matter", owner="")
    assert client.get(f"/projects/{pid}", headers=_as("mallory")).status_code == 404


def test_a_project_you_own_is_still_readable(world, oidc):
    pid = world.create_project("Vance v. Ortiz", owner="user:alice")
    assert client.get(f"/projects/{pid}", headers=_as("alice")).status_code == 200


def test_another_users_project_404s(world, oidc):
    pid = world.create_project("Vance v. Ortiz", owner="user:alice")
    assert client.get(f"/projects/{pid}", headers=_as("mallory")).status_code == 404


def test_admin_does_not_bypass_another_matters_ethical_wall(world, oidc, monkeypatch):
    monkeypatch.setattr(auth, "is_dashboard_admin", lambda p: p == "user:root")
    pid = world.create_project("Vance v. Ortiz", owner="user:alice")
    assert client.get(f"/projects/{pid}", headers=_as("root")).status_code == 404
    world.add_project_member(pid, "user:root", "viewer", added_by="user:alice")
    assert client.get(f"/projects/{pid}", headers=_as("root")).status_code == 200


# --- 3. the goal list on the page is scoped too ----------------------------


def test_matter_member_sees_the_complete_matter_not_only_their_owned_rows(world, oidc):
    pid = world.create_project("Shared matter", owner="user:alice")
    gid = world.create_goal("Privileged strategy memo", "", owner="user:bob")
    world.set_goal_project(gid, pid)
    mine = world.create_goal("Alice own filing", "", owner="user:alice")
    world.set_goal_project(mine, pid)

    body = client.get(f"/projects/{pid}", headers=_as("alice")).text
    assert "Alice own filing" in body
    assert "Privileged strategy memo" in body


def test_stranger_and_revoked_member_cannot_read_matter_or_goal(world, oidc):
    pid = world.create_project("Shared matter", owner="user:alice")
    gid = world.create_goal("Privileged strategy memo", "", owner="user:alice",
                            project_id=pid)
    world.add_project_member(pid, "user:bob", "staff", added_by="user:alice")
    assert client.get(f"/chat/goal/{gid}", headers=_as("bob")).status_code == 200
    assert client.get(f"/chat/goal/{gid}", headers=_as("mallory")).status_code == 404
    assert world.deactivate_project_member(pid, "user:bob") is True
    assert client.get(f"/projects/{pid}", headers=_as("bob")).status_code == 404
    assert client.get(f"/chat/goal/{gid}", headers=_as("bob")).status_code == 404


def test_responsible_attorney_manages_members_but_staff_and_admin_cannot(
    world, oidc, monkeypatch,
):
    monkeypatch.setattr(auth, "is_dashboard_admin", lambda p: p == "user:root")
    monkeypatch.setattr("maverick.audit.audit_event", lambda *a, **k: True)
    pid = world.create_project("Shared matter", owner="user:alice")
    add = client.post(
        f"/projects/{pid}/members",
        data={"principal": "user:bob", "role": "staff"},
        headers=_as("alice", post=True),
        follow_redirects=False,
    )
    assert add.status_code == 303, add.text
    assert world.project_member_role(pid, "user:bob") == "staff"

    denied = client.post(
        f"/projects/{pid}/members",
        data={"principal": "user:mallory", "role": "viewer"},
        headers=_as("bob", post=True),
        follow_redirects=False,
    )
    assert denied.status_code == 404
    admin_denied = client.post(
        f"/projects/{pid}/members",
        data={"principal": "user:root", "role": "viewer"},
        headers=_as("root", post=True),
        follow_redirects=False,
    )
    assert admin_denied.status_code == 404

    revoke = client.post(
        f"/projects/{pid}/members/user%3Abob/revoke",
        headers=_as("alice", post=True),
        follow_redirects=False,
    )
    assert revoke.status_code == 303, revoke.text
    assert world.project_member_role(pid, "user:bob") is None


@pytest.mark.parametrize("audit_raises", [False, True])
def test_html_matter_access_mutations_fail_closed_when_audit_refuses(
    world, oidc, monkeypatch, audit_raises,
):
    pid = world.create_project("Privileged matter", owner="user:alice")
    world.add_project_member(pid, "user:bob", "staff", added_by="user:alice")

    def unavailable_audit(*_args, **_kwargs):
        if audit_raises:
            raise OSError("audit unavailable")
        return False

    monkeypatch.setattr("maverick.audit.audit_event", unavailable_audit)

    add = client.post(
        f"/projects/{pid}/members",
        data={"principal": "user:mallory", "role": "viewer"},
        headers=_as("alice", post=True),
        follow_redirects=False,
    )
    assert add.status_code == 503
    assert world.project_member_role(pid, "user:mallory") is None

    revoke = client.post(
        f"/projects/{pid}/members/user%3Abob/revoke",
        headers=_as("alice", post=True),
        follow_redirects=False,
    )
    assert revoke.status_code == 503
    assert world.project_member_role(pid, "user:bob") == "staff"

    egress = client.post(
        f"/projects/{pid}/egress",
        data={"egress_mode": "approved_services"},
        headers=_as("alice", post=True),
        follow_redirects=False,
    )
    assert egress.status_code == 503
    assert world.get_project(pid)["egress_mode"] == "local_only"


@pytest.mark.parametrize("audit_raises", [False, True])
def test_law_api_matter_access_mutations_fail_closed_when_audit_refuses(
    world, oidc, monkeypatch, audit_raises,
):
    pid = world.create_project("Privileged matter", owner="user:alice")
    world.add_project_member(pid, "user:bob", "staff", added_by="user:alice")

    def unavailable_audit(*_args, **_kwargs):
        if audit_raises:
            raise OSError("audit unavailable")
        return False

    monkeypatch.setattr("maverick.audit.audit_event", unavailable_audit)

    add = client.post(
        f"/api/v1/matters/{pid}/members",
        json={"principal": "user:mallory", "role": "viewer"},
        headers=_as("alice", post=True),
    )
    assert add.status_code == 503
    assert world.project_member_role(pid, "user:mallory") is None

    revoke = client.delete(
        f"/api/v1/matters/{pid}/members/user%3Abob",
        headers=_as("alice", post=True),
    )
    assert revoke.status_code == 503
    assert world.project_member_role(pid, "user:bob") == "staff"

    egress = client.patch(
        f"/api/v1/matters/{pid}/egress",
        json={"mode": "approved_services"},
        headers=_as("alice", post=True),
    )
    assert egress.status_code == 503
    assert world.get_project(pid)["egress_mode"] == "local_only"


def test_only_responsible_attorney_can_change_matter_egress_policy(
    world, oidc, monkeypatch,
):
    monkeypatch.setattr(auth, "is_dashboard_admin", lambda p: p == "user:root")
    monkeypatch.setattr("maverick.audit.audit_event", lambda *a, **k: True)
    pid = world.create_project("Privileged matter", owner="user:alice")
    world.add_project_member(pid, "user:bob", "attorney", added_by="user:alice")

    for actor in ("bob", "root"):
        denied = client.post(
            f"/projects/{pid}/egress",
            data={"egress_mode": "approved_services"},
            headers=_as(actor, post=True),
            follow_redirects=False,
        )
        assert denied.status_code == 404
        assert world.get_project(pid)["egress_mode"] == "local_only"

    changed = client.post(
        f"/projects/{pid}/egress",
        data={"egress_mode": "approved_services"},
        headers=_as("alice", post=True),
        follow_redirects=False,
    )
    assert changed.status_code == 303
    assert world.get_project(pid)["egress_mode"] == "approved_services"

    invalid = client.post(
        f"/projects/{pid}/egress",
        data={"egress_mode": "any_cloud"},
        headers=_as("alice", post=True),
        follow_redirects=False,
    )
    assert invalid.status_code == 422
    assert world.get_project(pid)["egress_mode"] == "approved_services"


# --- 4. filing a goal authorizes the TARGET project ------------------------


def test_cannot_file_a_goal_into_someone_elses_project(world, oidc):
    victim = world.create_project("Ortiz matter", owner="user:alice")
    gid = world.create_goal("mallory goal", "", owner="user:mallory")

    r = client.post(f"/chat/goal/{gid}/project", data={"project_id": str(victim)},
                    headers=_as("mallory", post=True), follow_redirects=False)
    assert r.status_code == 404
    assert world.get_goal(gid).project_id is None
