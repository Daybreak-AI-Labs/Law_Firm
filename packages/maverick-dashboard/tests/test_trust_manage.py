"""Agent Trust Plane management from the app: register / revoke / restore /
remove external agents via the admin API, writing the managed overlay
(agent_trust.json) -- never the operator's config file."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


@pytest.fixture(autouse=True)
def _world(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")


@pytest.fixture(autouse=True)
def managed(tmp_path, monkeypatch):
    """Point the managed registry overlay at a per-test file."""
    from maverick import agent_trust
    monkeypatch.setattr(agent_trust, "managed_path",
                        lambda: tmp_path / "agent_trust.json")


def _register(**overrides):
    entry = {"id": "partner-copilot", "direction": "inbound",
             "allow_tools": ["research"], "max_risk": "low",
             "max_dollars": 2.0}
    entry.update(overrides)
    return client.post("/api/v1/trust/agents", json=entry)


def test_register_and_list_roundtrip():
    r = _register()
    assert r.status_code == 201
    assert r.json() == {"ok": True, "id": "partner-copilot"}
    agents = client.get("/api/v1/trust/agents").json()["agents"]
    (a,) = [x for x in agents if x["id"] == "partner-copilot"]
    assert a["direction"] == "inbound"
    assert a["allow_tools"] == ["research"]
    assert a["max_risk"] == "low"
    assert a["max_dollars"] == 2.0
    assert a["revoked"] is False


def test_register_rejects_bad_entries():
    # malformed pubkey -> clear 400, not a silent drop
    assert _register(pubkey="not-hex").status_code in (400, 422)
    # bad id charset -> validation failure (pydantic or registry)
    assert _register(id="No Spaces Allowed").status_code in (400, 422)
    # bad direction is schema-gated
    assert _register(direction="sideways").status_code == 422


def test_revoke_restore_and_remove():
    assert _register().status_code == 201
    r = client.post("/api/v1/trust/agents/partner-copilot/revoke",
                    json={"revoked": True})
    assert r.status_code == 200
    agents = client.get("/api/v1/trust/agents").json()["agents"]
    assert [a["revoked"] for a in agents if a["id"] == "partner-copilot"] == [True]

    r = client.post("/api/v1/trust/agents/partner-copilot/revoke",
                    json={"revoked": False})
    assert r.status_code == 200
    agents = client.get("/api/v1/trust/agents").json()["agents"]
    assert [a["revoked"] for a in agents if a["id"] == "partner-copilot"] == [False]

    assert client.delete("/api/v1/trust/agents/partner-copilot").status_code == 204
    agents = client.get("/api/v1/trust/agents").json()["agents"]
    assert not [a for a in agents if a["id"] == "partner-copilot"]


def test_unmanaged_agent_actions_explain_the_config_file():
    # No managed entry: revoke/remove 404 with a human explanation instead of
    # silently succeeding (config-file entries are not editable from the app).
    r = client.post("/api/v1/trust/agents/config-only/revoke",
                    json={"revoked": True})
    assert r.status_code == 404
    assert "config" in r.json()["detail"]
    assert client.delete("/api/v1/trust/agents/config-only").status_code == 404


def test_trust_page_has_register_form_and_row_actions():
    _register()
    page = client.get("/trust").text
    assert "Register an external agent" in page
    assert 'id="trust-add"' in page
    assert "data-trust-revoke" in page
    assert "data-trust-remove" in page


def test_mutations_are_csrf_gated():
    # no Origin/Referer on a mutating call in no-token mode -> blocked
    bare = TestClient(app)
    assert bare.post("/api/v1/trust/agents",
                     json={"id": "x"}).status_code == 403
