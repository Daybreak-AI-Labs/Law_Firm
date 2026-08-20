"""Firm-only branding has no residual platform-vendor asset or route."""
from __future__ import annotations

from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


def _world(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setattr(
        "maverick_dashboard.public_origin.canonical_url",
        lambda path: "https://firm.example/" + str(path or "").lstrip("/"),
    )
    monkeypatch.setattr("maverick.audit.audit_event", lambda *a, **k: True)
    return world_model.WorldModel(tmp_path / "world.db")


def test_shell_uses_only_firm_wordmark(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch)
    t = client.get("/goals").text
    assert "daybreak-logo" not in t.lower()
    assert "Daybreak Labs" not in t
    assert "Bjerken and Day" in t
    assert client.get("/static/daybreak-logo.jpg").status_code == 404


def test_share_page_shows_the_firm_wordmark(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    reviewer = "user:reviewer"
    project_id = w.create_client_matter(
        "Client matter",
        principal=reviewer,
        domain="legal_obligations",
        matter_number="BRAND-001",
        jurisdiction="Tennessee",
        client_name="Branding Client",
    )
    gid = w.create_matter_goal(
        "Forecast",
        "",
        principal=reviewer,
        domain="legal_obligations",
        project_id=project_id,
    )
    assert gid is not None
    w.set_goal_status(gid, "done", result="ok")
    w.record_signoff(gid, "approved", decided_by=reviewer)
    token = client.post(f"/api/v1/goals/{gid}/share").json()["url"].split("/share/")[1]
    page = TestClient(app).get(f"/share/{token}").text   # anon viewer
    # This page is what a CLIENT sees, so it carries the firm's wordmark --
    # not the platform vendor's logo.
    assert "Bjerken and Day" in page
    assert "daybreak-logo.jpg" not in page
