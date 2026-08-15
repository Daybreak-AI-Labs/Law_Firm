"""Webhook signing secret is admin-editable from the Settings page: saved to
the dashboard overlay, picked up by maverick.webhooks.inbound_secret, never
echoed back, env var still wins."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard.app import app

client = TestClient(app, headers={"Origin": "http://testserver"})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / "mvhome"))
    monkeypatch.delenv("MAVERICK_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)


def test_settings_page_shows_webhook_state_without_the_value():
    page = client.get("/settings").text
    assert "Automation webhooks" in page
    assert "not set" in page


def test_save_secret_roundtrip_and_clear():
    r = client.post("/settings/webhooks",
                    data={"secret": "s3cret-value-123"},  # pragma: allowlist secret
                    follow_redirects=False)
    assert r.status_code == 303

    from maverick.webhooks import inbound_secret
    assert inbound_secret() == "s3cret-value-123"  # pragma: allowlist secret

    page = client.get("/settings").text
    assert "configured" in page
    assert "s3cret-value-123" not in page  # pragma: allowlist secret -- never echoed back

    r = client.post("/settings/webhooks", data={"secret": "  "},
                    follow_redirects=False)
    assert r.status_code == 303
    assert inbound_secret() is None or inbound_secret() == ""


def test_env_secret_still_wins(monkeypatch):
    client.post("/settings/webhooks",
                data={"secret": "overlay-secret"},  # pragma: allowlist secret
                follow_redirects=False)
    monkeypatch.setenv("MAVERICK_WEBHOOK_SECRET", "env-secret")  # pragma: allowlist secret
    from maverick.webhooks import inbound_secret
    assert inbound_secret() == "env-secret"  # pragma: allowlist secret


def test_cross_site_save_is_blocked():
    bare = TestClient(app)
    assert bare.post("/settings/webhooks",
                     data={"secret": "x"}).status_code == 403


def test_tenant_admin_cannot_set_global_webhook_secret(tmp_path, monkeypatch):
    # The webhook signing secret gates every auth-exempt inbound webhook
    # endpoint deployment-wide -- a principal who is only a TENANT-local admin
    # (global viewer) must be denied, not just a non-admin.
    from maverick.paths import reset_tenant, set_tenant
    from maverick_dashboard import auth, rbac

    rbac.set_role("user:alice", "viewer")
    rbac.set_tenant_role("acme", "user:alice", "admin")
    monkeypatch.setattr(auth, "caller_principal", lambda request: "user:alice")

    tok = set_tenant("acme")
    try:
        assert auth.role_for_principal("user:alice") == "admin"
        r = client.post("/settings/webhooks", data={"secret": "s"},
                        follow_redirects=False)
    finally:
        reset_tenant(tok)

    assert r.status_code == 403
    from maverick.webhooks import inbound_secret
    assert not inbound_secret()
