"""The public POST /form/{token} endpoint that records hosted-form submissions."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client():
    from maverick_dashboard.app import app
    return TestClient(app, headers={"Origin": "http://testserver"})


def _isolate(monkeypatch, tmp_path, triggers="1"):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_EVENT_TRIGGERS", triggers)
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")


def test_json_submission_is_recorded_and_pollable(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = _client().post("/form/signup", json={"email": "a@b.com", "plan": "pro"})
    assert r.status_code == 201 and r.json()["seq"] == 1
    # a form event source polling the same token sees it as a new event
    from maverick import automation_events as ev
    base = ev.get_source("form").poll({"token": "signup"}, "")   # baseline
    assert base.cursor == "1"


def test_form_submission_fires_after_baseline(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    c = _client()
    c.post("/form/lead", json={"name": "first"})
    from maverick import automation_events as ev
    base = ev.get_source("form").poll({"token": "lead"}, "")
    c.post("/form/lead", json={"name": "second"})
    out = ev.get_source("form").poll({"token": "lead"}, base.cursor)
    assert [e["name"] for e in out.events] == ["second"]


def test_urlencoded_submission(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = _client().post("/form/contact", data={"message": "hello"})
    assert r.status_code == 201
    from maverick import form_store
    events, _ = form_store.since("contact", "0")
    assert events and events[0]["message"] == "hello"


def test_form_disabled_when_triggers_off(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, triggers="0")
    r = _client().post("/form/x", json={"a": 1})
    assert r.status_code == 404


def test_invalid_json_is_400(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = _client().post("/form/x", content=b"{not json",
                       headers={"content-type": "application/json"})
    assert r.status_code == 400
