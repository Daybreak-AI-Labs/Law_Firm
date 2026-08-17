"""The global HALT boundary covers jobs that bypass the agent kernel."""
from __future__ import annotations

import pytest
from maverick import dreaming, killswitch, self_harness
from maverick import self_improvement as si
from maverick.learning_guard import check_learning_halt
from maverick.learning_rollout import Stage, run_rollout


@pytest.fixture(autouse=True)
def _clean_killswitch(monkeypatch, tmp_path):
    halt_file = tmp_path / "HALT"
    monkeypatch.setenv("MAVERICK_HALT_FILE", str(halt_file))

    def reset() -> None:
        killswitch.clear()
        killswitch._last_file_check_ts = 0.0
        killswitch._last_file_present = False
        killswitch._last_shared_check_ts = 0.0
        killswitch._last_shared_halt = None
        killswitch._shared_world = None

    reset()
    yield halt_file
    halt_file.unlink(missing_ok=True)
    reset()


def _reflexions() -> list[dict]:
    return [
        {
            "model_id": "M",
            "failure_class": "timeout",
            "goal_text": f"reconcile the recurring ledger export {index}",
            "failure_msg": "timed out",
        }
        for index in range(3)
    ]


def test_learning_refuses_when_local_halt_authority_is_unreadable(
    monkeypatch,
):
    class UnreadableHaltPath:
        def stat(self):
            raise PermissionError("permission denied")

    monkeypatch.setattr(
        killswitch, "_halt_file_path", lambda: UnreadableHaltPath())

    # The high-frequency agent path preserves its historical fail-open posture.
    killswitch.check(force_refresh=True)
    # Privileged learning writes fail closed when the same authority is sick.
    with pytest.raises(killswitch.Halted) as exc:
        check_learning_halt("test", "promotion")
    assert exc.value.source == "file-error"
    assert exc.value.reason == "HALT file authority unavailable"


def test_learning_refuses_when_configured_halt_mount_is_missing(
    monkeypatch, tmp_path,
):
    configured = tmp_path / "missing-mount" / "HALT"
    monkeypatch.setenv("MAVERICK_HALT_FILE", str(configured))

    # The compatibility hot path still treats an absent file as inactive.
    killswitch.check(force_refresh=True)
    with pytest.raises(killswitch.Halted) as exc:
        check_learning_halt("test", "promotion")
    assert exc.value.source == "file-error"
    assert exc.value.reason == "HALT file authority unavailable"


def test_default_halt_file_is_global_across_tenants(monkeypatch, tmp_path):
    from maverick import paths

    monkeypatch.delenv("MAVERICK_HALT_FILE", raising=False)
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / "home"))
    root_halt = paths.data_dir("HALT", tenant=None)
    root_halt.parent.mkdir(parents=True)
    root_halt.write_text("global operator stop", encoding="utf-8")
    token = paths.set_tenant("acme")
    try:
        killswitch._last_file_check_ts = 0.0
        with pytest.raises(killswitch.Halted) as ordinary:
            killswitch.check(force_refresh=True)
        assert ordinary.value.source == "file"
        with pytest.raises(killswitch.Halted) as learning:
            check_learning_halt("test", "promotion")
        assert learning.value.source == "file"
    finally:
        paths.reset_tenant(token)


def test_halt_cache_uses_monotonic_time_and_refreshes_after_clock_regression(
    monkeypatch,
):
    calls = 0

    class MissingHaltPath:
        def stat(self):
            nonlocal calls
            calls += 1
            raise FileNotFoundError

    ticks = iter((10.0, 9.0))
    # The autouse kill-switch cleanup also reads the monotonic clock during
    # teardown. Keep returning the regressed value after the two assertions so
    # the fixture cannot fail merely because this test exhausted its samples.
    monkeypatch.setattr(killswitch.time, "monotonic", lambda: next(ticks, 9.0))
    monkeypatch.setattr(
        killswitch.time, "time",
        lambda: (_ for _ in ()).throw(AssertionError("wall clock used")),
    )
    monkeypatch.setattr(killswitch, "_halt_file_path", MissingHaltPath)

    assert killswitch._file_halt_active(min_interval=1000.0) is False
    assert killswitch._file_halt_active(min_interval=1000.0) is False
    assert calls == 2


def test_local_halt_refuses_self_harness_before_proposal(
    monkeypatch, _clean_killswitch,
):
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "1")
    _clean_killswitch.write_text("operator stop", encoding="utf-8")
    proposed = 0

    def proposer(_signature):
        nonlocal proposed
        proposed += 1
        return "Inspect the export window before retrying."

    with pytest.raises(killswitch.Halted, match="source=file"):
        self_harness.run_self_harness(
            _reflexions(), model_id="M", propose_fn=proposer, min_support=3)

    assert proposed == 0


def test_local_halt_during_evaluation_blocks_followup_and_promotion(
    monkeypatch, _clean_killswitch, tmp_path,
):
    """A newly-created HALT is read fresh, not hidden by the start-check cache."""
    monkeypatch.setenv("MAVERICK_SELF_HARNESS", "1")
    calls: list[str] = []

    def first_arm(_line, _cases):
        calls.append("candidate")
        _clean_killswitch.write_text("stop during evaluation", encoding="utf-8")
        return 0.9

    def forbidden_arm(_line, _cases):
        calls.append("baseline")
        return 0.2

    store = tmp_path / "addenda.json"
    with pytest.raises(killswitch.Halted, match="source=file"):
        self_harness.run_self_harness(
            _reflexions(), model_id="M", min_support=3, path=store,
            propose_fn=lambda _sig: "Inspect the export window before retrying.",
            held_in=["dev"], held_out=["sealed"],
            score_with=first_arm, score_without=forbidden_arm,
        )

    assert calls == ["candidate"]
    assert not store.exists()


@pytest.mark.parametrize(
    "entrypoint",
    ["run_self_harness_pass", "run_self_harness_cycle", "run_self_harness_all_models"],
)
def test_self_harness_runner_wrappers_propagate_halt(monkeypatch, entrypoint):
    from maverick import self_improvement_runner as runner

    halt = killswitch.Halted("operator stop", "test")
    if entrypoint == "run_self_harness_all_models":
        monkeypatch.setattr(self_harness, "enabled", lambda: True)
        monkeypatch.setattr(runner, "harness_fleet_models", lambda: ["M"])
        monkeypatch.setattr(
            runner,
            "run_self_harness_cycle",
            lambda **_kwargs: (_ for _ in ()).throw(halt),
        )
        def call():
            return runner.run_self_harness_all_models()
    else:
        monkeypatch.setattr(
            self_harness,
            "enabled",
            lambda: (_ for _ in ()).throw(halt),
        )
        if entrypoint == "run_self_harness_pass":
            def call():
                return runner.run_self_harness_pass([], model_id="M")
        else:
            def call():
                return runner.run_self_harness_cycle(
                    reflexions=[], model_id="M", retire=False)

    with pytest.raises(killswitch.Halted, match="source=test"):
        call()


def test_local_halt_refuses_dream_before_replay_or_write(
    monkeypatch, _clean_killswitch, tmp_path,
):
    _clean_killswitch.write_text("maintenance stop", encoding="utf-8")
    monkeypatch.setattr(
        dreaming, "settings",
        lambda: (_ for _ in ()).throw(AssertionError("dream must stop before settings/replay")),
    )

    with pytest.raises(killswitch.Halted, match="source=file"):
        dreaming.dream_cycle(
            insights_path=tmp_path / "insights.ndjson",
            rehearsals_path=tmp_path / "rehearsals.ndjson")
    assert not (tmp_path / "insights.ndjson").exists()


def test_dream_cli_reports_halt_as_a_clean_refusal(
    monkeypatch, _clean_killswitch, tmp_path,
):
    from click.testing import CliRunner
    from maverick.cli import main

    monkeypatch.setenv("MAVERICK_DREAMING", "1")
    _clean_killswitch.write_text("operator stop", encoding="utf-8")

    result = CliRunner().invoke(
        main, ["--db", str(tmp_path / "world.db"), "dream"])

    assert result.exit_code == 1
    assert result.output == (
        "Error: dreaming refused: global learning HALT is active\n"
    )
    assert "operator stop" not in result.output
    assert str(_clean_killswitch) not in result.output
    assert "Traceback" not in result.output


def test_halt_armed_during_distillation_blocks_the_skill_save(
    monkeypatch, _clean_killswitch, tmp_path,
):
    from maverick.skill import distillation_v2

    called = 0

    def arm_before_save(_trajectories, **kwargs):
        nonlocal called
        called += 1
        _clean_killswitch.write_text("stop before skill save", encoding="utf-8")
        kwargs["before_save"]()
        raise AssertionError("HALT callback must not return")

    monkeypatch.setattr(
        distillation_v2, "distill_and_save_gated", arm_before_save)

    with pytest.raises(killswitch.Halted, match="source=file"):
        dreaming._distill_department_skills(
            {"finance": [{"success": True}, {"success": True}]},
            skill_store=tmp_path / "skills", min_cluster=2,
        )
    assert called == 1
    assert not (tmp_path / "skills").exists()


def test_self_improvement_returns_killswitch_gate_before_evaluation(
    monkeypatch, _clean_killswitch, tmp_path,
):
    monkeypatch.setattr(si, "enabled", lambda: True)
    _clean_killswitch.write_text("freeze promotions", encoding="utf-8")
    controller = si.SelfImprovementController(
        ledger=si.PromotionLedger(tmp_path / "promotions.json"))
    monkeypatch.setattr(
        controller, "evaluate",
        lambda _cand: (_ for _ in ()).throw(AssertionError("gate evaluation must not run")),
    )
    candidate = si.Candidate(
        rung="prompt", summary="candidate", baseline_score=0.1,
        candidate_score=0.9, samples=10,
        rollback={"action": "restore", "target": "prompt"})

    verdict = controller.promote(candidate)

    assert verdict.ok is False
    assert verdict.gates[0].gate == "killswitch"
    assert "source=file" in verdict.blocking_reason
    assert controller.ledger.all() == []


def test_rollout_halt_after_deploy_rolls_back_before_constraints(
    _clean_killswitch,
):
    deployed: list[float] = []
    rolled_back: list[str] = []
    evaluated = 0

    def deploy(candidate: str, fraction: float) -> None:
        assert candidate == "skill-v2"
        deployed.append(fraction)
        _clean_killswitch.write_text("stop canary", encoding="utf-8")

    def constraint(_candidate: str, _fraction: float):
        nonlocal evaluated
        evaluated += 1
        return True, "healthy"

    def rollback(candidate: str) -> bool:
        rolled_back.append(candidate)
        return True

    result = run_rollout(
        "skill-v2", [Stage("canary", 0.1), Stage("full", 1.0)], [constraint],
        deploy=deploy, rollback=rollback)

    assert deployed == [0.1]
    assert evaluated == 0
    assert rolled_back == ["skill-v2"]
    assert result.rolled_back is True and result.completed is False
    assert "source=file" in result.reason
