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


def test_download_uses_bounded_decrypting_reader(monkeypatch, tmp_path):
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

    from maverick import attachments

    calls = []
    real_read = attachments.read_bytes

    def observed_read(path, digest):
        calls.append((path, digest))
        return real_read(path, digest)

    monkeypatch.setattr(attachments, "read_bytes", observed_read)

    dl = c.get(f"/api/v1/goals/{gid}/attachments/{aid}/download")
    assert dl.status_code == 200
    assert dl.content == b"x" * 1024
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.headers["x-content-type-options"] == "nosniff"
    assert calls and calls[0][1] == r.json()["sha256"]


def test_download_fails_closed_on_attachment_integrity_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    c = _client()
    from maverick import attachments
    from maverick_dashboard import app as app_mod

    gid = app_mod._world().create_goal("tampered file", "")
    uploaded = c.post(
        f"/api/v1/goals/{gid}/attachments",
        files={"file": ("evidence.txt", io.BytesIO(b"original"), "text/plain")},
    )
    aid = uploaded.json()["id"]

    def reject(_path, _digest):
        raise attachments.AttachmentRejected("content-address mismatch")

    monkeypatch.setattr(attachments, "read_bytes", reject)

    response = c.get(f"/api/v1/goals/{gid}/attachments/{aid}/download")

    assert response.status_code == 410
    assert "integrity" in response.json()["detail"]


def test_download_does_not_fetch_missing_local_ciphertext_remotely(
    monkeypatch, tmp_path,
):
    _isolate(monkeypatch, tmp_path)
    c = _client()
    from pathlib import Path

    from maverick_dashboard import app as app_mod

    world = app_mod._world()
    gid = world.create_goal("local-only evidence", "")
    uploaded = c.post(
        f"/api/v1/goals/{gid}/attachments",
        files={"file": ("evidence.txt", io.BytesIO(b"original"), "text/plain")},
    )
    aid = uploaded.json()["id"]
    Path(world.list_attachments(gid)[0].path).unlink()

    response = c.get(f"/api/v1/goals/{gid}/attachments/{aid}/download")

    assert response.status_code == 410
    assert response.json()["detail"] == "attachment bytes not on this host"


def test_download_404_for_wrong_attachment(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    c = _client()
    from maverick_dashboard import app as app_mod
    gid = app_mod._world().create_goal("empty", "")
    r = c.get(f"/api/v1/goals/{gid}/attachments/999/download")
    assert r.status_code == 404
