"""Doc discovery + assessment memory over the dashboard API: search connected
sources for assessment paperwork, one-click attach it as goal evidence, and
recall what the org already knows about similar subjects.

Mutating /api/v1 requests carry a same-origin Origin (the CSRF contract).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})

MINI_PDF = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n"


@pytest.fixture(autouse=True)
def _fresh_world(tmp_path, monkeypatch):
    from maverick import config, world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    for var in ("MSGRAPH_ACCESS_TOKEN", "SLACK_BOT_TOKEN",
                "SLACK_SEARCH_TOKEN", "GDRIVE_ACCESS_TOKEN",
                "MAVERICK_ASSESS_DISCOVERY", "MAVERICK_ASSESS_LEARN"):
        monkeypatch.delenv(var, raising=False)
    config.reset_config_cache()
    import maverick_dashboard.api as api
    api._world_cache.clear()
    yield
    config.reset_config_cache()
    api._world_cache.clear()


def _goal() -> int:
    from maverick import world_model
    w = world_model.WorldModel(world_model.DEFAULT_DB)
    return w.create_goal("Privacy assessment: Acme CRM", "seeded",
                         domain="itgrc_dpia")


# ---- POST /api/v1/docs/discover ----------------------------------------------

def test_discover_with_nothing_configured_is_honest():
    resp = client.post("/api/v1/docs/discover", json={"subject": "Acme CRM"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured_sources"] == []
    assert body["hits"] == []


def test_discover_disabled_by_config(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[assessments]\ndoc_discovery = false\n", encoding="utf-8")
    monkeypatch.setenv("MAVERICK_CONFIG", str(cfg))
    from maverick import config
    config.reset_config_cache()
    resp = client.post("/api/v1/docs/discover", json={"subject": "Acme CRM"})
    assert resp.status_code == 403


def test_discover_returns_ranked_hits(monkeypatch):
    from maverick import doc_discovery
    monkeypatch.setenv("MSGRAPH_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(doc_discovery, "_post", lambda url, token, body: (200, {
        "value": [{"hitsContainers": [{"hits": [
            {"summary": "", "resource": {
                "id": "d1", "name": "Acme CRM DPA.pdf", "webUrl": "https://x",
                "size": 5, "file": {"mimeType": "application/pdf"},
                "parentReference": {"driveId": "drv"}}},
        ]}]}]}))
    resp = client.post("/api/v1/docs/discover", json={"subject": "Acme CRM"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured_sources"] == ["msgraph"]
    assert body["hits"][0]["name"] == "Acme CRM DPA.pdf"
    assert body["hits"][0]["ref"] == {"drive_id": "drv"}


def test_discover_validates_input():
    assert client.post("/api/v1/docs/discover", json={}).status_code == 422
    assert client.post("/api/v1/docs/discover",
                       json={"subject": "x", "limit": 0}).status_code == 422


# ---- POST /api/v1/goals/{id}/attachments/from-source ---------------------------

def test_attach_from_source_stores_real_attachment(monkeypatch):
    from maverick import doc_discovery
    gid = _goal()
    monkeypatch.setattr(doc_discovery, "fetch",
                        lambda source, doc_id, ref=None, **kw:
                        (MINI_PDF, "application/pdf"))
    resp = client.post(f"/api/v1/goals/{gid}/attachments/from-source", json={
        "source": "msgraph", "doc_id": "d1", "name": "Acme CRM DPA.pdf",
        "ref": {"drive_id": "drv"},
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["filename"] == "Acme CRM DPA.pdf"
    listed = client.get(f"/api/v1/goals/{gid}/attachments").json()
    assert [a["filename"] for a in listed] == ["Acme CRM DPA.pdf"]


def test_attach_from_source_unconfigured_source_is_400():
    gid = _goal()
    resp = client.post(f"/api/v1/goals/{gid}/attachments/from-source",
                       json={"source": "msgraph", "doc_id": "d1"})
    assert resp.status_code == 400


def test_attach_from_source_denies_executables(monkeypatch):
    from maverick import doc_discovery
    gid = _goal()
    monkeypatch.setattr(doc_discovery, "fetch",
                        lambda source, doc_id, ref=None, **kw:
                        (b"\x7fELF\x02\x01\x01" + b"\x00" * 16,
                         "application/pdf"))
    resp = client.post(f"/api/v1/goals/{gid}/attachments/from-source",
                       json={"source": "msgraph", "doc_id": "d1",
                             "name": "evil.pdf"})
    assert resp.status_code == 400


def test_attach_from_source_unknown_goal_404():
    resp = client.post("/api/v1/goals/99999/attachments/from-source",
                       json={"source": "msgraph", "doc_id": "d1"})
    assert resp.status_code == 404


# ---- GET /api/v1/assessment-memory/similar -------------------------------------

def test_memory_similar_returns_precedents_and_suggestions():
    from maverick.assessment import AssessmentSession, save_session
    s = AssessmentSession(type="pia", subject="Acme CRM")
    s.record("pia_necessity", "yes")
    save_session(s)
    resp = client.get("/api/v1/assessment-memory/similar",
                      params={"subject": "Acme CRM rollout", "type": "pia"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["similar"][0]["subject"] == "Acme CRM"
    assert body["suggestions"]["pia_necessity"]["answer"] == "yes"


def test_memory_similar_requires_subject():
    assert client.get("/api/v1/assessment-memory/similar",
                      params={"subject": "  "}).status_code == 422
