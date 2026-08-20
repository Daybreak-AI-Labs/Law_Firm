"""Fresh-install operator preflight: stable, offline, and actionable."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

_PROVIDER_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "XAI_API_KEY",
    "GROK_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "VLLM_BASE_URL",
    "TGI_BASE_URL",
    "OPENAI_COMPATIBLE_BASE_URL",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_AD_TOKEN",
    "AZURE_OPENAI_AUTH",
    "AZURE_OPENAI_TOKEN_SCOPE",
    "AZURE_OPENAI_DEPLOYMENT",
    "MAVERICK_MODEL_OVERRIDE",
)

_AUDIT_KEY_ENV = (
    "MAVERICK_AUDIT_SIGNING_KEY",
    "MAVERICK_AUDIT_SIGNING_KEY_WRAPPED",
    "MAVERICK_AUDIT_REQUIRE_OFFHOST_KEY",
    "MAVERICK_KMS_KEY_ID",
)


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    from maverick import config, runtime_overrides
    from maverick.llm import ROLE_MODELS

    path = tmp_path / "config.toml"
    monkeypatch.setenv("MAVERICK_CONFIG", str(path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MAVERICK_ROLES_FILE", str(tmp_path / "roles.toml"))
    monkeypatch.setattr(
        runtime_overrides,
        "OVERRIDES_PATH",
        tmp_path / "runtime-overrides.toml",
    )
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    monkeypatch.delenv("MAVERICK_WORLD_BACKEND", raising=False)
    monkeypatch.delenv("MAVERICK_EVIDENCE_GATEWAY", raising=False)
    for name in (*_PROVIDER_ENV, *_AUDIT_KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    for role in ROLE_MODELS:
        monkeypatch.delenv(
            f"MAVERICK_MODEL_OVERRIDE_{role.upper()}",
            raising=False,
        )
    config.reset_config_cache()
    yield path
    config.reset_config_cache()


def _by_id(report):
    return {check.id: check for check in report.checks}


def test_missing_config_has_stable_blocker_and_copyable_next_action(isolated_config):
    from maverick.operator_preflight import collect

    report = collect()

    assert report.ready is False
    assert report.schema == "maverick.operator-preflight.v1"
    assert _by_id(report)["config"].status == "blocked"
    assert report.next_action == "maverick init --fast"
    # No volatile timestamp: two reads produce the exact same automation shape.
    assert report.to_dict() == collect().to_dict()
    assert not (isolated_config.parent / "state").exists()


def test_run_profile_ready_with_local_provider(monkeypatch, isolated_config):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-present")
    isolated_config.write_text(
        "\n".join(
            [
                "[providers.ollama]",
                'base_url = "http://127.0.0.1:11434"',
                "[models]",
                'default = "ollama:test-model"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    from maverick import config, providers
    from maverick import operator_preflight as preflight

    config.reset_config_cache()
    monkeypatch.setattr(providers, "missing_sdks", lambda _specs: [])
    monkeypatch.setattr(
        preflight,
        "_dashboard_state",
        lambda *, required: preflight.Check(
            "dashboard", "Operator dashboard", "ready", "installed"
        ),
    )

    report = preflight.collect("run")

    assert report.ready is True
    assert report.blocker_count == 0
    assert _by_id(report)["provider"].status == "ready"
    # Cockpit-only features are honest attention items, not runtime blockers.
    assert _by_id(report)["evidence_graph"].status == "attention"


def test_run_profile_accepts_keyless_global_local_route(
    monkeypatch, isolated_config,
):
    isolated_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("MAVERICK_MODEL_OVERRIDE", "ollama:test-model")
    from maverick import config, providers
    from maverick import operator_preflight as preflight

    config.reset_config_cache()
    monkeypatch.setattr(providers, "missing_sdks", lambda _specs: [])
    report = preflight.collect("run")

    assert _by_id(report)["provider"].status == "ready"
    assert _by_id(report)["model_routes"].status == "ready"


def test_cockpit_profile_requires_all_evidence_dependencies(
    monkeypatch, isolated_config
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-present")
    isolated_config.write_text(
        "\n".join(
            [
                "[providers.ollama]",
                'base_url = "http://127.0.0.1:11434"',
                "[models]",
                'default = "ollama:test-model"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    from maverick import config, providers
    from maverick import operator_preflight as preflight

    config.reset_config_cache()
    monkeypatch.setattr(providers, "missing_sdks", lambda _specs: [])
    monkeypatch.setattr(
        preflight,
        "_dashboard_state",
        lambda *, required: preflight.Check(
            "dashboard", "Operator dashboard", "ready", "installed"
        ),
    )

    report = preflight.collect("cockpit")

    assert report.ready is False
    checks = _by_id(report)
    assert checks["evidence_graph"].status == "blocked"


def test_cockpit_profile_ready_when_dependencies_enabled(
    monkeypatch, isolated_config
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-present")
    monkeypatch.setenv("MAVERICK_TENANT", "test-company")
    isolated_config.write_text(
        "\n".join(
            [
                "[providers.ollama]",
                'base_url = "http://127.0.0.1:11434"',
                "[models]",
                'default = "ollama:test-model"',
                "[evidence_graph]",
                "enable = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    from maverick import config, providers
    from maverick import operator_preflight as preflight

    config.reset_config_cache()
    monkeypatch.setattr(providers, "missing_sdks", lambda _specs: [])
    monkeypatch.setattr(
        preflight,
        "_dashboard_state",
        lambda *, required: preflight.Check(
            "dashboard", "Operator dashboard", "ready", "installed"
        ),
    )

    report = preflight.collect("cockpit")

    assert report.ready is True
    assert all(
        _by_id(report)[name].status == "ready"
        for name in (
            "tenant_scope",
            "evidence_graph",
        )
    )


def test_cockpit_profile_requires_explicit_tenant_scope(
    monkeypatch, isolated_config
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-present")
    isolated_config.write_text(
        "\n".join(
            [
                "[providers.ollama]",
                'base_url = "http://127.0.0.1:11434"',
                "[models]",
                'default = "ollama:test-model"',
                "[evidence_graph]",
                "enable = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    from maverick import config, providers
    from maverick import operator_preflight as preflight

    config.reset_config_cache()
    monkeypatch.setattr(providers, "missing_sdks", lambda _specs: [])
    monkeypatch.setattr(
        preflight,
        "_dashboard_state",
        lambda *, required: preflight.Check(
            "dashboard", "Operator dashboard", "ready", "installed"
        ),
    )

    report = preflight.collect("cockpit")

    assert report.ready is False
    tenant = _by_id(report)["tenant_scope"]
    assert tenant.status == "blocked"
    assert tenant.remediation == "maverick config edit"


def test_selected_run_model_requires_its_own_provider_credential(
    monkeypatch, isolated_config
):
    isolated_config.write_text(
        "\n".join(
            [
                "[providers.ollama]",
                'base_url = "http://127.0.0.1:11434"',
                "[models]",
                'default = "anthropic:remote"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    from maverick import config
    from maverick import operator_preflight as preflight

    config.reset_config_cache()
    report = preflight.collect("run")

    route = _by_id(report)["model_routes"]
    assert route.status == "blocked"
    assert "anthropic: ANTHROPIC_API_KEY" in route.detail
    assert "ollama" not in route.detail


def test_secure_preflight_ignores_retired_role_edit_model(
    monkeypatch, isolated_config,
):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    isolated_config.write_text(
        '[models]\ndefault = "anthropic:remote"\n',
        encoding="utf-8",
    )
    (isolated_config.parent / "roles.toml").write_text(
        '[orchestrator]\nmodel = "ollama:qwen3"\n',
        encoding="utf-8",
    )
    from maverick import config
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-present")
    from maverick.llm import model_for_role, offline_model_for_role
    from maverick.operator_preflight import _routed_configuration_missing

    config.reset_config_cache()
    cfg = config.load_config()
    assert model_for_role("orchestrator") == "anthropic:remote"
    assert offline_model_for_role("orchestrator", config=cfg) == "anthropic:remote"
    assert _routed_configuration_missing(cfg) == {"anthropic": ()}


def test_dashboard_global_pin_matches_live_model_resolution(
    isolated_config,
):
    isolated_config.write_text("", encoding="utf-8")
    (isolated_config.parent / "runtime-overrides.toml").write_text(
        '[models]\ndefault = "vllm:local-orchestrator"\n',
        encoding="utf-8",
    )
    from maverick import config
    from maverick.llm import model_for_role, offline_model_for_role
    from maverick.operator_preflight import _routed_configuration_missing

    config.reset_config_cache()
    cfg = config.load_config()
    assert model_for_role("orchestrator") == "vllm:local-orchestrator"
    assert (
        offline_model_for_role("orchestrator", config=cfg)
        == "vllm:local-orchestrator"
    )
    assert _routed_configuration_missing(cfg) == {"vllm": ()}


def test_allowlist_mismatch_blocks_without_substitution(
    isolated_config,
):
    isolated_config.write_text(
        '[models]\ndefault = "anthropic:remote"\n',
        encoding="utf-8",
    )
    (isolated_config.parent / "runtime-overrides.toml").write_text(
        '[access]\nallowed_models = ["ollama:qwen3"]\n',
        encoding="utf-8",
    )
    from maverick import config
    from maverick.llm import ModelNotAllowedError, offline_model_for_role
    from maverick.operator_preflight import collect

    config.reset_config_cache()
    cfg = config.load_config()
    with pytest.raises(ModelNotAllowedError):
        offline_model_for_role("orchestrator", config=cfg)
    assert _by_id(collect("run"))["model_routes"].status == "blocked"


def test_secure_preflight_ignores_per_role_environment_override(
    monkeypatch, isolated_config,
):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    isolated_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("MAVERICK_MODEL_OVERRIDE", "openai:global")
    monkeypatch.setenv(
        "MAVERICK_MODEL_OVERRIDE_ORCHESTRATOR",
        "ollama:role-specific",
    )
    from maverick import config
    from maverick.llm import model_for_role, offline_model_for_role
    from maverick.operator_preflight import _routed_configuration_missing

    config.reset_config_cache()
    cfg = config.load_config()
    assert model_for_role("orchestrator") == "openai:global"
    assert offline_model_for_role("orchestrator", config=cfg) == "openai:global"
    assert _routed_configuration_missing(cfg) == {"openai": ("api_key",)}


@pytest.mark.parametrize(
    ("auth_env", "auth_value"),
    [
        ("AZURE_OPENAI_AD_TOKEN", "static-entra-token"),
        ("AZURE_OPENAI_AUTH", "entra_id"),
    ],
)
def test_azure_route_accepts_entra_authentication(
    monkeypatch, isolated_config, auth_env, auth_value,
):
    from maverick.operator_preflight import _route_configuration_missing

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt5")
    monkeypatch.setenv(auth_env, auth_value)

    assert _route_configuration_missing("azure", {}) == ()


def test_azure_route_rejects_ambiguous_or_unknown_auth(
    monkeypatch, isolated_config,
):
    from maverick.operator_preflight import _route_configuration_missing

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt5")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key")
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "token")
    assert _route_configuration_missing("azure", {}) == ("authentication",)

    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    monkeypatch.delenv("AZURE_OPENAI_AD_TOKEN")
    monkeypatch.setenv("AZURE_OPENAI_AUTH", "not-a-real-mode")
    assert _route_configuration_missing("azure", {}) == ("authentication",)


@pytest.mark.parametrize(
    ("table", "environment", "expected"),
    [
        (
            {"auth_mode": "entra_id"},
            {"AZURE_OPENAI_AUTH": "api_key"},
            (),
        ),
        (
            {"auth_mode": "api_key", "api_key": "configured-key"},  # pragma: allowlist secret
            {"AZURE_OPENAI_AUTH": "entra_id"},
            (),
        ),
        (
            {"auth_mode": "unknown", "api_key": "configured-key"},  # pragma: allowlist secret
            {},
            ("authentication",),
        ),
        (
            {"auth_mode": "entra_id", "api_key": "configured-key"},
            {},
            ("authentication",),
        ),
        (
            {"auth_mode": "api_key"},
            {"AZURE_OPENAI_AD_TOKEN": "static-entra-token"},
            ("authentication",),
        ),
    ],
)
def test_azure_route_honors_config_auth_mode_like_runtime(
    monkeypatch,
    isolated_config,
    table,
    environment,
    expected,
):
    from maverick.operator_preflight import _route_configuration_missing

    for name in (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_AD_TOKEN",
        "AZURE_OPENAI_AUTH",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt5")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    config = {
        "providers": {
            "azure": {
                "base_url": "https://res.openai.azure.com",
                **table,
            },
        },
    }
    assert _route_configuration_missing("azure", config) == expected




def test_preflight_json_never_echoes_provider_secret(isolated_config):
    secret = "sk-do-not-print-this-value"  # pragma: allowlist secret
    isolated_config.write_text(
        (
            "[providers.openai]\n"
            f'api_key = "{secret}"  # pragma: allowlist secret\n'
        ),
        encoding="utf-8",
    )
    from maverick import config
    from maverick.cli import main

    config.reset_config_cache()
    result = CliRunner().invoke(main, ["preflight", "--json"])

    assert secret not in result.output
