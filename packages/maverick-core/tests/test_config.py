"""Config loader tests."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from maverick.config import _interp, config_path, get_safety, load_config


def test_default_config_path_honors_maverick_home(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    monkeypatch.setenv("MAVERICK_HOME", str(home))
    monkeypatch.delenv("MAVERICK_CONFIG", raising=False)

    assert config_path() == home / "config.toml"


def test_missing_config_returns_empty_dict():
    cfg = load_config(Path("/this/path/does/not/exist.toml"))
    assert cfg == {}


def test_corrupt_config_fails_soft_to_empty_dict():
    """A corrupt/unparseable config.toml must not crash the agent loop; it
    fails soft to {} like a missing file. Regression: load_config raised
    TOMLDecodeError, which propagated through every get_role_model/get_safety
    caller. The common real-world trigger is a Windows backslash path that
    TOML reads as an invalid \\U escape."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write('workdir = "C:\\Users\\x\\ws"\n[unterminated\n')  # invalid TOML
        path = Path(f.name)
    try:
        assert load_config(path) == {}
    finally:
        path.unlink()


def test_corrupt_active_config_exposes_security_health_error(tmp_path, monkeypatch):
    import maverick.config as cfg_mod

    path = tmp_path / "config.toml"
    path.write_text("[agent_trust\nenforce = true\n", encoding="utf-8")
    monkeypatch.setenv("MAVERICK_CONFIG", str(path))
    cfg_mod.reset_config_cache()
    try:
        assert cfg_mod.load_config() == {}  # ordinary consumers remain fail-soft
        assert str(path) in cfg_mod.config_source_errors()
    finally:
        cfg_mod.reset_config_cache()


def test_deleted_corrupt_optional_overlay_clears_security_health_error(
    tmp_path, monkeypatch,
):
    import maverick.config as cfg_mod

    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    overlay = tmp_path / cfg_mod.DASHBOARD_OVERRIDES_BASENAME
    overlay.write_text("[agent_trust\nenforce = true\n", encoding="utf-8")
    monkeypatch.setenv("MAVERICK_CONFIG", str(path))
    cfg_mod.reset_config_cache()
    try:
        cfg_mod.load_config()
        assert str(overlay) in cfg_mod.config_source_errors()

        overlay.unlink()
        cfg_mod.load_config()  # optional overlay is now skipped

        assert str(overlay) not in cfg_mod.config_source_errors()
    finally:
        cfg_mod.reset_config_cache()


def test_unreadable_optional_overlay_retains_security_health_error(
    tmp_path, monkeypatch,
):
    import maverick.config as cfg_mod

    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    overlay = tmp_path / cfg_mod.DASHBOARD_OVERRIDES_BASENAME
    overlay.write_text("[agent_trust\nenforce = true\n", encoding="utf-8")
    monkeypatch.setenv("MAVERICK_CONFIG", str(path))
    cfg_mod.reset_config_cache()
    try:
        cfg_mod.load_config()
        assert str(overlay) in cfg_mod.config_source_errors()

        real_stat = Path.stat

        def _deny_overlay_stat(self, *args, **kwargs):
            if self == overlay:
                raise PermissionError("policy source is not readable")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _deny_overlay_stat)

        assert str(overlay) in cfg_mod.config_source_errors()
    finally:
        cfg_mod.reset_config_cache()


def test_env_var_interpolation(monkeypatch):
    monkeypatch.setenv("MAVERICK_TEST_KEY", "hello")
    assert _interp("${MAVERICK_TEST_KEY}") == "hello"
    assert _interp("prefix-${MAVERICK_TEST_KEY}-suffix") == "prefix-hello-suffix"


def test_unset_env_var_becomes_empty(monkeypatch):
    monkeypatch.delenv("MAVERICK_NEVER_SET", raising=False)
    assert _interp("${MAVERICK_NEVER_SET}") == ""


def test_config_cache_avoids_reparse_but_keeps_interp_live(tmp_path, monkeypatch):
    # Uses a benign [persona] key so the literal isn't flagged by detect-secrets.
    import maverick.config as cfg_mod
    cfg_mod.reset_config_cache()
    path = tmp_path / "c.toml"
    path.write_text('[persona]\nname = "${MAVERICK_CFG_TEST_VAL}"\n')

    calls = {"n": 0}
    real_load = cfg_mod.tomllib.load

    def _counting_load(f):
        calls["n"] += 1
        return real_load(f)

    monkeypatch.setattr(cfg_mod.tomllib, "load", _counting_load)

    monkeypatch.setenv("MAVERICK_CFG_TEST_VAL", "first")
    assert cfg_mod.load_config(path)["persona"]["name"] == "first"
    # Second read: parse is cached (no new tomllib.load) ...
    monkeypatch.setenv("MAVERICK_CFG_TEST_VAL", "second")
    assert cfg_mod.load_config(path)["persona"]["name"] == "second"
    assert calls["n"] == 1  # parsed once, interpolation re-ran live

    # Editing the file (mtime/size changes) invalidates the cache.
    path.write_text(
        '[persona]\nname = "${MAVERICK_CFG_TEST_VAL}"\nstyle = "balanced"\n')
    cfg = cfg_mod.load_config(path)
    assert cfg["persona"]["style"] == "balanced"
    assert calls["n"] == 2
    cfg_mod.reset_config_cache()


def test_load_config_with_models_section():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(
            '[models]\n'
            'default = "anthropic:claude-opus-4-7"\n'
            'catalog = ["anthropic:claude-opus-4-7", "ollama:phi3:14b"]\n'
        )
        path = Path(f.name)
    try:
        cfg = load_config(path)
        assert cfg["models"]["default"] == "anthropic:claude-opus-4-7"
        assert cfg["models"]["catalog"] == [
            "anthropic:claude-opus-4-7", "ollama:phi3:14b",
        ]
    finally:
        path.unlink()


def test_nested_dict_interpolation(monkeypatch):
    monkeypatch.setenv("X", "42")
    data = {"a": "${X}", "b": ["${X}", "plain"], "c": {"inner": "${X}"}}
    out = _interp(data)
    assert out["a"] == "42"
    assert out["b"] == ["42", "plain"]
    assert out["c"]["inner"] == "42"


def test_get_safety_preserves_constitution_rules(monkeypatch):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(
            '[safety]\n'
            'block_threshold = "high"\n'
            '[[safety.constitution]]\n'
            'name = "no_zephyr"\n'
            'pattern = "zephyr-token"\n'
            'severity = "high"\n'
        )
        path = Path(f.name)
    monkeypatch.setenv("MAVERICK_CONFIG", str(path))
    try:
        safety = get_safety()
        assert safety["constitution"] == [
            {"name": "no_zephyr", "pattern": "zephyr-token", "severity": "high"}
        ]
    finally:
        path.unlink()


def test_toml_cache_is_bounded(tmp_path, monkeypatch):
    """The parsed-TOML cache must not grow without bound across many distinct
    config paths (e.g. one per tenant). Oldest entries are evicted past the cap."""
    from maverick import config as cfg
    cfg.reset_config_cache()
    monkeypatch.setattr(cfg, "_TOML_CACHE_MAX", 8)
    try:
        for i in range(40):
            p = tmp_path / f"t{i}.toml"
            p.write_text(f'[budget]\nmax_dollars = {i}\n', encoding="utf-8")
            assert cfg._read_toml_raw(p) == {"budget": {"max_dollars": i}}
        assert len(cfg._toml_cache) <= 8     # bounded, not 40
    finally:
        cfg.reset_config_cache()


def test_env_flag_tristate(monkeypatch):
    from maverick.config import env_flag

    for val in ("1", "true", "TRUE", "Yes", "on", " on "):
        monkeypatch.setenv("MAVERICK_X", val)
        assert env_flag("MAVERICK_X") is True, val
    for val in ("0", "false", "No", "off", "OFF"):
        monkeypatch.setenv("MAVERICK_X", val)
        assert env_flag("MAVERICK_X") is False, val
    for val in ("", "maybe", "2"):
        monkeypatch.setenv("MAVERICK_X", val)
        assert env_flag("MAVERICK_X") is None, val
    monkeypatch.delenv("MAVERICK_X", raising=False)
    assert env_flag("MAVERICK_X") is None


def _write_wizard_files(home, env_line: str):
    """A wizard-shaped install: config.toml referencing ${VAR} + secrets in .env."""
    mav = home / ".maverick"
    mav.mkdir(exist_ok=True)
    (mav / "config.toml").write_text(
        '[providers.anthropic]\napi_key = "${MAVERICK_ENVFILE_TEST_KEY}"\n',
        encoding="utf-8",
    )
    (mav / ".env").write_text(f"# wizard-written secrets\n{env_line}\n", encoding="utf-8")


def test_wizard_env_file_is_loaded_for_interpolation(tmp_path, monkeypatch):
    """The installer persists every collected secret ONLY to ~/.maverick/.env
    and writes config values as "${VAR}". Compose/systemd inject that file, but
    a bare pip/CLI install had NO injector, so a freshly validated key
    interpolated to "" and `maverick start` exited 2 right after `maverick
    init` said "Setup complete". load_config() must export the file itself."""
    import os

    import maverick.config as cfg_mod

    cfg_mod.reset_config_cache()
    _write_wizard_files(tmp_path, "MAVERICK_ENVFILE_TEST_KEY=tok-123")  # pragma: allowlist secret
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows Path.home()
    monkeypatch.delenv("MAVERICK_CONFIG", raising=False)
    monkeypatch.delenv("MAVERICK_ENVFILE_TEST_KEY", raising=False)
    try:
        cfg = cfg_mod.load_config()
        assert cfg["providers"]["anthropic"]["api_key"] == "tok-123"  # pragma: allowlist secret
        # ...and direct env readers (llm._provider_api_key) see it too.
        assert os.environ["MAVERICK_ENVFILE_TEST_KEY"] == "tok-123"
    finally:
        os.environ.pop("MAVERICK_ENVFILE_TEST_KEY", None)
        cfg_mod.reset_config_cache()


def test_env_file_never_overrides_exported_var(tmp_path, monkeypatch):
    """An explicitly exported variable wins over a stale ~/.maverick/.env value
    (setdefault semantics, matching compose env_file / systemd EnvironmentFile)."""
    import os

    import maverick.config as cfg_mod

    cfg_mod.reset_config_cache()
    _write_wizard_files(tmp_path, "MAVERICK_ENVFILE_TEST_KEY=stale-file-value")  # pragma: allowlist secret
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MAVERICK_CONFIG", raising=False)
    monkeypatch.setenv("MAVERICK_ENVFILE_TEST_KEY", "from-shell")
    try:
        cfg = cfg_mod.load_config()
        assert cfg["providers"]["anthropic"]["api_key"] == "from-shell"  # pragma: allowlist secret
        assert os.environ["MAVERICK_ENVFILE_TEST_KEY"] == "from-shell"
    finally:
        cfg_mod.reset_config_cache()


def test_env_file_key_satisfies_any_provider_configured(tmp_path, monkeypatch):
    """The CLI preflight path: with the key present ONLY in ~/.maverick/.env,
    any_provider_configured() must be True (was False -> exit 2)."""
    import os

    import maverick.config as cfg_mod

    cfg_mod.reset_config_cache()
    mav = tmp_path / ".maverick"
    mav.mkdir()
    (mav / "config.toml").write_text(
        '[providers.anthropic]\napi_key = "${ANTHROPIC_API_KEY}"\n', encoding="utf-8",
    )
    (mav / ".env").write_text("ANTHROPIC_API_KEY=tok-e2e\n", encoding="utf-8")  # pragma: allowlist secret
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MAVERICK_CONFIG", raising=False)
    for var in (
        cfg_mod.PROVIDER_CREDENTIAL_ENV_VARS
        + cfg_mod.PROVIDER_BASE_URL_ENV_VARS
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        assert cfg_mod.any_provider_configured() is True
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        cfg_mod.reset_config_cache()


def test_incomplete_azure_credential_does_not_satisfy_configured_probe(monkeypatch):
    import maverick.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load_config", dict)
    for var in (
        cfg_mod.PROVIDER_CREDENTIAL_ENV_VARS
        + cfg_mod.PROVIDER_BASE_URL_ENV_VARS
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "static-entra-token")

    assert cfg_mod.any_provider_configured() is False


@pytest.mark.parametrize(
    ("auth_name", "auth_value"),
    [
        ("AZURE_OPENAI_AD_TOKEN", "static-entra-token"),
        ("AZURE_OPENAI_AUTH", "entra_id"),
        ("AZURE_OPENAI_API_KEY", "azure-api-key"),
    ],
)
def test_complete_azure_route_satisfies_configured_probe(
    monkeypatch, auth_name, auth_value,
):
    import maverick.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load_config", dict)
    for var in (
        cfg_mod.PROVIDER_CREDENTIAL_ENV_VARS
        + cfg_mod.PROVIDER_BASE_URL_ENV_VARS
        + ("AZURE_OPENAI_AUTH", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt5")
    monkeypatch.setenv(auth_name, auth_value)

    assert cfg_mod.any_provider_configured() is True


def test_azure_config_auth_mode_satisfies_configured_probe(monkeypatch):
    import maverick.config as cfg_mod

    table = {
        "base_url": "https://res.openai.azure.com",
        "auth_mode": "entra_id",
    }
    monkeypatch.setattr(
        cfg_mod,
        "load_config",
        lambda: {"providers": {"azure": table}},
    )
    for var in (
        cfg_mod.PROVIDER_CREDENTIAL_ENV_VARS
        + cfg_mod.PROVIDER_BASE_URL_ENV_VARS
        + ("AZURE_OPENAI_AUTH", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")
    ):
        monkeypatch.delenv(var, raising=False)
    # A conflicting environment mode must not override the admitted table.
    monkeypatch.setenv("AZURE_OPENAI_AUTH", "api_key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt5")

    assert cfg_mod.azure_provider_configuration_missing(table) == ()
    assert cfg_mod.any_provider_configured() is True


def test_codex_login_file_satisfies_configured_probe(tmp_path, monkeypatch):
    import maverick.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "load_config", dict)
    for var in (
        cfg_mod.PROVIDER_CREDENTIAL_ENV_VARS
        + cfg_mod.PROVIDER_BASE_URL_ENV_VARS
        + ("AZURE_OPENAI_AUTH", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")
    ):
        monkeypatch.delenv(var, raising=False)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert cfg_mod.any_provider_configured() is False
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    assert cfg_mod.any_provider_configured() is True


def test_env_file_recheck_is_throttled(monkeypatch):
    """load_config() sits on per-row hot paths (_dec_field on every sealed
    column read); the .env freshness re-check must NOT stat the file on every
    call or the world_read perf SLA breaches. One check per throttle window."""
    import maverick.config as cfg_mod

    cfg_mod.reset_config_cache()
    calls = []
    monkeypatch.setattr(cfg_mod, "_load_env_file", lambda p: calls.append(p))
    try:
        for _ in range(100):
            cfg_mod.load_config()
        assert len(calls) == 1
        # reset_config_cache (the test hook) re-arms an immediate re-check.
        cfg_mod.reset_config_cache()
        cfg_mod.load_config()
        assert len(calls) == 2
    finally:
        cfg_mod.reset_config_cache()
