"""Product-default contract for governed learning and the separate DGM gate."""
from __future__ import annotations

from maverick import (
    consequence,
    credit,
    data_engine,
    dreaming,
    evaluator_evolution,
    experience,
    failure_telemetry,
    jit_rl,
    operations_scientist,
    prm_guidance,
    promotion_effect,
    reasoning_reward,
    reflexion,
    rehearsal,
    self_harness,
    self_improvement,
    self_learning,
    self_tuning_budget,
    trajectory_store,
)
from maverick.skill import distillation_local

_ENV_FLAGS = (
    "MAVERICK_SELF_LEARNING",
    "MAVERICK_REFLEXION",
    "MAVERICK_DREAMING",
    "MAVERICK_SELF_HARNESS",
    "MAVERICK_SELF_IMPROVEMENT",
    "MAVERICK_TRAJECTORY_CAPTURE",
    "MAVERICK_DISTILL_LOCAL",
    "MAVERICK_CONSEQUENCE",
    "MAVERICK_CREDIT",
    "MAVERICK_DATA_ENGINE",
    "MAVERICK_EXPERIENCE_GUIDANCE",
    "MAVERICK_REHEARSAL",
    "MAVERICK_OPERATIONS_SCIENTIST",
    "MAVERICK_EVALUATOR_EVOLUTION",
    "MAVERICK_FAILURE_TELEMETRY",
    "MAVERICK_BUDGET_SELF_TUNING",
    "MAVERICK_PRM_GUIDANCE",
    "MAVERICK_REASONING_REWARD",
    "MAVERICK_REASONING_REWARD_AUDIT",
    "MAVERICK_JIT_RL",
    "MAVERICK_CAUSAL_PROMOTION",
)


def _isolate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    for name in _ENV_FLAGS:
        monkeypatch.delenv(name, raising=False)
    from maverick.config import reset_config_cache
    reset_config_cache()


def _governed_states() -> dict[str, bool]:
    return {
        "self_learning": self_learning.enabled(),
        "reflexion": reflexion.enabled(),
        "dreaming": dreaming.enabled(),
        "self_harness": self_harness.enabled(),
        "self_improvement": self_improvement.enabled(),
        "capture": trajectory_store.enabled(),
        "distill_local": distillation_local.enabled(),
        "consequence": consequence.enabled(),
        "credit": credit.enabled(),
        "data_engine": data_engine.enabled(),
        "experience": experience.enabled(),
        "rehearsal": rehearsal.enabled(),
        "operations_scientist": operations_scientist.enabled(),
        "evaluator_evolution": evaluator_evolution.enabled(),
        "failure_telemetry": failure_telemetry.enabled(),
        "budget_tuning": self_tuning_budget.enabled(),
        "prm_guidance": prm_guidance.enabled(),
        "reasoning_reward": reasoning_reward.enabled(),
        "reward_audit": reasoning_reward.audit_rewards_enabled(),
        "jit_rl": jit_rl.enabled(),
        "causal_promotion": promotion_effect.enabled(),
    }


def test_governed_learning_defaults_on(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert all(_governed_states().values())
    from maverick.config import get_self_harness, get_self_improvement, get_self_learning

    assert get_self_learning()["allow_provider_egress"] is False
    assert get_self_harness()["risk_limited"] is True
    assert get_self_harness()["auto_run"] is True
    assert get_self_improvement()["capture"] is True
    assert "factory_learning" not in get_self_improvement()


def test_malformed_learning_booleans_fail_closed(
    tmp_path, monkeypatch,
):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "config.toml").write_text(
        """
[self_learning]
enable = "false"
allow_provider_egress = "false"

[self_harness]
enable = "false"
risk_limited = "false"
auto_run = "false"

[self_improvement]
enable = "false"
causal_promotion = "false"
require_signed_approval = "false"
""".strip() + "\n",
        encoding="utf-8",
    )
    from maverick.config import (
        get_self_harness,
        get_self_improvement,
        get_self_learning,
        reset_config_cache,
    )
    reset_config_cache()

    settings = get_self_learning()
    assert settings["enable"] is False
    assert settings["allow_provider_egress"] is False
    assert self_learning.provider_egress_enabled() is False
    harness = get_self_harness()
    assert harness["enable"] is False
    assert harness["risk_limited"] is True
    assert harness["auto_run"] is False
    improvement = get_self_improvement()
    assert improvement["enable"] is False
    assert improvement["causal_promotion"] is True
    assert improvement["require_signed_approval"] is True


def test_malformed_present_learning_environment_fails_closed(
    tmp_path, monkeypatch,
):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "not-a-boolean")
    assert self_harness.enabled() is False
    monkeypatch.delenv("MAVERICK_SELF_HARNESS")
    monkeypatch.setenv("MAVERICK_CAUSAL_PROMOTION", "not-a-boolean")
    assert promotion_effect.enabled() is False


def test_invalid_global_min_improvement_freezes_shared_promotion(
    tmp_path, monkeypatch,
):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "config.toml").write_text(
        '[self_improvement]\nmin_improvement = "0.25"\n',
        encoding="utf-8",
    )
    from maverick.config import get_self_improvement, reset_config_cache

    reset_config_cache()
    settings = get_self_improvement()
    assert settings["promotion_policy_valid"] is False
    assert settings["min_improvement"] == -1.0

    self_improvement.reset_shared()
    try:
        controller = self_improvement.shared()
        verdict = controller.evaluate(self_improvement.Candidate(
            rung="config",
            summary="must not promote under a malformed global margin",
            baseline_score=0.1,
            candidate_score=0.9,
            samples=20,
            rollback="snapshot-1",
        ))
        assert verdict.promote is False
        assert any(
            gate.gate == "evidence" and gate.ok is False
            for gate in verdict.gates
        )
    finally:
        self_improvement.reset_shared()


def test_zero_environment_overrides_disable_default_learning(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for name in _ENV_FLAGS:
        monkeypatch.setenv(name, "0")
    assert not any(_governed_states().values())


def test_corrupt_active_config_fails_default_learning_closed(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "config.toml").write_text("[self_learning\nenable = true\n")
    from maverick.config import reset_config_cache
    reset_config_cache()
    assert not any(_governed_states().values())


