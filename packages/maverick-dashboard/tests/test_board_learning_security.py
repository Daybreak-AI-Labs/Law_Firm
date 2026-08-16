"""Live executive board on /learning: the payload the page fetches
(/api/v1/learning) and the board skeleton (stamp + card containers +
board.js, no slicer — the payload is not windowed). Charts render
client-side; the payload shape is the contract. The /security records
workspace and its board were deleted with the GRC cluster; the board
endpoint must refuse the department rather than serve an orphaned payload."""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client():
    from maverick_dashboard.app import app
    return TestClient(app, headers={"Origin": "http://testserver"})


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    from maverick import consequence, world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    consequence.reset_shared()


def test_security_board_is_gone_with_the_grc_cluster(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert _client().get("/api/v1/dashboards/security").status_code == 404


def test_learning_payload_has_component_counts(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = _client().get("/api/v1/learning")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d["components_on"], int)
    assert isinstance(d["components_total"], int)
    assert d["components_total"] >= d["components_on"] >= 0


def test_learning_page_ships_the_board_skeleton_without_slicer(
    monkeypatch, tmp_path,
):
    _isolate(monkeypatch, tmp_path)
    t = _client().get("/learning").text
    assert "/static/board.js" in t
    assert 'id="bd-stamp"' in t
    for marker in ("bd-systems", "bd-acc"):
        assert marker in t
    # The payload is not windowed — no time-range slicer on this board.
    assert "bd-slicer" not in t
