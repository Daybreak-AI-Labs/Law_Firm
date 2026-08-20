"""Matter/owner isolation and offline-only boundaries for local DGM."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from maverick import (
    self_harness as harness,
)
from maverick import (
    self_improvement_runner as runner,
)
from maverick.matter_context import (
    GOAL_EXECUTION_PURPOSE,
    MatterContext,
    matter_context_scope,
)

MATTER_ID = 71
OWNER = "user:alice"
TEST_KEY = "33" * 32


def _secure_learning(monkeypatch) -> None:
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPT_PER_TENANT", "0")
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", TEST_KEY)


def _matter_context(
    matter_id: int = MATTER_ID, principal: str = OWNER,
    purpose: str = GOAL_EXECUTION_PURPOSE,
) -> MatterContext:
    return MatterContext(
        matter_id=matter_id,
        client_id=7,
        principal=principal,
        membership_role="attorney",
        domain="legal",
        jurisdiction="Tennessee",
        purpose=purpose,
        source="test",
    )


def _addendum(line: str) -> str:
    return f"Operating guidance learned for this model:\n- {line}"


def _records() -> list[dict]:
    return [
        {
            "ts": float(index),
            "goal_text": f"export ledger report {index}",
            "failure_class": "timeout",
            "failure_msg": "deadline",
            "reflection": "bound the export window",
            "model_id": "M",
            "matter_id": matter,
            "owner": owner,
        }
        for index, (matter, owner) in enumerate(
            (
                (MATTER_ID, OWNER),
                (MATTER_ID, OWNER),
                (MATTER_ID + 1, OWNER),
                (MATTER_ID, "user:bob"),
                (None, None),
            ),
            1,
        )
    ]


def _runner_settings() -> dict:
    return {
        "_config_valid": True,
        "risk_limited": False,
        "min_support": 1,
        "require_held_out": False,
        "min_delta": 0.0,
        "min_held_out": 0,
        "confidence_z": 0.0,
        "max_cost_factor": None,
        "max_latency_factor": None,
        "max_tool_calls_factor": None,
        "min_support_by_class": {},
        "candidates_per_signature": 1,
        "max_promotions_per_cycle": 0,
        "semantic_mining": False,
        "mine_bucket_by": (),
        "holdout_rotations": 1,
        "promote_as_canary": False,
    }


def test_scope_reflexions_denies_other_matter_owner_and_legacy_rows():
    scope = runner._learning_scope(MATTER_ID, OWNER)
    assert scope is not None

    selected = runner._scope_reflexions(_records(), scope=scope)

    assert len(selected) == 2
    assert {row["matter_id"] for row in selected} == {MATTER_ID}
    assert {row["owner"] for row in selected} == {OWNER}


def test_runner_refuses_every_reflexion_pass_without_exact_scope(monkeypatch):
    monkeypatch.setattr(harness, "enabled", lambda: True)
    monkeypatch.setattr(
        harness, "settings",
        lambda: (_ for _ in ()).throw(AssertionError("settings read")),
    )
    monkeypatch.setattr(
        harness, "run_self_harness",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("DGM ran")),
    )

    report = runner.run_self_harness_pass(_records(), model_id="M")

    assert report.promoted == 0
    assert report.skipped == [
        "reflexion processing requires exact matter and owner scope"
    ]


def test_runner_filters_exact_scope_and_never_applies_runtime(monkeypatch):
    captured: dict = {}

    def fake_run(records, **kwargs):
        captured["records"] = list(records)
        captured["kwargs"] = kwargs
        return harness.SelfHarnessReport(model_id=kwargs["model_id"])

    monkeypatch.setattr(harness, "enabled", lambda: True)
    monkeypatch.setattr(harness, "settings", _runner_settings)
    monkeypatch.setattr(runner, "_risk_calibration_boundary", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(harness, "run_self_harness", fake_run)

    runner.run_self_harness_pass(
        _records(), model_id="M", project_id=MATTER_ID, owner=OWNER,
    )

    assert len(captured["records"]) == 2
    assert all(row["matter_id"] == MATTER_ID for row in captured["records"])
    assert all(row["owner"] == OWNER for row in captured["records"])
    assert captured["kwargs"]["project_id"] == MATTER_ID
    assert captured["kwargs"]["owner"] == OWNER
    assert captured["kwargs"]["apply_promotions"] is False


def test_direct_harness_is_offline_by_default_and_apply_needs_scope_and_approval(
    monkeypatch,
):
    gated: list[dict] = []

    def fake_gate(*_args, **kwargs):
        gated.append(kwargs)
        return True, "promoted"

    monkeypatch.setattr(harness, "enabled", lambda: True)
    monkeypatch.setattr(harness, "_governance_readiness", lambda: (False, True))
    monkeypatch.setattr(harness, "_gate_and_apply", fake_gate)
    records = _records()[:2]
    common = {
        "model_id": "M",
        "min_support": 1,
        "held_in": ["development"],
        "held_out": ["confirmation"],
        "score_with": lambda _line, _cases: 0.9,
        "score_without": lambda _line, _cases: 0.4,
    }

    offline = harness.run_self_harness(
        records, project_id=MATTER_ID, owner=OWNER, **common,
    )
    missing_scope = harness.run_self_harness(
        records, apply_promotions=True, promotion_authorize=lambda: True,
        **common,
    )
    missing_approval = harness.run_self_harness(
        records, apply_promotions=True, project_id=MATTER_ID, owner=OWNER,
        **common,
    )
    approved = harness.run_self_harness(
        records, apply_promotions=True, project_id=MATTER_ID, owner=OWNER,
        promotion_authorize=lambda: True, **common,
    )

    assert offline.validated == 1 and offline.promoted == 0
    assert missing_scope.mined == 0 and missing_scope.validated == 0
    assert missing_approval.validated == 1 and missing_approval.promoted == 0
    assert approved.promoted == 1
    assert len(gated) == 1
    assert gated[0]["matter_id"] == MATTER_ID
    assert gated[0]["owner_scope"] == hashlib.sha256(
        OWNER.encode("utf-8")
    ).hexdigest()[:16]


def test_direct_harness_filters_cross_matter_owner_and_legacy_rows(monkeypatch):
    captured: list[dict] = []

    monkeypatch.setattr(harness, "enabled", lambda: True)
    monkeypatch.setattr(harness, "_governance_readiness", lambda: (False, True))
    monkeypatch.setattr(
        harness,
        "mine_failures",
        lambda records, **_kwargs: captured.extend(records) or [],
    )

    harness.run_self_harness(
        _records(), model_id="M", project_id=MATTER_ID, owner=OWNER,
    )

    assert len(captured) == 2
    assert {row["matter_id"] for row in captured} == {MATTER_ID}
    assert {row["owner"] for row in captured} == {OWNER}


def test_secure_addendum_recall_requires_context_and_denies_other_scopes(
    monkeypatch, tmp_path,
):
    _secure_learning(monkeypatch)
    monkeypatch.setattr(harness, "enabled", lambda: True)
    store = tmp_path / "addenda.json"
    alice_scope = hashlib.sha256(OWNER.encode()).hexdigest()[:16]
    bob_scope = hashlib.sha256(b"user:bob").hexdigest()[:16]
    exact = harness._matter_scoped_key(
        "M", matter_id=MATTER_ID, owner_scope=alice_scope,
    )
    exact_domain = harness._matter_scoped_key(
        "M", matter_id=MATTER_ID, owner_scope=alice_scope,
        context="domain=legal",
    )
    other_matter = harness._matter_scoped_key(
        "M", matter_id=MATTER_ID + 1, owner_scope=alice_scope,
    )
    other_owner = harness._matter_scoped_key(
        "M", matter_id=MATTER_ID, owner_scope=bob_scope,
    )
    harness._write_addenda(
        {
            "M": _addendum("legacy global secret"),
            exact: _addendum("alice matter guidance"),
            exact_domain: _addendum("alice legal guidance"),
            other_matter: _addendum("other matter secret"),
            other_owner: _addendum("bob matter secret"),
        },
        store,
    )

    assert harness.recall_addendum("M", store, domain="legal") == ""
    with matter_context_scope(_matter_context(purpose="inspection")):
        assert harness.recall_addendum("M", store, domain="legal") == ""
    with matter_context_scope(_matter_context()):
        recalled = harness.recall_addendum("M", store, domain="legal")

    assert "alice matter guidance" in recalled
    assert "alice legal guidance" in recalled
    assert "legacy global secret" not in recalled
    assert "other matter secret" not in recalled
    assert "bob matter secret" not in recalled


def test_secure_recall_and_outcome_counters_touch_only_bound_scope(
    monkeypatch, tmp_path,
):
    _secure_learning(monkeypatch)
    monkeypatch.setattr(harness, "enabled", lambda: True)
    store = tmp_path / "addenda.json"
    alice_scope = hashlib.sha256(OWNER.encode()).hexdigest()[:16]
    bob_scope = hashlib.sha256(b"user:bob").hexdigest()[:16]
    exact = harness._matter_scoped_key(
        "M", matter_id=MATTER_ID, owner_scope=alice_scope,
    )
    exact_domain = harness._matter_scoped_key(
        "M", matter_id=MATTER_ID, owner_scope=alice_scope,
        context="domain=legal",
    )
    other_matter = harness._matter_scoped_key(
        "M", matter_id=MATTER_ID + 1, owner_scope=alice_scope,
    )
    other_owner = harness._matter_scoped_key(
        "M", matter_id=MATTER_ID, owner_scope=bob_scope,
    )
    keys = ["M", exact, exact_domain, other_matter, other_owner]
    shared_line = "verify the client instruction"
    harness._write_addenda(
        {key: _addendum(shared_line) for key in keys}, store,
    )
    meta: dict[str, dict] = {}
    for key in keys:
        harness._upsert_line_meta(
            meta, key, shared_line, {"matter_id": MATTER_ID}, now=1.0,
        )
    harness._write_line_meta(meta, store)

    # Missing context cannot mutate usage or efficacy evidence.
    harness.note_recall("M", path=store, min_interval_s=0, now=2.0)
    harness.note_outcome("M", True, line=shared_line, path=store)
    before = harness.load_line_meta(store)
    assert all(not row.get("recall_notes") for row in before.values())
    assert all(not row.get("recall_success") for row in before.values())

    with matter_context_scope(_matter_context()):
        harness.note_recall(
            "M", path=store, min_interval_s=0, now=3.0, domain="legal",
        )
        harness.note_outcome(
            "M", True, line=shared_line, path=store, domain="legal",
        )

    after = harness.load_line_meta(store)
    exact_ids = {
        harness._line_id(exact, shared_line),
        harness._line_id(exact_domain, shared_line),
    }
    for key in keys:
        row = after[harness._line_id(key, shared_line)]
        if harness._line_id(key, shared_line) in exact_ids:
            assert row["recall_notes"] == 1
            assert row["recall_success"] == 1
        else:
            assert not row.get("recall_notes")
            assert not row.get("recall_success")


def test_secure_promotion_writes_only_exact_matter_owner_key(
    monkeypatch, tmp_path,
):
    from maverick import self_improvement as improvement

    _secure_learning(monkeypatch)
    monkeypatch.setattr(improvement, "enabled", lambda: True)
    monkeypatch.setattr(harness, "check_learning_halt", lambda *_a, **_k: None)
    preparation = SimpleNamespace(
        ok=True, blocking_reason=None, committed=False, needs_apply=True,
    )
    captured: dict = {}

    def prepare(candidate, *_args, **_kwargs):
        captured["candidate"] = candidate
        return preparation

    monkeypatch.setattr(
        improvement, "prepare_promotion", prepare,
    )
    monkeypatch.setattr(
        improvement,
        "authorize_prepared",
        lambda *_a, **_k: SimpleNamespace(ok=True, blocking_reason=None),
    )
    monkeypatch.setattr(
        improvement,
        "commit_prepared",
        lambda *_a, **_k: SimpleNamespace(ok=True, blocking_reason=None),
    )

    class Controller:
        def recover_promotions(self, *_args, **_kwargs):
            return []

    proposal = harness.HarnessProposal(
        model_id="M",
        signature="timeout",
        addendum_line="verify the filing window",
        rationale="repeated deadline misses",
        hypothesis="the filing window was not checked before drafting",
    )
    validation = harness.ValidationResult(
        True, 0.4, 0.4, "validated",
        baseline_score=0.4, candidate_score=0.8, samples=8,
        held_in_samples=8, held_out_samples=8, effect_ci_low=0.1,
    )
    store = tmp_path / "addenda.json"
    owner_scope = hashlib.sha256(OWNER.encode()).hexdigest()[:16]

    ok, reason = harness._gate_and_apply(
        proposal,
        validation,
        controller=Controller(),
        path=store,
        promotion_authorize=lambda: True,
        matter_id=MATTER_ID,
        owner_scope=owner_scope,
    )

    expected = harness._matter_scoped_key(
        "M", matter_id=MATTER_ID, owner_scope=owner_scope,
    )
    assert ok is True and reason == "promoted"
    assert set(harness.load_addenda(store)) == {expected}
    assert "M" not in harness.load_addenda(store)

    candidate = captured["candidate"]
    receipt = json.dumps({
        "summary": candidate.summary,
        "payload": candidate.payload,
        "provenance": candidate.provenance,
        "audit_payload": candidate.audit_payload,
    }, sort_keys=True, ensure_ascii=False)
    for raw in (
        proposal.addendum_line,
        proposal.signature,
        proposal.rationale,
        proposal.hypothesis,
    ):
        assert raw not in receipt
    assert candidate.payload["matter_id"] == MATTER_ID
    assert candidate.payload["owner_scope"] == owner_scope
    assert candidate.payload["addendum_bytes"] == len(
        proposal.addendum_line.encode("utf-8")
    )
    assert candidate.payload["addendum_sha256"] == hashlib.sha256(
        proposal.addendum_line.encode("utf-8")
    ).hexdigest()
    assert "artifact_identity" not in candidate.payload


def test_explicitly_insecure_mode_keeps_legacy_global_recall(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_SECURE_DEFAULT", "0")
    monkeypatch.setattr(harness, "enabled", lambda: True)
    store = tmp_path / "addenda.json"
    harness._write_addenda({"M": _addendum("legacy local guidance")}, store)

    assert "legacy local guidance" in harness.recall_addendum("M", store)
    assert harness._promotion_store_key(
        "M", "", matter_id=MATTER_ID,
        owner_scope=hashlib.sha256(OWNER.encode()).hexdigest()[:16],
    ) == "M"


def test_corpus_harvest_never_reads_without_scope(monkeypatch, tmp_path):
    from maverick import reflexion

    class TrapWorld:
        def list_goals(self, **_kwargs):
            raise AssertionError("world goals read")

    monkeypatch.setattr(harness, "settings", dict)
    monkeypatch.setattr(
        reflexion, "list_recent",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("reflexion log read")),
    )

    assert runner.run_corpus_harvest(
        TrapWorld(), mode="auto", key="M",
        corpus_path=str(tmp_path / "eval.json"),
    ) == 0


def test_corpus_harvest_filters_scope_and_only_stages(monkeypatch, tmp_path):
    from maverick import reflexion
    from maverick import self_harness_eval as evaluation

    captured: dict = {}

    class ScopedWorld:
        def list_goals(self, **kwargs):
            captured["world_query"] = kwargs
            return [
                SimpleNamespace(
                    title="exact", description="", result="done",
                    created_at=2.0, updated_at=2.0,
                    project_id=MATTER_ID, owner=OWNER,
                ),
                SimpleNamespace(
                    title="wrong", description="", result="done",
                    created_at=2.0, updated_at=2.0,
                    project_id=MATTER_ID + 1, owner=OWNER,
                ),
            ]

    monkeypatch.setattr(harness, "settings", dict)
    monkeypatch.setattr(reflexion, "list_recent", lambda **_kwargs: _records())
    monkeypatch.setattr(evaluation, "load_eval_corpus", lambda _path: {})
    monkeypatch.setattr(evaluation, "load_pending", lambda _path: {})
    monkeypatch.setattr(evaluation, "load_rejected", lambda _path: {})

    def harvest(records, goals, *, known):
        captured["records"] = list(records)
        captured["goals"] = list(goals)
        captured["known"] = known
        return [{"goal": "exact", "expected": "done"}]

    def stage(path, key, candidates):
        captured["path"] = Path(path)
        captured["key"] = key
        captured["candidates"] = candidates
        return len(candidates)

    monkeypatch.setattr(evaluation, "harvest_corpus_candidates", harvest)
    monkeypatch.setattr(evaluation, "stage_candidates", stage)
    monkeypatch.setattr(
        evaluation, "merge_candidates",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("auto merge")),
    )

    added = runner.run_corpus_harvest(
        ScopedWorld(), mode="auto", key="M",
        corpus_path=str(tmp_path / "eval.json"),
        project_id=MATTER_ID, owner=OWNER, raise_errors=True,
    )

    owner_scope = hashlib.sha256(OWNER.encode("utf-8")).hexdigest()[:16]
    assert added == 1
    assert len(captured["records"]) == 2
    assert [goal.title for goal in captured["goals"]] == ["exact"]
    assert captured["world_query"]["project_id"] == MATTER_ID
    assert captured["world_query"]["owner"] == OWNER
    assert captured["path"] == (
        tmp_path / "matters" / f"matter-{MATTER_ID}"
        / f"owner-{owner_scope}" / "eval.json"
    )
    assert captured["candidates"] == [{"goal": "exact", "expected": "done"}]


def test_fleet_transfer_is_absent():
    assert not hasattr(harness, "run_transfer")
    assert not hasattr(runner, "run_self_harness_transfer")
    assert not hasattr(runner, "run_self_harness_transfer_sweep")
    assert not hasattr(runner, "run_self_harness_all_models")
