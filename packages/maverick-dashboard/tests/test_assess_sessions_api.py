"""Assessment-session review API: the worklist, the full record the pop-out
renders, and sending follow-up questions back to the respondent.

Mutating /api/v1 requests carry a same-origin Origin (the CSRF contract).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


@pytest.fixture(autouse=True)
def _fresh_world(tmp_path, monkeypatch):
    from maverick import config, world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    config.reset_config_cache()
    import maverick_dashboard.api as api
    api._world_cache.clear()
    yield
    config.reset_config_cache()
    api._world_cache.clear()


def _saved_session():
    from maverick.assessment import AssessmentSession, save_session
    s = AssessmentSession(type="pia", subject="Acme CRM")
    s.record("pia_necessity", "no", "Marketing collects more than needed.")
    save_session(s)
    return s


def test_sessions_worklist():
    s = _saved_session()
    resp = client.get("/api/v1/assess/sessions")
    assert resp.status_code == 200
    rows = resp.json()["sessions"]
    assert rows[0]["id"] == s.id
    assert rows[0]["status"] == "pending_review"
    assert "open_followups" in rows[0]


def test_get_full_record_for_the_popout():
    s = _saved_session()
    resp = client.get(f"/api/v1/assess/sessions/{s.id}")
    assert resp.status_code == 200
    rec = resp.json()
    assert rec["subject"] == "Acme CRM"
    assert rec["answers"]["pia_necessity"]["answer"] == "no"
    assert rec["result"]["findings"], "the risk answer must yield a finding"


def test_get_unknown_record_404():
    assert client.get("/api/v1/assess/sessions/nope").status_code == 404
    # Traversal-shaped ids are refused, not resolved.
    assert client.get("/api/v1/assess/sessions/..%2F..%2Fetc").status_code == 404


def test_post_followups_flips_status_and_validates():
    s = _saved_session()
    current = client.get(f"/api/v1/assess/sessions/{s.id}").json()
    resp = client.post(f"/api/v1/assess/sessions/{s.id}/followups",
                       json={"questions": ["Which sub-processors?",
                                           "DPA countersigned?"],
                             "expected_revision": current["revision"]})
    assert resp.status_code == 200, resp.text
    rec = resp.json()
    assert rec["status"] == "needs_more"
    assert len(rec["followups"]) == 2
    # Validation: empty list 422; unknown id 404.
    assert client.post(f"/api/v1/assess/sessions/{s.id}/followups",
                       json={"questions": [],
                             "expected_revision": rec["revision"]}).status_code == 422
    assert client.post("/api/v1/assess/sessions/nope/followups",
                       json={"questions": ["Q?"],
                             "expected_revision": 0}).status_code == 404
