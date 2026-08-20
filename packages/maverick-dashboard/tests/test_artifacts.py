"""Artifacts on the goal page: a goal's versioned outputs render by kind (table
-> grid, else text), and GET /api/v1/goals/<id>/artifacts lists the latest."""
from __future__ import annotations

from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


def _world(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    return world_model.WorldModel(tmp_path / "world.db")


def test_goal_page_renders_artifacts(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    gid = w.create_goal("Review obligations", "", domain="legal_obligations")
    w.add_artifact(gid, "table", "Deadline tracker", "| Date | Duty |\n| --- | --- |\n| May 1 | Notice |")
    w.add_artifact(gid, "markdown", "Counsel memo", "# Memo\n\nNotice review needed.")
    t = client.get(f"/chat/goal/{gid}").text
    assert "<h2>Artifacts</h2>" in t
    assert "Deadline tracker" in t and "Counsel memo" in t
    assert '<table class="deliverable__table">' in t   # table artifact -> grid
    assert "<td>Notice</td>" in t
    assert "Notice review needed." in t                  # markdown artifact -> text body


def test_artifact_versions_shown(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    gid = w.create_goal("g", "", domain="legal_obligations")
    w.add_artifact(gid, "text", "Note", "ZZZ_BODY_ONE")
    w.add_artifact(gid, "text", "Note", "ZZZ_BODY_TWO")
    t = client.get(f"/chat/goal/{gid}").text
    assert "ZZZ_BODY_TWO" in t          # latest version rendered
    assert "ZZZ_BODY_ONE" not in t      # older version not rendered (latest only)


def test_artifacts_api_lists_latest(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    gid = w.create_goal("g", "")
    w.add_artifact(gid, "table", "T", "| a |\n| - |\n| 1 |")
    r = client.get(f"/api/v1/goals/{gid}/artifacts")
    assert r.status_code == 200
    arts = r.json()["artifacts"]
    assert len(arts) == 1 and arts[0]["title"] == "T" and arts[0]["kind"] == "table"


def test_goal_without_artifacts_has_no_panel(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    gid = w.create_goal("plain", "")
    w.set_goal_status(gid, "done", result="just text")
    t = client.get(f"/chat/goal/{gid}").text
    assert "<h2>Artifacts</h2>" not in t


def test_artifact_history_endpoint_diffs(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    gid = w.create_goal("g", "")
    w.add_artifact(gid, "text", "Note", "alpha\nbeta")
    w.add_artifact(gid, "text", "Note", "alpha\ngamma")
    r = client.get(f"/api/v1/goals/{gid}/artifacts/history", params={"title": "Note"})
    assert r.status_code == 200
    vs = r.json()["versions"]
    assert [v["version"] for v in vs] == [1, 2]
    assert vs[0]["diff"] == ""                      # first version, nothing to diff against
    assert "-beta" in vs[1]["diff"] and "+gamma" in vs[1]["diff"]


def test_goal_page_autoescapes_hostile_artifact_content(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    gid = w.create_goal("g", "", domain="legal_obligations")
    hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    w.add_artifact(gid, "text", hostile, hostile)
    t = client.get(f"/chat/goal/{gid}").text
    assert hostile not in t
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in t
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in t
