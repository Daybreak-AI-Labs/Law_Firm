"""Goal attachments panel API: upload → list → download round-trip."""
from __future__ import annotations

import io

from fastapi.testclient import TestClient


def _client():
    from maverick_dashboard.app import app
    return TestClient(app, headers={"Origin": "http://testserver"})


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    from maverick import attachments, world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setattr(attachments, "DEFAULT_ROOT", tmp_path / "attach")


def test_upload_list_download_roundtrip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    c = _client()
    from maverick_dashboard import app as app_mod
    gid = app_mod._world().create_goal("with files", "")

    r = c.post(
        f"/api/v1/goals/{gid}/attachments",
        files={"file": ("notes.txt", io.BytesIO(b"remember the milk"), "text/plain")},
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]

    listed = c.get(f"/api/v1/goals/{gid}/attachments").json()
    assert [a["filename"] for a in listed] == ["notes.txt"]

    dl = c.get(f"/api/v1/goals/{gid}/attachments/{aid}/download")
    assert dl.status_code == 200
    assert dl.content == b"remember the milk"
    assert "attachment" in dl.headers["content-disposition"]
    assert "notes.txt" in dl.headers["content-disposition"]
    assert dl.headers["x-content-type-options"] == "nosniff"


def test_download_streams_file_without_reading_all_bytes(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    c = _client()
    from maverick_dashboard import app as app_mod
    gid = app_mod._world().create_goal("streamed file", "")

    r = c.post(
        f"/api/v1/goals/{gid}/attachments",
        files={"file": ("large.txt", io.BytesIO(b"x" * 1024), "text/plain")},
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]

    def fail_read_bytes(self):
        raise AssertionError("download endpoint must stream instead of read_bytes")

    monkeypatch.setattr("pathlib.Path.read_bytes", fail_read_bytes)

    dl = c.get(f"/api/v1/goals/{gid}/attachments/{aid}/download")
    assert dl.status_code == 200
    assert dl.content == b"x" * 1024
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.headers["x-content-type-options"] == "nosniff"


def test_download_404_for_wrong_attachment(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    c = _client()
    from maverick_dashboard import app as app_mod
    gid = app_mod._world().create_goal("empty", "")
    r = c.get(f"/api/v1/goals/{gid}/attachments/999/download")
    assert r.status_code == 404
