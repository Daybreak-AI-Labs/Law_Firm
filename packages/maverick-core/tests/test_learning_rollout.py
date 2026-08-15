"""Governed rollout of learning: staged promotion with auto-rollback."""
from __future__ import annotations

from pathlib import Path

import pytest
from maverick.learning_rollout import (
    DEFAULT_STAGES,
    DeploymentReceipt,
    Stage,
    run_rollout,
    threshold_constraint,
)


def _recorder():
    deployed: list = []
    rolled: list = []

    def rollback(candidate):
        rolled.append(candidate)
        return True

    return deployed, rolled, (lambda c, f: deployed.append((c, f))), rollback


def test_all_constraints_pass_completes_full_rollout():
    deployed, rolled, deploy, rollback = _recorder()
    ok = threshold_constraint("win_rate", lambda c, f: 0.9, floor=0.8)
    res = run_rollout("skill-x", DEFAULT_STAGES, [ok], deploy=deploy, rollback=rollback)
    assert res.completed and not res.rolled_back
    assert res.reached_fraction == 1.0
    assert [f for _c, f in deployed] == [0.1, 0.5, 1.0]   # canary -> half -> full
    assert rolled == []


def test_failing_constraint_auto_rolls_back_and_stops():
    deployed, rolled, deploy, rollback = _recorder()
    # win-rate collapses once past the canary (fraction > 0.1)
    def metric(c, f):
        return 0.9 if f <= 0.1 else 0.4
    res = run_rollout("skill-x", DEFAULT_STAGES,
                      [threshold_constraint("win_rate", metric, floor=0.8)],
                      deploy=deploy, rollback=rollback)
    assert res.rolled_back and not res.completed
    assert "win_rate" in res.reason
    assert res.reached_fraction == 0.1                 # only the canary stuck
    assert [f for _c, f in deployed] == [0.1, 0.5]     # deployed canary + half, then stopped
    assert rolled == ["skill-x"]                       # rolled back exactly once


def test_canary_failure_rolls_back_immediately():
    deployed, rolled, deploy, rollback = _recorder()
    res = run_rollout("bad", [Stage("canary", 0.1), Stage("full", 1.0)],
                      [threshold_constraint("health", lambda c, f: 0.0, floor=0.5)],
                      deploy=deploy, rollback=rollback)
    assert res.rolled_back and res.reached_fraction == 0.0
    assert [f for _c, f in deployed] == [0.1] and rolled == ["bad"]


def test_a_constraint_that_errors_is_treated_as_failing():
    deployed, rolled, deploy, rollback = _recorder()
    def boom(c, f):
        raise RuntimeError("eval harness down")
    res = run_rollout("x", DEFAULT_STAGES, [boom], deploy=deploy, rollback=rollback)
    assert res.rolled_back and "error" in res.reason
    assert rolled == ["x"]


def test_deploy_error_is_caught_and_rolled_back():
    deployed, rolled = [], []

    def deploy(candidate, fraction):
        deployed.append((candidate, fraction))
        raise RuntimeError("partial deploy")

    def rollback(candidate):
        rolled.append(candidate)
        return True

    res = run_rollout(
        "x", [Stage("canary", 0.1), Stage("full", 1.0)],
        [lambda _c, _f: (True, "ok")],
        deploy=deploy, rollback=rollback,
    )
    assert deployed == [("x", 0.1)] and rolled == ["x"]
    assert res.rolled_back and not res.completed
    assert "deploy failed" in res.reason


def test_rollback_error_is_caught_and_never_claimed():
    def rollback(_candidate):
        raise RuntimeError("restore failed")

    res = run_rollout(
        "x", [Stage("canary", 0.1), Stage("full", 1.0)],
        [lambda _c, _f: (False, "health")],
        deploy=lambda _c, _f: None, rollback=rollback,
    )
    assert not res.rolled_back and not res.completed
    assert "rollback failed (RuntimeError)" in res.reason


def test_unverified_rollback_return_is_never_claimed():
    res = run_rollout(
        "x", [Stage("canary", 0.1), Stage("full", 1.0)],
        [lambda _c, _f: (False, "health")],
        deploy=lambda _c, _f: None, rollback=lambda _candidate: None,
    )
    assert not res.rolled_back and not res.completed
    assert "no verified success receipt" in res.reason


@pytest.mark.parametrize(
    ("stages", "constraints", "reason"),
    [
        ([Stage("canary", 0.1)], [], "health constraint"),
        ([], [lambda _c, _f: (True, "ok")], "rollout stage"),
        (
            [Stage("full", 1.0), Stage("canary", 0.1)],
            [lambda _c, _f: (True, "ok")],
            "strictly increasing",
        ),
        (
            [Stage("canary", float("nan"))],
            [lambda _c, _f: (True, "ok")],
            "finite",
        ),
    ],
)
def test_invalid_rollout_plan_fails_before_deploy(stages, constraints, reason):
    deployed = []
    res = run_rollout(
        "x", stages, constraints,
        deploy=lambda candidate, fraction: deployed.append((candidate, fraction)),
        rollback=lambda _candidate: None,
    )
    assert not res.completed and not res.rolled_back and deployed == []
    assert reason in res.reason


@pytest.mark.parametrize(
    "outcome",
    [(False, ""), (False, None), ("yes", "health")],
)
def test_constraint_must_return_literal_true_or_stage_fails(outcome):
    deployed, rolled, deploy, rollback = _recorder()
    res = run_rollout(
        "x", DEFAULT_STAGES, [lambda _c, _f: outcome],
        deploy=deploy, rollback=rollback,
    )
    assert not res.completed and res.rolled_back
    assert res.stages[0].failing_constraint


def test_promote_skill_live_audits_each_stage_and_completes(monkeypatch):
    # Exercises the previously-untested live wiring: snapshot once, audit a
    # LEARNING_UPDATE per stage (the call that was silently wrong), no rollback.
    from maverick import learning_rollout as lr
    calls = {"snapshot": 0, "audit": []}
    def _snapshot(*args, **kwargs):
        calls["snapshot"] += 1
        assert kwargs == {"publish_empty": True, "raise_on_error": True}
        return Path("snapshots/exact-snapshot")

    monkeypatch.setattr("maverick.dreaming.snapshot_learning_state", _snapshot)
    monkeypatch.setattr("maverick.dreaming.rollback_learning_state",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not roll back")))
    monkeypatch.setattr("maverick.audit.record",
                        lambda kind, **payload: calls["audit"].append((kind, payload)))
    ok = lr.threshold_constraint("h", lambda c, f: 1.0, floor=0.5)
    res = lr.promote_skill_live(
        "sk", [ok],
        stages=[lr.Stage("canary", 0.1), lr.Stage("full", 1.0)],
        deploy_backend=lambda c, f: lr.DeploymentReceipt(c, f, f"rev-{f}"),
        rollback_backend=lambda _c: True,
    )
    from maverick.audit import EventKind
    assert res.completed and calls["snapshot"] == 1
    assert len(calls["audit"]) == 2
    assert all(k == EventKind.LEARNING_UPDATE for k, _ in calls["audit"])
    assert calls["audit"][0][1]["candidate"] == "sk"
    assert calls["audit"][0][1]["deployed_revision"] == "rev-0.1"


def test_promote_skill_live_requires_real_deployment_backends():
    called = False

    def constraint(_candidate, _fraction):
        nonlocal called
        called = True
        return True, "ok"

    from maverick import learning_rollout as lr

    res = lr.promote_skill_live("sk", [constraint])
    assert not res.completed and not res.rolled_back and not called
    assert "backends are required" in res.reason


def test_promote_skill_live_aborts_without_exact_snapshot(monkeypatch):
    from maverick import learning_rollout as lr

    deployed = []
    monkeypatch.setattr(
        "maverick.dreaming.snapshot_learning_state", lambda *a, **k: None,
    )
    res = lr.promote_skill_live(
        "sk", [lambda _c, _f: (True, "ok")],
        deploy_backend=lambda c, f: deployed.append((c, f)),
        rollback_backend=lambda _candidate: True,
    )
    assert not res.completed and not res.rolled_back
    assert deployed == []
    assert "snapshot failed" in res.reason


def test_promote_skill_live_rolls_back_on_failure(monkeypatch):
    from maverick import learning_rollout as lr
    rolled, remote_rolled = [], []

    def restore(snapshot, *args, **kwargs):
        rolled.append(snapshot)
        return ["insights.ndjson"]

    monkeypatch.setattr(
        "maverick.dreaming.snapshot_learning_state",
        lambda *a, **k: Path("snapshots/snapshot-for-this-rollout"),
    )
    monkeypatch.setattr(
        "maverick.dreaming.rollback_learning_state",
        restore,
    )
    monkeypatch.setattr("maverick.audit.record", lambda *a, **k: None)
    bad = lr.threshold_constraint("h", lambda c, f: 0.0, floor=0.5)
    res = lr.promote_skill_live(
        "sk", [bad], stages=[lr.Stage("canary", 0.1), lr.Stage("full", 1.0)],
        deploy_backend=lambda c, f: DeploymentReceipt(c, f, "rev-1"),
        rollback_backend=lambda candidate: not remote_rolled.append(candidate),
    )
    assert res.rolled_back
    assert rolled == ["snapshot-for-this-rollout"]
    assert remote_rolled == ["sk"]


def test_promote_skill_live_never_claims_failed_restore(monkeypatch):
    from maverick import learning_rollout as lr

    monkeypatch.setattr(
        "maverick.dreaming.snapshot_learning_state",
        lambda *a, **k: Path("snapshots/exact-snapshot"),
    )
    monkeypatch.setattr(
        "maverick.dreaming.rollback_learning_state",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk failed")),
    )
    monkeypatch.setattr("maverick.audit.record", lambda *a, **k: None)
    bad = lr.threshold_constraint("h", lambda c, f: 0.0, floor=0.5)
    res = lr.promote_skill_live(
        "sk", [bad], stages=[lr.Stage("canary", 0.1), lr.Stage("full", 1.0)],
        deploy_backend=lambda c, f: DeploymentReceipt(c, f, "rev-1"),
        rollback_backend=lambda _candidate: True,
    )
    assert not res.rolled_back and not res.completed
    assert res.rollback_failures == {"learning_state": "RuntimeError: disk failed"}
    assert "rollback incomplete (learning_state)" in res.reason


def test_live_rollback_attempts_local_restore_after_deployment_failure(monkeypatch):
    from maverick import learning_rollout as lr

    restored = []
    monkeypatch.setattr(
        "maverick.dreaming.snapshot_learning_state",
        lambda *a, **k: Path("snapshots/exact-snapshot"),
    )

    def restore(snapshot):
        restored.append(snapshot)
        return ["insights.ndjson"]

    monkeypatch.setattr("maverick.dreaming.rollback_learning_state", restore)
    monkeypatch.setattr("maverick.audit.record", lambda *a, **k: None)
    bad = lr.threshold_constraint("h", lambda _c, _f: 0.0, floor=0.5)
    result = lr.promote_skill_live(
        "sk", [bad],
        stages=[lr.Stage("canary", 0.1), lr.Stage("full", 1.0)],
        deploy_backend=lambda c, f: DeploymentReceipt(c, f, "rev-1"),
        rollback_backend=lambda _candidate: False,
    )

    assert restored == ["exact-snapshot"]
    assert not result.completed and not result.rolled_back
    assert result.rollback_restored == ["insights.ndjson"]
    assert result.rollback_failures == {
        "deployment": "backend returned no success receipt",
    }


def test_live_rollout_reports_partial_store_restore_and_leaves_no_false_success(
    tmp_path, monkeypatch,
):
    from maverick import dreaming
    from maverick import learning_rollout as lr

    live = tmp_path / "live"
    live.mkdir()
    insights = live / "insights.ndjson"
    skills = live / "learned-skills"
    insights.write_text("trusted\n", encoding="utf-8")
    skills.mkdir()
    (skills / "trusted.md").write_text("trusted", encoding="utf-8")
    stores = {"insights.ndjson": insights, "learned-skills": skills}
    snapdir = tmp_path / "snapshots"

    monkeypatch.setattr(dreaming, "_live_stores", lambda: stores)
    monkeypatch.setattr(dreaming, "snapshots_dir", lambda: snapdir)
    original_restore = dreaming._restore_private_directory

    def fail_skill_restore(src, destination):
        if destination == skills:
            raise OSError("simulated learned-skills restore failure")
        return original_restore(src, destination)

    monkeypatch.setattr(dreaming, "_restore_private_directory", fail_skill_restore)
    monkeypatch.setattr("maverick.audit.record", lambda *a, **k: None)

    def deploy(_candidate, fraction):
        insights.write_text(f"poison-{fraction}\n", encoding="utf-8")
        (skills / "poison.md").write_text("poison", encoding="utf-8")
        return DeploymentReceipt("sk", fraction, f"rev-{fraction}")

    bad = lr.threshold_constraint("h", lambda _c, _f: 0.0, floor=0.5)
    result = lr.promote_skill_live(
        "sk", [bad],
        stages=[lr.Stage("canary", 0.1), lr.Stage("full", 1.0)],
        deploy_backend=deploy,
        rollback_backend=lambda _candidate: True,
    )

    assert not result.completed and not result.rolled_back
    assert result.rollback_restored == ["insights.ndjson"]
    assert "simulated learned-skills restore failure" in (
        result.rollback_failures["learned-skills"]
    )
    assert "rollback incomplete (learned-skills)" in result.reason
    assert insights.read_text(encoding="utf-8") == "trusted\n"
    assert (skills / "poison.md").read_text(encoding="utf-8") == "poison"
