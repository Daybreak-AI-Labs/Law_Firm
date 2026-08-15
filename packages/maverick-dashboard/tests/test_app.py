"""Dashboard smoke tests.

No network -- just verify the FastAPI app constructs, routes are
registered, and templates render with empty data.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app)


def test_livez():
    """Cheap liveness probe always 200s."""
    resp = client.get("/livez")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_healthz_returns_check_breakdown(monkeypatch):
    """Deep healthz returns a per-check map, may be 200 or 503."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    resp = client.get("/healthz")
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert "checks" in body


def test_readyz_deep_check_client_binding(tmp_path, monkeypatch):
    """/readyz fails (503 not_ready) when client binding is enforced but unset —
    a pod that is up yet refuses all work. A plain /healthz wouldn't catch it."""
    from maverick import client, world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("MAVERICK_CLIENT_ENFORCE", "1")
    monkeypatch.delenv("MAVERICK_CLIENT_ID", raising=False)
    client.reset_client_cache()
    try:
        resp = client_get_readyz()
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        assert "enforced" in body["checks"]["client_binding"]
    finally:
        client.reset_client_cache()


def test_readyz_ok_when_bound(tmp_path, monkeypatch):
    from maverick import client, world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("MAVERICK_CLIENT_ENFORCE", "1")
    monkeypatch.setenv("MAVERICK_CLIENT_ID", "acme")
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    client.reset_client_cache()
    try:
        resp = client_get_readyz()
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        client.reset_client_cache()


def client_get_readyz():
    return client.get("/readyz")


@pytest.mark.parametrize(
    ("module_name", "attribute", "check_name"),
    [
        ("client", "client_binding_enforced", "client_binding"),
        ("shield_policy", "shield_required", "shield"),
        ("agent_trust", "load_trust_state", "agent_trust"),
    ],
)
def test_readiness_deep_check_errors_fail_closed(
    monkeypatch, module_name, attribute, check_name
):
    """An unreadable trust posture is not evidence that a pod is ready."""
    from maverick import agent_trust, shield_policy
    from maverick import client as client_module
    from maverick_dashboard import app as dash_app

    modules = {
        "client": client_module,
        "shield_policy": shield_policy,
        "agent_trust": agent_trust,
    }
    monkeypatch.setattr(client_module, "client_binding_enforced", lambda: False)
    monkeypatch.setattr(shield_policy, "shield_required", lambda: False)
    monkeypatch.setattr(agent_trust, "load_trust_state", lambda: (False, {}))

    def _raise_probe_error():
        raise RuntimeError("probe unavailable")

    monkeypatch.setattr(modules[module_name], attribute, _raise_probe_error)
    ok, checks = dash_app._readiness_deep_checks()

    assert ok is False
    assert checks[check_name] == "unknown: RuntimeError"


def test_index_renders(tmp_path, monkeypatch):
    # Point the WorldModel at a fresh tmp DB so we don't depend on
    # ~/.maverick/world.db existing on the runner.
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Lightwork" in resp.text
    assert "overview" in resp.text or "goals" in resp.text


def test_goals_page_renders(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    resp = client.get("/goals")
    assert resp.status_code == 200


def test_skills_page_renders(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    resp = client.get("/skills")
    assert resp.status_code == 200


def test_compartments_page_renders(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    resp = client.get("/compartments")
    assert resp.status_code == 200
    assert "compartments" in resp.text.lower()
    # the built-in finance pack shows up as a domain in the roster
    assert "finance" in resp.text
    # a pack that declares an output contract surfaces its deliverable
    assert "Delivers:" in resp.text
    assert "Cash-flow &amp; liquidity runway" in resp.text


def test_facts_page_renders(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    resp = client.get("/facts")
    assert resp.status_code == 200
