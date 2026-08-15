"""Govern → Assessments: the register page + the draft / review / export API."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client():
    from maverick_dashboard.app import app
    return TestClient(app, headers={"Origin": "http://testserver"})


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)


def _an_agent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maverick.domain_edit import list_agents
    agents = list_agents()
    return agents[0]["name"] if agents else None


def test_page_renders(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = _client().get("/assessments")
    assert r.status_code == 200
    import re
    assert re.search(r'<h1 class="page-title[^"]*"[^>]*>Assessments', r.text)
    assert "Assess a subject" in r.text


def test_empty_register(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    j = _client().get("/api/v1/assessments").json()
    assert j["assessments"] == []
    assert "baseline" in j["templates"]


def test_draft_review_and_export(monkeypatch, tmp_path):
    name = _an_agent(monkeypatch, tmp_path)
    if not name:
        import pytest
        pytest.skip("no packs discovered")
    c = _client()
    # Draft
    r = c.post(f"/api/v1/assessments/agent/{name}/refresh", json={})
    assert r.status_code == 200
    a = r.json()
    assert a["kind"] == "agent" and a["subject"] == name
    assert set(a["lenses"]) == {"privacy", "security", "ai_risk"}
    # It shows up in the register
    lst = c.get("/api/v1/assessments").json()["assessments"]
    assert any(x["subject"] == name for x in lst)
    # Review each lens -> reviewed
    for lens in ("privacy", "security", "ai_risk"):
        rr = c.post(f"/api/v1/assessments/agent/{name}/review",
                    json={"lens": lens, "decision": "accepted", "cadence_days": 90})
        assert rr.status_code == 200
    assert c.get(f"/api/v1/assessments/agent/{name}").json()["status"] == "reviewed"
    # CSV export contains the subject
    csv = c.get("/api/v1/assessments/export?format=csv")
    assert csv.status_code == 200 and "text/csv" in csv.headers["content-type"]
    assert name in csv.text


def test_review_rejects_bad_lens(monkeypatch, tmp_path):
    name = _an_agent(monkeypatch, tmp_path)
    if not name:
        import pytest
        pytest.skip("no packs discovered")
    c = _client()
    c.post(f"/api/v1/assessments/agent/{name}/refresh", json={})
    r = c.post(f"/api/v1/assessments/agent/{name}/review",
               json={"lens": "nonsense", "decision": "accepted"})
    assert r.status_code == 422  # schema pattern rejects it


def test_sweep_endpoint(monkeypatch, tmp_path):
    name = _an_agent(monkeypatch, tmp_path)
    if not name:
        import pytest
        pytest.skip("no packs discovered")
    c = _client()
    c.post(f"/api/v1/assessments/agent/{name}/refresh", json={})
    r = c.post("/api/v1/assessments/sweep", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["refreshed"] >= 1 and "due" in body


def test_refresh_unknown_agent_404(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = _client().post("/api/v1/assessments/agent/____nope____/refresh", json={})
    assert r.status_code == 404


def test_bad_kind_400(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = _client().post("/api/v1/assessments/widget/x/refresh", json={})
    assert r.status_code == 400
