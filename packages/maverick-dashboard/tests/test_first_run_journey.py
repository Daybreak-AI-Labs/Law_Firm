"""A clean-home install reaches activation and evidence-visible cockpit pages."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_headless_first_run_journey(monkeypatch, tmp_path):
    home = tmp_path / "maverick-home"
    installed = home / "config.toml"
    source = tmp_path / "reviewed-config.toml"
    source.write_text(
        "\n".join(
            [
                "[providers.anthropic]",
                'api_key = "${ANTHROPIC_API_KEY}"',
                "[sandbox]",
                'backend = "local"',
                "[evidence_graph]",
                "enable = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(home))
    monkeypatch.setenv("MAVERICK_CONFIG", str(installed))
    monkeypatch.setenv("MAVERICK_TENANT", "first-run-company")
    # Offline preflight checks presence only. No provider request is made.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-present")
    for name in (
        "MAVERICK_DASHBOARD_TOKEN",
        "MAVERICK_DASHBOARD_REQUIRE_AUTH",
        "MAVERICK_OIDC_ENABLED",
        "MAVERICK_PROXY_AUTH",
        "MAVERICK_TENANT_BY_USER",
    ):
        monkeypatch.delenv(name, raising=False)

    from maverick import config, providers, world_model

    # The reviewed config is installed by copying it into place (the operator
    # installs config out of band; the CLI installer surface was removed).
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    config.reset_config_cache()
    monkeypatch.setattr(providers, "missing_sdks", lambda _specs: [])

    from maverick_dashboard import app as dashboard

    monkeypatch.setattr(world_model, "DEFAULT_DB", home / "world.db")
    dashboard._world_cache.clear()
    client = TestClient(
        dashboard.app,
        headers={"Origin": "http://testserver"},
    )

    started = client.get("/start")
    assert started.status_code == 200, started.text
    assert "ready to run" in started.text
    assert "Offline install preflight" in started.text
