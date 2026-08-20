"""Installer configuration parity with retained kernel features."""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover -- Py 3.10 CI matrix
    import tomli as tomllib  # type: ignore[no-redef]


# ---------- _safe_int / _safe_float ----------

def test_safe_int_handles_whitespace():
    from maverick_installer.wizard import _safe_int
    assert _safe_int("  42 ", default=0) == 42


def test_safe_int_falls_back_on_junk():
    from maverick_installer.wizard import _safe_int
    assert _safe_int("not-a-number", default=9) == 9
    assert _safe_int("", default=5) == 5
    assert _safe_int(None, default=7) == 7  # type: ignore[arg-type]


def test_safe_float_falls_back_on_junk():
    from maverick_installer.wizard import _safe_float
    assert _safe_float("xyz", default=1.5) == 1.5
    assert _safe_float("", default=2.5) == 2.5
    assert _safe_float("3.14", default=0) == 3.14


# ---------- wizard sections must be known to config-lint ----------

def test_wizard_written_sections_are_known_to_config_lint():
    """Every [section] block the wizard writes must be a section the runtime
    registry (migrate.KNOWN_SECTIONS, which config-lint sources) recognizes --
    otherwise an operator who enables a documented, wizard-offered feature gets
    a false "unknown config section" warning. Regression: [self_harness],
    [self_improvement], [dreaming], [rehearsal], [memory_guard],
    [actions], [domains], [fairness_monitor] and [speculative] all
    shipped unrecognized (the section-parity test only checked the two
    registries against each other, not against what the wizard writes)."""
    import re
    from pathlib import Path

    from maverick.migrate import KNOWN_SECTIONS
    from maverick_installer import wizard

    src = Path(wizard.__file__).read_text(encoding="utf-8")
    written = set(re.findall(
        r"""lines\.append\(\s*["']\[([a-z_][a-z0-9_]*)\]["']\s*\)""", src))
    assert written, "no [section] writes found in the wizard -- regex stale?"
    missing = sorted(written - set(KNOWN_SECTIONS))
    assert not missing, f"wizard writes sections config-lint will false-flag: {missing}"


def test_model_risk_literal_sections_are_known_to_config_lint():
    from maverick.migrate import KNOWN_SECTIONS
    from maverick_installer import wizard

    rendered = wizard._cfg_security_suite({
        "security_ops": True,
        "evidence_graph": True,
        "model_risk_assurance": True,
    })
    sections = set(tomllib.loads("\n".join(rendered)))
    missing = sorted(sections - set(KNOWN_SECTIONS))
    assert not missing, f"model-risk sections config-lint will false-flag: {missing}"


@pytest.mark.parametrize("name", [
    "pick_web_search",
    "pick_tool_acl",
    "pick_rate_limits",
    "pick_retention",
    "pick_persona",
    "pick_self_learning",
])
def test_new_pick_exists(name):
    from maverick_installer import wizard
    assert callable(getattr(wizard, name)), f"{name} missing"


def test_collect_api_keys_prompts_for_openai_compatible_base_url(monkeypatch):
    from maverick_installer import wizard

    prompts: list[str] = []
    answers = {
        "OPENAI_COMPATIBLE_API_KEY": "sk-compatible",
        "OPENAI_COMPATIBLE_BASE_URL": "http://localhost:1234/v1",
    }

    def fake_secret(prompt: str, *args, **kwargs):
        prompts.append(prompt)
        for env_name, value in answers.items():
            if env_name in prompt:
                return value
        return ""

    monkeypatch.setattr(wizard, "_q_secret", fake_secret)
    keys = wizard.collect_api_keys(["openai_compatible"], set())

    assert keys == answers
    assert any("OPENAI_COMPATIBLE_API_KEY" in prompt for prompt in prompts)
    assert any("OPENAI_COMPATIBLE_BASE_URL" in prompt for prompt in prompts)


# ---------- one explicit run-wide model pin ----------

def test_provider_and_model_pickers_have_no_vendor_default(monkeypatch):
    from maverick_installer import wizard

    seen_defaults: list[str | None] = []

    def select(_message, choices, default=None):
        seen_defaults.append(default)
        return next(choice for choice in choices if choice.startswith("openai"))

    monkeypatch.setattr(wizard, "_q_select", select)

    providers = wizard.pick_providers()
    run_model = wizard.pick_run_model(providers)

    assert providers == ["openai"]
    assert run_model.startswith("openai:")
    assert seen_defaults == [None, None]


@pytest.mark.parametrize(
    ("providers", "run_model"),
    [
        (["anthropic"], ""),
        (["anthropic"], "claude-sonnet-4-6"),
        (["anthropic"], "openai:gpt-5.4"),
        (["anthropic", "openai"], "anthropic:claude-sonnet-4-6"),
        (["anthropic"], "anthropic:not-in-the-catalog"),
    ],
)
def test_write_config_rejects_missing_or_mismatched_model_pin(
    tmp_path: Path,
    monkeypatch,
    providers: list[str],
    run_model: str,
):
    with pytest.raises(ValueError):
        _write_full_config(
            tmp_path,
            monkeypatch,
            providers=providers,
            run_model=run_model,
        )


def test_invalid_model_pin_fails_before_credentials_are_written(
    tmp_path: Path,
    monkeypatch,
):
    from maverick_installer import wizard

    monkeypatch.setattr(wizard, "CONFIG_DIR", tmp_path / ".maverick")
    monkeypatch.setattr(wizard, "CONFIG_FILE", tmp_path / ".maverick" / "config.toml")
    monkeypatch.setattr(wizard, "ENV_FILE", tmp_path / ".maverick" / ".env")

    with pytest.raises(ValueError):
        wizard.write_config(
            providers=["anthropic"],
            run_model="openai:gpt-5.4",
            safety={"profile": "balanced"},
            budget={"max_dollars": 5.0},
            sandbox={"backend": "local", "workdir": str(tmp_path / "work")},
            keys={
                "ANTHROPIC_API_KEY": "must-not-be-written",  # pragma: allowlist secret
            },
        )

    assert not wizard.CONFIG_DIR.exists()


def test_write_config_emits_only_one_global_model_pin(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(tmp_path, monkeypatch)

    assert parsed["models"] == {"default": "anthropic:claude-sonnet-4-6"}


def test_every_catalog_provider_emits_its_exact_selected_model(
    tmp_path: Path,
    monkeypatch,
):
    from maverick_installer import models

    for provider, info in models.PROVIDERS.items():
        run_model = f"{provider}:{info['models'][0]['id']}"
        parsed = _write_full_config(
            tmp_path / provider,
            monkeypatch,
            providers=[provider],
            run_model=run_model,
        )
        assert parsed["models"] == {"default": run_model}
        assert set(parsed["providers"]) == {provider}


# ---------- write_config emits new TOML sections ----------

def _write_full_config(tmp_path: Path, monkeypatch, **overrides) -> dict:
    from maverick_installer import wizard
    monkeypatch.setattr(wizard, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wizard, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(wizard, "ENV_FILE", tmp_path / ".env")
    base = dict(
        providers=["anthropic"],
        run_model="anthropic:claude-sonnet-4-6",
        safety={"profile": "balanced", "block_threshold": "high",
                "scan_input": True, "scan_tool_calls": True, "scan_output": True},
        budget={"max_dollars": 5.0, "max_wall_seconds": 3600.0, "max_tool_calls": 500},
        sandbox={"backend": "local", "workdir": str(tmp_path / "ws"), "timeout": 60},
        keys={},
        capabilities={"computer_use": False, "browser": False},
    )
    base.update(overrides)
    wizard.write_config(**base)
    body = (tmp_path / "config.toml").read_text()
    return tomllib.loads(body)


def test_write_config_emits_knowledge(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        knowledge={"enable": True, "embedder": "local", "store": "sqlite"},
    )
    assert parsed["knowledge"]["enable"] is True
    assert parsed["knowledge"]["embedder"] == "local"
    assert parsed["knowledge"]["store"] == "sqlite"


def test_write_config_omits_knowledge_by_default(tmp_path: Path, monkeypatch):
    # No override -> no [knowledge] section (per-domain RAG is opt-in).
    parsed = _write_full_config(tmp_path, monkeypatch)
    assert "knowledge" not in parsed


def test_write_config_builds_deterministic_on_box(tmp_path: Path, monkeypatch):
    from maverick import config
    from maverick_knowledge.embed import DeterministicEmbedder, build_embedder

    parsed = _write_full_config(
        tmp_path, monkeypatch,
        knowledge={"enable": True, "embedder": "deterministic", "store": "sqlite"},
    )

    monkeypatch.setattr(config, "load_config", lambda *a, **k: parsed)
    monkeypatch.delenv("MAVERICK_EMBED_PROVIDER", raising=False)
    resolved = config.get_knowledge()
    assert isinstance(build_embedder(resolved), DeterministicEmbedder)


def test_legacy_hosted_knowledge_config_fails_closed(monkeypatch):
    from maverick import config
    from maverick_knowledge.embed import build_embedder

    monkeypatch.setattr(
        config, "load_config",
        lambda *a, **k: {"knowledge": {"enable": True, "embedder": "hosted"}},
    )
    monkeypatch.delenv("MAVERICK_EMBED_PROVIDER", raising=False)
    resolved = config.get_knowledge()
    with pytest.raises(RuntimeError, match="external embedder 'hosted' was removed"):
        build_embedder(resolved)


def test_write_config_emits_regulated_scalars(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        advanced={"compliance_disclosure_text": "AI assistant in use."},
    )
    assert parsed["compliance"]["disclosure_text"] == "AI assistant in use."


def test_write_config_self_harness_ships_production_floors(tmp_path: Path, monkeypatch):
    # Enabling self-harness writes the validation floors so a wizard-built config
    # is production-safe by default (an operator can relax them).
    parsed = _write_full_config(tmp_path, monkeypatch, advanced={"self_harness": True})
    assert parsed["self_harness"]["enable"] is True
    assert parsed["self_harness"]["require_held_out"] is True
    assert parsed["self_harness"]["min_held_out"] == 5
    assert parsed["self_harness"]["min_delta"] == 0.02
    # The optional advanced knobs are NOT written unless opted into, so the
    # default block stays minimal and each knob keeps its historical default.
    for k in ("eval_corpus", "eval_budget_dollars", "mine_bucket_by",
              "semantic_mining", "efficacy_review", "promote_as_canary",
              "auto_run", "metamorphic", "relapse_failure_share",
              "calibrate_judge", "corpus_harvest", "store",
              "candidates_per_signature", "retire_after_days"):
        assert k not in parsed["self_harness"]


def test_write_config_self_harness_advanced_knobs(tmp_path: Path, monkeypatch):
    # The advanced-paths follow-up surfaces the newer knobs; the writer emits each
    # one only when opted into, and they round-trip through config.get_self_harness.
    parsed = _write_full_config(tmp_path, monkeypatch, advanced={
        "self_harness": True,
        "self_harness_eval_corpus": "/etc/maverick/corpus.json",
        "self_harness_eval_budget": 2.5,
        "self_harness_bucket_domain": True,
        "self_harness_semantic_mining": True,
        "self_harness_efficacy_review": True,
        "self_harness_canary": True,
        "self_harness_auto_run": True,
        "self_harness_metamorphic": True,
        "self_harness_relapse": True,
        "self_harness_calibrate_judge": True,
        "self_harness_corpus_harvest": "propose",
        "self_harness_store": "world",
        "self_harness_candidates": 3,
        "self_harness_retire_days": 45,
    })
    sh = parsed["self_harness"]
    assert sh["eval_corpus"] == "/etc/maverick/corpus.json"
    assert sh["eval_budget_dollars"] == 2.5
    assert sh["mine_bucket_by"] == ["domain"]
    assert sh["semantic_mining"] is True
    assert sh["efficacy_review"] is True
    assert sh["promote_as_canary"] is True
    assert sh["auto_run"] is True
    assert sh["metamorphic"] is True
    assert sh["relapse_failure_share"] == 0.5
    assert sh["calibrate_judge"] is True
    assert sh["corpus_harvest"] == "propose"
    assert sh["store"] == "world"
    assert sh["candidates_per_signature"] == 3
    assert sh["retire_after_days"] == 45
    # The kernel actually reads what the wizard wrote.
    from maverick import config
    monkeypatch.setattr(config, "load_config", lambda *a, **k: parsed)
    resolved = config.get_self_harness()
    assert resolved["eval_corpus"] == "/etc/maverick/corpus.json"
    assert resolved["eval_budget_dollars"] == 2.5
    assert resolved["mine_bucket_by"] == ("domain",)
    assert resolved["semantic_mining"] is True
    assert resolved["efficacy_review"] is True
    assert resolved["promote_as_canary"] is True
    assert resolved["auto_run"] is True
    assert resolved["metamorphic"] is True
    assert resolved["relapse_failure_share"] == 0.5
    assert resolved["calibrate_judge"] is True
    assert resolved["corpus_harvest"] == "propose"
    assert resolved["store"] == "world"
    assert resolved["candidates_per_signature"] == 3
    assert resolved["retire_after_days"] == 45.0


def test_write_config_self_harness_default_candidate_count_omitted(tmp_path: Path, monkeypatch):
    # Opting into the advanced follow-up but leaving best-of-N / retire at their
    # defaults must NOT write the knob (default 1 / 0 == historical behavior).
    parsed = _write_full_config(tmp_path, monkeypatch, advanced={
        "self_harness": True,
        "self_harness_candidates": 1,
        "self_harness_retire_days": 0,
        "self_harness_eval_corpus": "/etc/maverick/corpus.json",
        "self_harness_eval_budget": 0,          # explicit 0 = uncapped, not written
    })
    assert "candidates_per_signature" not in parsed["self_harness"]
    assert "retire_after_days" not in parsed["self_harness"]
    assert "eval_budget_dollars" not in parsed["self_harness"]


def test_write_config_emits_self_learning(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        self_learning={
            "enable": True,
            "allow_provider_egress": False,
            "distill_local": True,
        },
    )
    assert parsed["self_learning"]["enable"] is True
    assert parsed["self_learning"]["allow_provider_egress"] is False
    assert parsed["self_learning"]["distill_local"] is True


def test_write_config_emits_durable_when_enabled(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        durable={"enabled": True, "keep_last": 5},
    )
    assert parsed["durable"]["enabled"] is True
    assert parsed["durable"]["keep_last"] == 5


def test_write_config_omits_durable_when_disabled(tmp_path: Path, monkeypatch):
    # Off by default: a disabled durable dict writes no [durable] section,
    # keeping the config minimal (the kernel defaults to off anyway).
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        durable={"enabled": False},
    )
    assert "durable" not in parsed


def test_write_config_roundtrips_backslash_paths(tmp_path: Path, monkeypatch):
    """A Windows backslash workdir must round-trip as a TOML string."""
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        sandbox={"backend": "local", "workdir": r"C:\Users\me\maverick ws", "timeout": 60},
    )
    assert parsed["sandbox"]["workdir"] == r"C:\Users\me\maverick ws"


def test_write_config_emits_tool_acl(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        tool_acl={"denied_tools": ["web_search"]},
    )
    assert parsed["security"]["denied_tools"] == ["web_search"]


def test_write_config_emits_rate_limits_with_glob(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        rate_limits={"web_search": "10/60", "http_*": "60/60"},
    )
    assert parsed["rate_limits"]["web_search"] == "10/60"
    # Glob keys must be quoted in TOML.
    assert parsed["rate_limits"]["http_*"] == "60/60"


def test_write_config_emits_retention(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        retention={"audit_days": 90, "episodes_days": 365, "events_days": 180},
    )
    assert parsed["retention"]["audit_days"] == 90
    assert parsed["retention"]["episodes_days"] == 365


def test_write_config_emits_persona(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        persona={"name": "Hawk", "style": "concise"},
    )
    assert parsed["persona"]["name"] == "Hawk"
    assert parsed["persona"]["style"] == "concise"


def test_write_config_emits_personas(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        personas={"default": ["fpa_analyst", "treasurer"]},
    )
    assert parsed["personas"]["default"] == ["fpa_analyst", "treasurer"]


def test_write_config_emits_web_search_capability(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch, web_search_enabled=True,
    )
    assert parsed["capabilities"]["web_search"] is True


def test_write_config_omits_empty_optional_sections(tmp_path: Path, monkeypatch):
    """Unspecified optionals should not emit empty sections."""
    parsed = _write_full_config(tmp_path, monkeypatch)
    for sec in ("security", "rate_limits", "retention", "persona",
                "self_learning"):
        assert sec not in parsed, f"{sec} should be absent"


# ---------- pick_*() functions return safe defaults when declined ----------

class _StubQ:
    """Mock the questionary primitives — every prompt returns 'no'/empty."""

    def __init__(self, monkeypatch):
        monkeypatch.setattr(
            "maverick_installer.wizard._q_confirm",
            lambda *a, **kw: False,
        )
        monkeypatch.setattr(
            "maverick_installer.wizard._q_text",
            lambda *a, **kw: kw.get("default", ""),
        )
        monkeypatch.setattr(
            "maverick_installer.wizard._q_select",
            lambda *a, **kw: kw.get("default", a[1][0]) if len(a) > 1 else "",
        )
        monkeypatch.setattr(
            "maverick_installer.wizard._q_checkbox",
            lambda *a, **kw: kw.get("default", []),
        )


def test_pick_tool_acl_skipped(monkeypatch):
    _StubQ(monkeypatch)
    from maverick_installer.wizard import pick_tool_acl
    assert pick_tool_acl() == {}


def test_pick_rate_limits_skipped(monkeypatch):
    _StubQ(monkeypatch)
    from maverick_installer.wizard import pick_rate_limits
    assert pick_rate_limits() == {}


def test_pick_retention_skipped(monkeypatch):
    _StubQ(monkeypatch)
    from maverick_installer.wizard import pick_retention
    assert pick_retention() == {}


def test_pick_persona_skipped(monkeypatch):
    _StubQ(monkeypatch)
    from maverick_installer.wizard import pick_persona
    assert pick_persona() == {}


def test_pick_web_search_skipped(monkeypatch):
    _StubQ(monkeypatch)
    from maverick_installer.wizard import pick_web_search
    enabled, envs = pick_web_search()
    assert enabled is False
    assert envs == []


# ---------- new advanced knobs (build-wave feature toggles) ----------

def test_write_config_emits_tools_output_cache(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch, advanced={"output_cache": True},
    )
    assert parsed["tools"]["output_cache"] is True


def test_write_config_emits_consequence_when_enabled(tmp_path: Path, monkeypatch):
    # The Consequence Engine had a config knob + kernel reader but no wizard step,
    # so it was unreachable through the installer. Pin that it now emits.
    parsed = _write_full_config(
        tmp_path, monkeypatch, advanced={"consequence": True},
    )
    assert parsed["consequence"]["enable"] is True


def test_write_config_persists_consequence_opt_out(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(tmp_path, monkeypatch, advanced={"consequence": False})
    assert parsed["consequence"]["enable"] is False


def test_write_config_emits_self_learning_distill_local(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        self_learning={"enable": True, "distill_local": True},
    )
    assert parsed["self_learning"]["distill_local"] is True


def test_write_config_omits_new_knobs_when_off(tmp_path: Path, monkeypatch):
    # Empty advanced -> no retired routing/system tables and no [tools].
    parsed = _write_full_config(tmp_path, monkeypatch, advanced={})
    assert "system" not in parsed
    assert "routing" not in parsed
    assert "tools" not in parsed


def test_pick_advanced_includes_output_cache(monkeypatch):
    _StubQ(monkeypatch)
    from maverick_installer.wizard import pick_advanced
    adv = pick_advanced()
    assert "output_cache" in adv
    retired = {
        "cost_aware", "local_first", "energy_aware", "hedge_requests",
        "verify_ensemble",
    }
    assert not (retired & set(adv))


def test_pick_self_learning_includes_distill_local(monkeypatch):
    # Force every confirm to True so the enabled branch runs.
    monkeypatch.setattr("maverick_installer.wizard._q_confirm", lambda *a, **kw: True)
    from maverick_installer.wizard import pick_self_learning
    result = pick_self_learning()
    assert result["enable"] is True
    assert result["distill_local"] is True


def test_pick_self_learning_defaults_to_safe_governed_learning(monkeypatch):
    """Accepting every prompt default enables learning, not executable autonomy."""
    monkeypatch.setattr(
        "maverick_installer.wizard._q_confirm",
        lambda *a, default=False, **kw: default,
    )
    from maverick_installer.wizard import pick_self_learning

    result = pick_self_learning()

    assert result == {
        "enable": True,
        "allow_provider_egress": False,
        "distill_local": True,
    }


def test_retired_finance_suite_is_not_advertised_or_configurable():
    import inspect

    from maverick.migrate import KNOWN_SECTIONS
    from maverick_installer import wizard

    assert not hasattr(wizard, "pick_finance")
    assert "finance" not in inspect.signature(wizard.write_config).parameters
    assert {"finance", "finance_operations", "screening"}.isdisjoint(KNOWN_SECTIONS)


def test_retired_governed_connector_writes_are_not_advertised_or_configurable():
    import inspect

    import maverick.config as config
    from maverick.migrate import KNOWN_SECTIONS
    from maverick_installer import wizard

    assert not hasattr(wizard, "pick_governed_connectors")
    assert "governed_connectors" not in inspect.signature(wizard.write_config).parameters
    assert not hasattr(config, "get_governed_connectors")
    assert "governed_connectors" not in KNOWN_SECTIONS


# ---------- learning defaults: reasoning_reward + jit_rl opt-out, governance opt-in ----------

def test_pick_advanced_includes_learning_toggles(monkeypatch):
    _StubQ(monkeypatch)
    from maverick_installer.wizard import pick_advanced
    adv = pick_advanced()
    for key in ("structured_verifier_off", "jit_rl_off",
                "reward_laundering", "signed_approval"):
        assert key in adv, f"{key} missing from pick_advanced()"


def test_write_config_disables_reasoning_reward(tmp_path: Path, monkeypatch):
    # The rubric verifier is on by default; the wizard opt-out writes enable=false.
    parsed = _write_full_config(
        tmp_path, monkeypatch, advanced={"structured_verifier_off": True})
    assert parsed["reasoning_reward"]["enable"] is False
    # The kernel reads what the wizard wrote.
    from maverick import config
    monkeypatch.setattr(config, "load_config", lambda *a, **k: parsed)
    assert config.get_reasoning_reward()["enable"] is False


def test_write_config_disables_jit_rl(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(tmp_path, monkeypatch, advanced={"jit_rl_off": True})
    assert parsed["jit_rl"]["enable"] is False
    from maverick import config
    monkeypatch.setattr(config, "load_config", lambda *a, **k: parsed)
    assert config.get_jit_rl()["enable"] is False


def test_write_config_emits_reward_laundering_floor(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch, advanced={"reward_laundering": True})
    assert parsed["calibration"]["enforce"] is True
    assert parsed["calibration"]["min_resistance"] == 0.5
    from maverick import config
    monkeypatch.setattr(config, "load_config", lambda *a, **k: parsed)
    assert config.get_calibration()["min_resistance"] == 0.5


def test_write_config_emits_signed_approval(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch, advanced={"signed_approval": True})
    assert parsed["self_improvement"]["require_signed_approval"] is True
    from maverick import config
    monkeypatch.setattr(config, "load_config", lambda *a, **k: parsed)
    assert config.get_self_improvement()["require_signed_approval"] is True


def test_write_config_omits_learning_knobs_by_default(tmp_path: Path, monkeypatch):
    # No advanced opt-in -> the on-by-default features write no override sections,
    # and the opt-in governance knobs are absent.
    parsed = _write_full_config(tmp_path, monkeypatch, advanced={})
    assert "reasoning_reward" not in parsed
    assert "jit_rl" not in parsed
    assert "calibration" not in parsed
    assert "self_improvement" not in parsed


def test_write_config_ignores_retired_factory_learning_knob(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(
        tmp_path, monkeypatch, advanced={"factory_learning": False}
    )
    assert "self_improvement" not in parsed


def test_write_config_emits_audit_rewards(tmp_path: Path, monkeypatch):
    parsed = _write_full_config(tmp_path, monkeypatch, advanced={"audit_rewards": True})
    assert parsed["reasoning_reward"]["audit_rewards"] is True
    from maverick import config
    monkeypatch.setattr(config, "load_config", lambda *a, **k: parsed)
    assert config.get_reasoning_reward()["audit_rewards"] is True


def test_write_config_reasoning_reward_both_keys_one_table(tmp_path: Path, monkeypatch):
    # structured_verifier_off + audit_rewards must share ONE [reasoning_reward]
    # table (two tables would be invalid TOML).
    parsed = _write_full_config(
        tmp_path, monkeypatch,
        advanced={"structured_verifier_off": True, "audit_rewards": True})
    assert parsed["reasoning_reward"]["enable"] is False
    assert parsed["reasoning_reward"]["audit_rewards"] is True


def test_write_config_never_emits_self_modify(tmp_path: Path, monkeypatch):
    """The DGM rung is deleted; no wizard path may write its section back."""
    parsed = _write_full_config(tmp_path, monkeypatch, advanced={})
    assert "self_modify" not in parsed


