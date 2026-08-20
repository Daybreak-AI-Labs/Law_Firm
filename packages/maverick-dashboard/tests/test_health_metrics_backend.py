"""/healthz + /metrics probe the CONFIGURED world backend, not a hard-coded
local world.db -- so a Postgres-backed (HA) deployment reports the DB it
actually uses, and the goal-count gauge reflects the real store.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


def test_unreadable_auth_posture_keeps_probe_details_redacted(monkeypatch):
    from maverick import oidc
    from maverick_dashboard.health_routes import health_should_redact

    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)

    def unavailable():
        raise RuntimeError("auth config unavailable")

    monkeypatch.setattr(oidc, "oidc_enabled", unavailable)
    assert health_should_redact() is True


def _use_sqlite(monkeypatch, tmp_path):
    """Point the dashboard's _world() at an isolated SQLite world."""
    from maverick import world_model
    db = tmp_path / "world.db"
    monkeypatch.setattr(world_model, "DEFAULT_DB", db)
    from maverick_dashboard import app as dash_app
    dash_app._world_cache.clear()
    return world_model.WorldModel(db)


def test_metrics_goal_counts_from_configured_backend(monkeypatch, tmp_path):
    wm = _use_sqlite(monkeypatch, tmp_path)
    g1 = wm.create_goal("a")
    wm.set_goal_status(g1, "done")
    wm.create_goal("b")  # stays pending

    text = client.get("/metrics").text
    assert "# TYPE maverick_goals_total gauge" in text
    assert 'maverick_goals_total{status="done"} 1' in text
    assert 'maverick_goals_total{status="pending"} 1' in text
    assert 'maverick_metrics_backend_up{backend="world"} 1' in text


def test_metrics_exposes_world_backend_failure(monkeypatch, tmp_path):
    _use_sqlite(monkeypatch, tmp_path)

    class _Broken:
        def goal_status_counts(self):
            raise RuntimeError("db down")

    from maverick_dashboard import app as dash_app

    monkeypatch.setattr(dash_app, "_world", lambda: _Broken())
    response = client.get("/metrics")
    assert response.status_code == 200
    assert 'maverick_metrics_backend_up{backend="world"} 0' in response.text


def test_health_and_metrics_use_public_inflight_snapshot(monkeypatch, tmp_path):
    _use_sqlite(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    from maverick import runner

    monkeypatch.setattr(runner, "inflight_goals", lambda: 3)
    # A regression to the semaphore's private ``_value`` would fail loudly.
    monkeypatch.setattr(runner, "_run_semaphore", object())

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["checks"]["runner"] == (
        f"in_flight=3/{runner.MAX_CONCURRENT_GOALS}"
    )
    text = client.get("/metrics").text
    assert "maverick_concurrent_goals 3" in text


def test_healthz_db_ok_via_ping(monkeypatch, tmp_path):
    _use_sqlite(monkeypatch, tmp_path)
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    body = client.get("/healthz").json()
    # No token -> full checks block; the db check uses the backend ping.
    assert body["checks"]["db"] == "ok"


def test_healthz_db_fail_is_reported(monkeypatch, tmp_path):
    _use_sqlite(monkeypatch, tmp_path)
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)

    class _Broken:
        def ping(self):
            raise RuntimeError("db down")

    from maverick_dashboard import app as dash_app
    monkeypatch.setattr(dash_app, "_world", lambda: _Broken())
    resp = client.get("/healthz")
    body = resp.json()
    assert resp.status_code == 503
    assert body["checks"]["db"].startswith("fail")


