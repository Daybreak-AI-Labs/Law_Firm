"""Run tree on the dashboard: the read-only counterfactual view and its JSON
endpoint — forked runs listed, branches nested, empty state honest, and the
same owner scoping every other goal surface uses."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from maverick import config, session_tree, world_model
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "0")
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setattr(session_tree, "registry_path",
                        lambda: tmp_path / "session_tree.json")
    # api.py and app.py keep separate per-path WorldModel caches; drop both so
    # the new DEFAULT_DB binds on this test's first request.
    from maverick_dashboard import api as api_mod
    from maverick_dashboard import app as app_mod
    api_mod._world_cache.clear()
    app_mod._world_cache.clear()
    config.reset_config_cache()
    yield
    config.reset_config_cache()


def _forked_run(owner="ada@corp.test"):
    """A parent run with a trail, plus one branch off its second event."""
    from maverick import session_tree
    from maverick.world_model import open_world
    world = open_world()
    gid = world.create_goal("ship the migration", "cutover", owner=owner)
    ids = [world.append_event(gid, "planner", "status", s)
           for s in ("planned", "called the API", "wrote the file")]
    child = session_tree.fork(gid, at_event=ids[1], label="blue/green instead",
                              forked_by=owner)
    return gid, child, ids


def test_page_renders_a_forked_run_and_its_branch():
    gid, child, ids = _forked_run()
    r = client.get("/run-tree")
    assert r.status_code == 200
    body = r.text
    assert "Run tree" in body
    assert f"#{gid}" in body and f"#{child}" in body
    assert "blue/green instead" in body
    assert f"forked at event #{ids[1]}" in body
    # The nesting is real markup, not a flat list.
    assert "loop(" not in body and "<ul>" in body


def test_page_renders_the_empty_state_with_no_forks():
    r = client.get("/run-tree")
    assert r.status_code == 200
    assert "No runs have been forked yet" in r.text
    assert "0 forked runs" in r.text


def test_api_returns_the_tree_json():
    gid, child, ids = _forked_run()
    r = client.get(f"/api/v1/run-tree/{gid}")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["root"] == gid
    assert body["lineage"] == {
        "goal_id": gid, "parent": None, "children": [child], "depth": 0,
        "root": gid, "label": "", "forked_at_event": None}
    tree = body["tree"]
    assert tree["goal_id"] == gid and len(tree["children"]) == 1
    branch = tree["children"][0]
    assert branch["goal_id"] == child and branch["label"] == "blue/green instead"
    assert branch["forked_at_event"] == ids[1] and branch["children"] == []


def test_api_answers_from_the_root_when_asked_about_a_branch():
    gid, child, _ = _forked_run()
    body = client.get(f"/api/v1/run-tree/{child}").json()
    assert body["goal_id"] == child and body["root"] == gid
    assert body["lineage"]["parent"] == gid and body["lineage"]["depth"] == 1
    assert body["tree"]["goal_id"] == gid   # the whole tree, both branches


def test_api_404s_an_unknown_run():
    assert client.get("/api/v1/run-tree/999999").status_code == 404


def test_owner_scoping_hides_another_principal_s_run(monkeypatch):
    # Same rule as every other goal surface: an authenticated non-admin sees
    # only runs they own, and a foreign run is a 404, not a 403. Hermetic OIDC
    # seam (as in test_authz_owner_scoping): `Bearer ada` -> principal user:ada.
    import maverick_dashboard.auth as auth
    from maverick.oidc import VerifiedPrincipal
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(auth, "verify_oidc_token", lambda token, **_kw: (
        VerifiedPrincipal(sub=token, issuer="https://issuer.example",
                          audience="maverick", claims={"sub": token})))
    gid, child, _ = _forked_run(owner="user:ada")

    def _as(user):
        return {"Origin": "http://testserver", "Authorization": f"Bearer {user}"}

    assert client.get(f"/api/v1/run-tree/{gid}", headers=_as("bob")).status_code == 404
    assert "No runs have been forked yet" in client.get(
        "/run-tree", headers=_as("bob")).text
    # The owner still sees the whole tree.
    owned = client.get(f"/api/v1/run-tree/{gid}", headers=_as("ada"))
    assert owned.status_code == 200
    assert owned.json()["tree"]["children"][0]["goal_id"] == child
    assert f"#{child}" in client.get("/run-tree", headers=_as("ada")).text
