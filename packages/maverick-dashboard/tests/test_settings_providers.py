"""Minimal provider-key settings use the encrypted dashboard overlay."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient


def _no_provider_env(monkeypatch):
    from maverick import config
    for v in config.PROVIDER_KEY_ENV_VARS + config.PROVIDER_BASE_URL_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    for v in ("GOOGLE_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(v, raising=False)


def test_provider_key_overlay_unblocks(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    _no_provider_env(monkeypatch)
    from maverick import config
    from maverick_dashboard import settings_store
    assert config.any_provider_configured() is False
    settings_store.set_provider("anthropic", api_key="sk-test-123456")  # pragma: allowlist secret
    assert config.dashboard_overrides_path().exists()
    raw = config.dashboard_overrides_path().read_text(encoding="utf-8")
    assert "sk-test-123456" not in raw  # pragma: allowlist secret
    assert "MVKAR1:" in raw
    assert not config.config_path().exists()            # config.toml untouched
    assert config.any_provider_configured() is True
    assert config.get_provider_config("anthropic")["api_key"] == "sk-test-123456"  # pragma: allowlist secret
    settings_store.clear_provider("anthropic")
    assert config.any_provider_configured() is False


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    from maverick_dashboard import app as dash_app
    dash_app._world_cache.clear()
    return TestClient(dash_app.app, headers={"Origin": "http://testserver"})


def test_settings_page_has_provider_entry_without_capability_editor(monkeypatch, tmp_path):
    _no_provider_env(monkeypatch)
    c = _client(monkeypatch, tmp_path)
    r = c.get("/settings")
    assert r.status_code == 200
    assert 'action="/settings/providers"' in r.text
    assert 'action="/settings/capabilities"' not in r.text
    assert 'name="web_search"' not in r.text


def test_provider_post_redacts_and_persists(monkeypatch, tmp_path):
    _no_provider_env(monkeypatch)
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    c = _client(monkeypatch, tmp_path)
    from maverick import config
    assert c.post("/settings/providers",
                  data={"provider": "openai", "api_key": "sk-secret-abcd9999"}).status_code == 200  # pragma: allowlist secret
    assert config.get_provider_config("openai")["api_key"] == "sk-secret-abcd9999"  # pragma: allowlist secret
    body = c.get("/settings").text
    assert "sk-secret-abcd9999" not in body  # pragma: allowlist secret  (raw key never echoed)
    assert "9999" in body                                # only the masked hint


def test_provider_secret_refuses_plaintext_store(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "0")
    from maverick import config
    from maverick_dashboard import settings_store

    with pytest.raises(
        settings_store.SecuritySuiteConfigUnavailable,
        match="at-rest encryption is disabled",
    ):
        settings_store.set_provider(
            "anthropic", api_key="sk-must-not-land",  # pragma: allowlist secret
        )
    path = config.dashboard_overrides_path()
    assert not path.exists() or "sk-must-not-land" not in path.read_text(
        encoding="utf-8"
    )


def test_retired_capability_editor_is_not_mounted(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from maverick import config
    assert c.post("/settings/capabilities", data={"browser": "on"}).status_code == 404
    assert config.get_capabilities()["browser"] is False


def test_concurrent_provider_updates_both_apply(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    _no_provider_env(monkeypatch)
    from maverick_dashboard import settings_store

    barrier = threading.Barrier(2)

    def do_anthropic():
        barrier.wait()
        settings_store.set_provider("anthropic", api_key="sk-test-abc123")  # pragma: allowlist secret

    def do_openai():
        barrier.wait()
        settings_store.set_provider("openai", api_key="sk-test-def456")  # pragma: allowlist secret

    ts = [threading.Thread(target=do_anthropic), threading.Thread(target=do_openai)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    overlay = settings_store.load_overlay()
    assert overlay.get("providers", {}).get("anthropic", {}).get("api_key")
    assert overlay.get("providers", {}).get("openai", {}).get("api_key")
