"""Public perf dashboard: /api/v1/perf + the page."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

import maverick.world_model as world_model  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from maverick_dashboard import api as api_mod  # noqa: E402
from maverick_dashboard.app import app  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    import maverick_dashboard.api as api
    monkeypatch.setattr(api, "_PERF_SLA_CACHE", None)
    return TestClient(app, headers={"Origin": "http://testserver"})


def test_perf_api_shape(client):
    r = client.get("/api/v1/perf")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d["sla"], list) and d["sla"], "SLA rows measured live"
    for row in d["sla"]:
        assert {"name", "measured", "threshold", "passed"} <= set(row)


def test_perf_api_carries_no_benchmark_history(client):
    """The endpoint is SLA-only.

    It used to fold in recorded benchmark scores and a longitudinal era
    retrospective; both harnesses went with the prune, so the keys went too
    rather than being served permanently empty.
    """
    d = client.get("/api/v1/perf").json()
    assert set(d) <= {"sla", "sla_error"}


def test_perf_api_caches_live_sla(client, monkeypatch, tmp_path):
    import maverick.continuous_benchmark as cb
    import maverick.perf_sla as perf_sla
    from maverick.perf_sla import SLAResult

    calls = 0

    def fake_run_all():
        nonlocal calls
        calls += 1
        return [SLAResult("fake", 1.0, 2.0, "ms")]

    monkeypatch.setattr(cb, "_store_path", lambda: tmp_path / "nope")
    monkeypatch.setattr(perf_sla, "run_all", fake_run_all)

    assert client.get("/api/v1/perf").status_code == 200
    assert client.get("/api/v1/perf").status_code == 200
    assert calls == 1


def test_perf_page_renders(client):
    r = client.get("/perf")
    assert r.status_code == 200
    assert "/api/v1/perf" in r.text


def test_glance_endpoint_shape(client):
    r = client.get("/api/v1/glance")
    assert r.status_code == 200
    d = r.json()
    assert {"active", "done_today", "failed_today", "spend_today",
            "last_result", "as_of"} == set(d)


def test_glance_endpoint_applies_owner_scope(client, monkeypatch):
    seen = {}
    closed = []

    class _World:
        def list_goals(self, *, owner=None, limit=None):
            seen["owner"] = owner
            seen["limit"] = limit
            return []

        def close(self):
            pass

    monkeypatch.setattr(api_mod, "goal_owner_filter", lambda request: "user:alice")
    world = _World()
    monkeypatch.setattr(world_model, "open_world", lambda: world)
    monkeypatch.setattr(
        world_model, "close_world_if_owned", lambda candidate: closed.append(candidate)
    )

    r = client.get("/api/v1/glance")

    assert r.status_code == 200, r.text
    assert seen == {"owner": "user:alice", "limit": 10_000}
    assert closed == [world]
