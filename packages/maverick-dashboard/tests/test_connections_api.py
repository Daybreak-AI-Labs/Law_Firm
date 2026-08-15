"""Dashboard connections API: CRUD over sealed named connections (gated)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def _client():
    from maverick_dashboard.app import app
    return TestClient(app, headers={"Origin": "http://testserver"})


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONNECTIONS", "1")
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    monkeypatch.setattr("maverick.paths.data_dir", lambda *p, **k: tmp_path.joinpath(*p))
    monkeypatch.setattr("maverick.connections.seal_text_for_tenant", lambda t, s: s.encode())
    monkeypatch.setattr("maverick.connections.unseal_text_for_tenant", lambda t, b: b.decode())


def test_gated_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_CONNECTIONS", raising=False)
    monkeypatch.setattr("maverick.connections.enabled", lambda: False)
    assert _client().get("/api/v1/connections").status_code == 403


def test_create_list_delete_and_token_never_returned(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    c = _client()
    r = c.post("/api/v1/connections", json={
        "name": "Zendesk Prod", "connector": "zendesk",
        "base_url": "https://x.zendesk.com", "token": "sekret"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "zendesk-prod" and body["has_token"] is True
    assert "token" not in body
    listed = c.get("/api/v1/connections").json()["connections"]
    assert listed[0]["name"] == "zendesk-prod" and "token" not in listed[0]
    assert c.delete("/api/v1/connections/zendesk-prod").status_code == 200
    assert c.get("/api/v1/connections").json()["connections"] == []


def test_bad_name_is_400(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = _client().post("/api/v1/connections", json={"name": "!!!", "token": "x"})
    assert r.status_code == 400


def test_test_endpoint_reports_unreachable_without_crashing(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    c = _client()
    c.post("/api/v1/connections", json={"name": "n", "base_url": "https://127.0.0.1:0", "token": "t"})
    r = c.post("/api/v1/connections/n/test")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert c.post("/api/v1/connections/nope/test").status_code == 404


def test_test_endpoint_blocks_ssrf_to_internal_hosts(monkeypatch, tmp_path):
    # The probe carries the sealed token, so it MUST go through the SSRF-safe
    # path: a base_url pointing at cloud metadata / a private host is refused
    # (BlockedHost) BEFORE any request leaves -- no internal-network oracle, no
    # token forwarded via a redirect.
    _isolate(monkeypatch, tmp_path)
    c = _client()
    for host in ("http://169.254.169.254/latest/meta-data/",
                 "http://127.0.0.1/", "http://10.0.0.5/"):
        c.post("/api/v1/connections", json={"name": "ssrf", "base_url": host, "token": "t"})
        r = c.post("/api/v1/connections/ssrf/test")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["ok"] is False and "not permitted" in j["detail"], (host, j)


def test_connections_page_renders(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    r = _client().get("/connections")
    assert r.status_code == 200 and 'id="cx-form"' in r.text


def test_connection_use_policy_roundtrips_without_token(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    c = _client()
    r = c.post("/api/v1/connections", json={
        "name": "shared", "connector": "acme", "token": "never-return-me",
        "access": "tenant", "allowed_principals": ["user:alice"],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["access"] == "tenant"
    assert body["allowed_principals"] == ["user:alice"]
    assert "token" not in body and "never-return-me" not in r.text


@pytest.mark.parametrize(
    ("status", "authenticated"),
    [(200, True), (204, True), (302, False), (401, False), (403, False), (404, False)],
)
def test_probe_separates_reachability_from_authentication(
    monkeypatch, tmp_path, status, authenticated,
):
    _isolate(monkeypatch, tmp_path)

    calls = []

    def _probe(*args, **kwargs):
        calls.append(kwargs.get("headers") or {})
        if 200 <= status < 300 and len(calls) == 2:
            return SimpleNamespace(status_code=401)
        return SimpleNamespace(status_code=status)

    monkeypatch.setattr(
        "maverick.tools._ssrf.safe_get",
        _probe,
    )
    c = _client()
    created = c.post("/api/v1/connections", json={
        "name": "probe", "connector": "acme",
        "base_url": "https://api.example.test", "token": "secret",
    })
    assert created.status_code == 201, created.text
    tested = c.post("/api/v1/connections/probe/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["reachable"] is True
    assert tested.json()["authenticated"] is authenticated
    assert tested.json()["ok"] is authenticated

    listed = c.get("/api/v1/connections").json()["connections"][0]
    assert listed["last_test"]["status"] == status
    assert listed["last_test"]["reachable"] is True
    assert listed["last_test"]["authenticated"] is authenticated
    assert "token" not in listed and "secret" not in str(listed)


def test_public_2xx_endpoint_is_not_mistaken_for_authenticated_readiness(
    monkeypatch, tmp_path,
):
    _isolate(monkeypatch, tmp_path)
    calls = []

    def _public(*args, **kwargs):
        calls.append(kwargs.get("headers") or {})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("maverick.tools._ssrf.safe_get", _public)
    c = _client()
    created = c.post("/api/v1/connections", json={
        "name": "public-root",
        "connector": "acme",
        "base_url": "https://public.example.test",
        "token": "secret",
    })
    assert created.status_code == 201, created.text

    tested = c.post("/api/v1/connections/public-root/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["reachable"] is True
    assert tested.json()["authenticated"] is False
    assert "did not prove" in tested.json()["detail"]
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_probe_cannot_mark_concurrently_rotated_credentials_ready(
    monkeypatch, tmp_path,
):
    _isolate(monkeypatch, tmp_path)
    from maverick import connections

    c = _client()
    created = c.post("/api/v1/connections", json={
        "name": "race",
        "connector": "acme",
        "base_url": "https://api.example.test",
        "token": "old-secret",
    })
    assert created.status_code == 201, created.text

    rotated = False

    def rotate_during_probe(*args, **kwargs):
        nonlocal rotated
        if not rotated:
            current = connections.get_connection("race")
            assert current is not None
            connections.set_connection(
                "race",
                connector="acme",
                base_url="https://replacement.example.test",
                token="new-secret",
                owner=str(current.get("owner") or ""),
                expected_owner=str(current.get("owner") or ""),
            )
            rotated = True
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("maverick.tools._ssrf.safe_get", rotate_during_probe)
    tested = c.post("/api/v1/connections/race/test")
    assert tested.status_code == 409, tested.text

    replacement = connections.get_connection("race")
    assert replacement is not None
    assert replacement["token"] == "new-secret"
    assert "last_test" not in replacement
