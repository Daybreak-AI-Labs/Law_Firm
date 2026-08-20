"""Offline, matter-scoped self-harness and verifier-evidence coordination.

This module ties capture -> evidence for the retained firm learning loop:

* **judgment** -- ``build_prm_examples`` turns captured trajectories into
  training rows for the small reward *head* (an MLP over the frontier model's
  outputs -- NOT an LLM, so no open-weights model is implied);
* **calibration** -- ``collect_calibration`` feeds the verifier-drift interlock
  from trusted ground truth so the freeze is always armed; and
* **prompt harness** -- exact-matter candidate generation/evaluation with
  explicit operator-only promotion through the governed transaction ledger.

Generic synthesized-tool, routing-policy, code, and weight producers are not
part of the firm product.
"""
from __future__ import annotations

import logging
import math
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from .learning_guard import Halted

log = logging.getLogger(__name__)


def _learning_scope(
    project_id: int | None, owner: str | None,
) -> tuple[int, str, str] | None:
    """Return exact matter, owner, and opaque owner key; fail closed."""
    from .skill.distillation_local import scoped_store

    store = scoped_store(None, project_id=project_id, owner=owner)
    if store is None or owner is None:
        return None
    try:
        matter_id = int(store.parent.name.removeprefix("matter-"))
    except (TypeError, ValueError):
        return None
    owner_scope = store.name.removeprefix("owner-")
    if (
        matter_id <= 0 or len(owner_scope) != 16
        or any(ch not in "0123456789abcdef" for ch in owner_scope)
    ):
        return None
    return matter_id, str(owner), owner_scope


def _scoped_corpus_path(
    corpus_path: str | Path, *, scope: tuple[int, str, str],
) -> str:
    """Place every raw/eval corpus in one matter + principal namespace."""
    matter_id, _owner, owner_scope = scope
    path = Path(corpus_path)
    matter_dir = f"matter-{matter_id}"
    owner_dir = f"owner-{owner_scope}"
    if (
        path.parent.name == owner_dir
        and path.parent.parent.name == matter_dir
        and path.parent.parent.parent.name == "matters"
    ):
        return str(path)
    return str(path.parent / "matters" / matter_dir / owner_dir / path.name)


def _scope_reflexions(
    reflexions, *, scope: tuple[int, str, str],
) -> list[dict]:
    """Select only records stamped with this exact matter and owner."""
    matter_id, owner, _owner_scope = scope
    out: list[dict] = []
    for raw in reflexions or []:
        record = raw.to_dict() if hasattr(raw, "to_dict") else raw
        if not isinstance(record, Mapping):
            continue
        raw_matter = record.get("matter_id")
        if isinstance(raw_matter, bool):
            continue
        try:
            record_matter = int(raw_matter)
        except (TypeError, ValueError):
            continue
        if record_matter != matter_id or record.get("owner") != owner:
            continue
        out.append(dict(record))
    return out


def _learning_provider_egress_enabled() -> bool:
    """Fail-closed authority check for auxiliary self-harness model calls."""
    try:
        from .self_learning import provider_egress_enabled
        return bool(provider_egress_enabled())
    except Exception:  # pragma: no cover -- uncertainty cannot authorize egress
        return False


# -- calibration: arm the verifier-drift interlock from any ground truth -----

def collect_calibration(confidence: float, correct: bool, *, source: str = "auto",
                        evaluator_id: str = "",
                        enabled_fn: Callable[[], bool] | None = None) -> bool:
    """Record a ``(verifier_confidence, ground_truth)`` sample when collection is on.

    Ground truth is anything trustworthy: a coding-mode test outcome, a human
    approval/denial, a hindsight regression. Feeding these keeps
    ``calibration.learning_frozen`` meaningful -- the gate that refuses to let
    the system learn from a drifting judge. No-op (returns False) when off.
    """
    try:
        from . import calibration
        on = enabled_fn() if enabled_fn is not None else calibration.collect_from_coding_enabled()
        if not on:
            return False
        return bool(calibration.record_sample(
            float(confidence), bool(correct), source=source,
            evaluator_id=evaluator_id))
    except Exception:  # pragma: no cover -- never block a run on calibration capture
        log.debug("calibration capture failed", exc_info=True)
        return False


# -- judgment: a training set for the small reward head (Phase 1) ------------

def build_prm_examples(  # noqa: C901 - trust validation stays at the corpus boundary
    store, *, limit: int = 10_000,
    trusted_labels: Mapping[object, Mapping[str, object]] | None = None,
    trusted_outcome_sources: frozenset[str] | None = None,
) -> list[dict]:
    """Build outcome-supervised PRM rows without recycling PRM predictions.

    A captured step's ``promise`` and ``progress`` fields are predictions made by
    the currently active PRM.  They are features of the historical decision, not
    labels, and are deliberately ignored here.  An episode is emitted only when
    it has either:

    * a delayed terminal ``outcome`` whose ``outcome_source`` is explicitly
      trusted and whose ``outcome_verified_at`` is no earlier than the episode;
      or
    * a caller-supplied ``trusted_labels`` entry.  Passing that mapping is an
      explicit trust boundary: the caller is responsible for authenticating its
      provenance. Entries are keyed by ``(goal_id, episode_id)`` or
      ``(task_id, episode_id)`` and contain ``outcome``, ``source``, and
      ``verified_at``. Requiring an episode key prevents one task-level label
      from being copied across independent attempts.

    The terminal outcome is a Monte-Carlo promise/value target for every step in
    the episode. ``progress`` remains unlabeled unless the trusted entry supplies
    it; inventing a progress target from verifier confidence would recreate the
    circular training bug.  Identity and event/label time are retained so the
    verifier head can perform a leakage-resistant group/temporal split.
    """
    try:
        from .prm import StepContext, step_features
    except Exception:  # pragma: no cover
        return []

    # Stored source names are ordinary trajectory metadata, not attestations.
    # Trust none by default; a caller that has authenticated a native producer
    # must opt into its exact source, or supply independently authenticated
    # ``trusted_labels``. This prevents an agent-written ``source='tests'``
    # string from becoming its own reward label.
    if trusted_outcome_sources is None:
        trusted_outcome_sources = frozenset()
    allowed_sources = {str(v).strip().lower() for v in trusted_outcome_sources if str(v).strip()}
    explicit = trusted_labels or {}

    def _finite(value) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _bounded(value) -> float | None:
        number = _finite(value)
        return number if number is not None and 0.0 <= number <= 1.0 else None

    try:
        captured = list(store.iter_steps(limit=limit))
    except Exception:  # pragma: no cover -- an unavailable store is no corpus
        return []

    episodes: dict[tuple[int, int], list] = {}
    for step in captured:
        try:
            key = (int(step.goal_id or 0), int(step.episode_id or 0))
        except (TypeError, ValueError):
            continue
        episodes.setdefault(key, []).append(step)

    rows: list[dict] = []
    for (goal_id, episode_id), episode in episodes.items():
        ordered = sorted(
            episode,
            key=lambda s: (_finite(getattr(s, "ts", None)) or 0.0,
                           int(getattr(s, "step", 0) or 0)),
        )
        episode_end = max((_finite(getattr(s, "ts", None)) or 0.0 for s in ordered), default=0.0)
        task_ids = {
            str(getattr(s, "task_id", "") or "").strip()
            for s in ordered if str(getattr(s, "task_id", "") or "").strip()
        }
        # Never let one task's terminal outcome label another task's steps just
        # because both were recorded under the same episode key.
        if len(task_ids) > 1:
            continue
        task_id = next(iter(task_ids), str(goal_id))

        # An injected label mapping is explicit trusted input.  Native stored
        # outcomes require both independent-source provenance and delayed time.
        matching_labels = [explicit.get(key) for key in (
            (goal_id, episode_id), (task_id, episode_id))
            if isinstance(explicit.get(key), Mapping)]
        if len(matching_labels) > 1 and dict(matching_labels[0]) != dict(matching_labels[1]):
            continue
        label = matching_labels[0] if matching_labels else None
        label_kind = "explicit_trusted" if label is not None else "verified_outcome"

        if label is None:
            candidates = []
            for s in ordered:
                outcome = _bounded(getattr(s, "outcome", None))
                source = str(getattr(s, "outcome_source", "") or "").strip().lower()
                verified_at = _finite(getattr(s, "outcome_verified_at", None))
                if (bool(getattr(s, "is_final", False))
                        and outcome is not None and source in allowed_sources
                        and verified_at is not None and verified_at >= episode_end):
                    candidates.append((verified_at, outcome, source))
            if not candidates:
                continue
            verified_at, outcome, source = max(candidates, key=lambda item: item[0])
            promise_label = outcome
            progress_label = None
        else:
            outcome = _bounded(label.get("outcome"))
            source = str(label.get("source") or "").strip()
            verified_at = _finite(label.get("verified_at"))
            if (outcome is None or not source or verified_at is None
                    or verified_at < episode_end):
                continue
            promise_label = _bounded(label.get("promise"))
            if promise_label is None:
                promise_label = outcome
            progress_label = _bounded(label.get("progress"))

        outcome_id = f"{task_id}:{episode_id}:{source}:{verified_at:.6f}"
        for s in ordered:
            event_ts = _finite(getattr(s, "ts", None))
            if event_ts is None:
                continue
            try:
                ctx = StepContext(
                    goal_id=goal_id, step_index=int(s.step or 0), role=s.role or "other",
                    tool_name=(s.tool or None), tool_succeeded=s.tool_succeeded,
                    is_final=bool(s.is_final), error=(s.error or None), prior_step_score=0.5,
                )
                rows.append({
                    "features": step_features(ctx),
                    "promise": promise_label,
                    "progress": progress_label,
                    "goal_id": goal_id,
                    "task_id": task_id,
                    "episode_id": episode_id,
                    "step": int(s.step or 0),
                    "ts": event_ts,
                    "outcome": outcome,
                    "outcome_id": outcome_id,
                    "outcome_source": source,
                    "outcome_verified_at": verified_at,
                    "label_kind": label_kind,
                })
            except Exception:  # pragma: no cover -- skip malformed steps only
                continue
    # Equalize task influence. A verbose/model-controlled trajectory may expose
    # useful within-task process features, but it cannot receive more aggregate
    # training weight or promotion evidence than a concise task.
    per_task: dict[str, int] = {}
    for row in rows:
        key = str(row["task_id"])
        per_task[key] = per_task.get(key, 0) + 1
    for row in rows:
        row["weight"] = 1.0 / per_task[str(row["task_id"])]
    return rows


# -- self-harness: learn a model-specific harness addendum (Phase 4 sibling) --

def _risk_calibration_boundary(
    settings: Mapping[str, object], *, scoring: bool,
    evaluator_id: str | None,
) -> tuple[str | None, Callable[[], bool] | None]:
    """Preflight calibration and build the final post-eval receipt check."""
    if not _fresh_calibration_receipt(settings):
        return ("risk-limited promotion requires a fresh adequate calibration "
                "receipt", None)
    if not bool(settings.get("risk_limited")) or not scoring:
        return None, None
    bound_id = str(evaluator_id or "").strip()
    if not bound_id:
        return "risk-limited promotion requires an exact evaluator identity", None
    evidence_since = time.time()

    def _authorize_promotion() -> bool:
        return _refresh_bound_calibration_receipt(
            settings, evaluator_id=bound_id, evidence_since=evidence_since)

    return None, _authorize_promotion


def run_self_harness_pass(  # noqa: C901 - fail-closed orchestration boundary
    reflexions=None, *, model_id: str | None = None,
    project_id: int | None = None, owner: str | None = None,
    held_in=None, held_out=None,
    score_with=None, score_without=None, propose_fn=None, controller=None,
    min_support: int | None = None, limit: int = 500,
    require_held_out: bool | None = None, min_delta: float | None = None,
    min_held_out: int | None = None, candidates_per_signature: int | None = None,
    semantic_mining: bool | None = None, similarity_fn=None,
    bucket_by: tuple[str, ...] | None = None,
    metamorphic_fn=None, metamorphic_tolerance: float | None = None,
    holdout_rotations: int | None = None,
    canary: bool | None = None,
    holdout_authorize=None,
    eval_for_context=None,
    calibration_evaluator_id: str | None = None,
):
    """The automatic entry point for the self-harness loop (mine -> propose ->
    validate -> gate), to be called by a scheduler / the self-improvement loop.

    Resolves ``model_id`` to the configured orchestrator model and loads recent
    model-tagged reflexions only when an exact matter and owner are supplied.
    Matter-scoped passes mine/propose/evaluate offline but never mutate the
    runtime addendum store. The tuning knobs
    (``min_support``/``require_held_out``/``min_delta``/``min_held_out``/
    ``candidates_per_signature``/``semantic_mining``/``bucket_by``/``canary``)
    default to ``None`` and are then filled from ``[self_harness]`` config, so an
    operator's settings take effect automatically; an explicit argument overrides
    config. ``canary`` stages this pass's promotions on probation
    (``promote_as_canary`` in config): still recalled, but graduated/demoted from
    real run outcomes by the cycle's canary review instead of being permanent
    on arrival. An
    embedding-backed ``similarity_fn`` may be injected for true semantic
    clustering; otherwise ``semantic_mining`` selects the deterministic built-in. ``score_with``/``score_without`` are the LIVE held-in/held-out A/B
    over the candidate prompt -- injected by the caller because a real evaluation
    needs a real model; without them the pass is a dry inspection that writes
    nothing. Operational failures return an inert report; :class:`Halted` is
    re-raised so the operator interlock remains distinguishable.
    """
    try:
        from . import self_harness
        if not self_harness.enabled():
            return self_harness.SelfHarnessReport(model_id=str(model_id or ""))
        scope = _learning_scope(project_id, owner)
        if scope is None:
            report = self_harness.SelfHarnessReport(model_id=str(model_id or ""))
            report.skipped.append(
                "reflexion processing requires exact matter and owner scope"
            )
            return report
        st = self_harness.settings()
        if st.get("_config_valid") is not True:
            report = self_harness.SelfHarnessReport(model_id=str(model_id or ""))
            report.skipped.append(
                "self-harness configuration could not be resolved safely")
            return report
        calibration_reason, promotion_authorize = _risk_calibration_boundary(
            st,
            scoring=(score_with is not None or score_without is not None
                     or eval_for_context is not None),
            evaluator_id=calibration_evaluator_id)
        if calibration_reason is not None:
            report = self_harness.SelfHarnessReport(model_id=str(model_id or ""))
            report.skipped.append(calibration_reason)
            return report
        live_evaluation = (
            score_with is not None or score_without is not None
            or eval_for_context is not None)
        if (st.get("risk_limited") and live_evaluation
                and holdout_authorize is None):
            report = self_harness.SelfHarnessReport(model_id=str(model_id or ""))
            report.skipped.append(
                "risk-limited promotion requires sealed holdout query authorization")
            return report
        if (st.get("risk_limited") and live_evaluation
                and metamorphic_fn is None):
            report = self_harness.SelfHarnessReport(model_id=str(model_id or ""))
            report.skipped.append(
                "risk-limited promotion requires a bound metamorphic evaluator")
            return report
        floors = _validation_floors(st)
        if min_support is None:
            min_support = st["min_support"]
        if require_held_out is None:
            require_held_out = st["require_held_out"]
        if min_delta is None:
            min_delta = floors["min_delta"]
        if min_held_out is None:
            min_held_out = floors["min_held_out"]
        if candidates_per_signature is None:
            candidates_per_signature = st["candidates_per_signature"]
        if semantic_mining is None:
            semantic_mining = st["semantic_mining"]
        if bucket_by is None:
            bucket_by = st["mine_bucket_by"]
        if holdout_rotations is None:
            holdout_rotations = st.get("holdout_rotations", 1)
        if canary is None:
            canary = st.get("promote_as_canary", False)
        if metamorphic_tolerance is None:
            metamorphic_tolerance = floors["metamorphic_tolerance"]
        if st.get("risk_limited"):
            # Public Python arguments are not an escape hatch from the risk
            # contract.  Clamp every override that could reduce evidence,
            # increase search multiplicity, bypass canary staging, or tolerate
            # metamorphic regression. Invalid values fall back to the profile.
            min_support = (
                max(st["min_support"], min_support)
                if isinstance(min_support, int) and not isinstance(min_support, bool)
                else st["min_support"])
            min_delta = (
                max(floors["min_delta"], float(min_delta))
                if isinstance(min_delta, (int, float))
                and not isinstance(min_delta, bool) and math.isfinite(float(min_delta))
                else floors["min_delta"])
            min_held_out = (
                max(floors["min_held_out"], min_held_out)
                if isinstance(min_held_out, int)
                and not isinstance(min_held_out, bool)
                else floors["min_held_out"])
            require_held_out = True
            candidates_per_signature = st["candidates_per_signature"]
            holdout_rotations = 1
            canary = True
            metamorphic_tolerance = 0.0
        if not model_id:
            from .llm import model_for_role
            model_id = model_for_role("orchestrator")
        if reflexions is None:
            from . import reflexion
            reflexions = [r.to_dict() for r in reflexion.list_recent(limit=limit)]
        if scope is not None:
            reflexions = _scope_reflexions(reflexions, scope=scope)
        return self_harness.run_self_harness(
            reflexions, model_id=model_id,
            project_id=scope[0], owner=scope[1],
            held_in=held_in, held_out=held_out,
            score_with=score_with, score_without=score_without,
            propose_fn=propose_fn, controller=controller, min_support=min_support,
            require_held_out=require_held_out, min_delta=min_delta,
            min_held_out=min_held_out, confidence_z=floors["confidence_z"],
            max_cost_factor=floors["max_cost_factor"],
            max_latency_factor=floors["max_latency_factor"],
            max_tool_calls_factor=floors["max_tool_calls_factor"],
            min_support_by_class=st["min_support_by_class"],
            candidates_per_signature=candidates_per_signature,
            max_promotions_per_cycle=st.get("max_promotions_per_cycle", 0),
            semantic_mining=semantic_mining, similarity_fn=similarity_fn,
            bucket_by=bucket_by, metamorphic_fn=metamorphic_fn,
            metamorphic_tolerance=metamorphic_tolerance,
            holdout_rotations=holdout_rotations, canary=bool(canary),
            holdout_authorize=holdout_authorize,
            eval_for_context=eval_for_context,
            promotion_authorize=promotion_authorize,
            apply_promotions=False)
    except Halted:
        raise
    except Exception:  # pragma: no cover -- learning never perturbs a run
        log.debug("self-harness pass failed", exc_info=True)
        from . import self_harness
        return self_harness.SelfHarnessReport(model_id=str(model_id or ""))


def _fresh_calibration_receipt(
    settings: Mapping[str, object], *, now: float | None = None,
    evaluator_id: str | None = None, evidence_since: float | None = None,
) -> bool:
    """Require a structurally valid, recent risk-limited calibration receipt.

    The initial cycle check may read the general calibration verdict
    (``evaluator_id=None``).  The final pre-promotion check supplies an exact
    evaluator identity and current-cycle evidence boundary, which selects the
    separate Self-Harness receipt and prevents a stale/different judge verdict
    from authorizing a promotion.
    """
    if not bool(settings.get("risk_limited")):
        return True
    try:
        max_age = float(settings.get("calibration_max_age_hours", 24.0)) * 3600.0
        if not math.isfinite(max_age) or max_age <= 0:
            return False
        from . import calibration
        receipt_path = (calibration._risk_verdict_path()
                        if evaluator_id is not None else None)
        verdict = (calibration._load_verdict(receipt_path)
                   if receipt_path is not None else calibration._load_verdict())
        if not isinstance(verdict, Mapping) or verdict.get("adequate") is not True:
            return False
        stamp = float(verdict.get("ts"))
        sample_n = verdict.get("n")
        n_correct = verdict.get("n_correct")
        n_incorrect = verdict.get("n_incorrect")
        if any(not isinstance(value, int) or isinstance(value, bool)
               for value in (sample_n, n_correct, n_incorrect)):
            return False
        calibration_settings = calibration._settings()
        min_samples = max(
            20, int(calibration_settings.get("min_samples", 20)))
        min_discrimination = max(
            0.15, float(calibration_settings.get("min_discrimination", 0.15)))
        discrimination = float(verdict.get("discrimination"))
        brier = float(verdict.get("brier"))
        natural_n = n_correct + n_incorrect
        if (natural_n < min_samples or n_correct <= 0 or n_incorrect <= 0
                or n_correct + n_incorrect > sample_n
                or not math.isfinite(discrimination)
                or discrimination < min_discrimination or discrimination > 1.0
                or not math.isfinite(brier) or not 0.0 <= brier <= 1.0):
            return False
        current = time.time() if now is None else float(now)
        if (not math.isfinite(current) or not math.isfinite(stamp)
                or not 0.0 <= current - stamp <= max_age):
            return False
        if evaluator_id is None:
            return True
        bound_id = str(evaluator_id or "").strip()
        if (not bound_id
                or verdict.get("schema") != "maverick-calibration-receipt-v2"
                or str(verdict.get("evaluator_id") or "") != bound_id):
            return False
        sample_min = float(verdict.get("sample_min_ts"))
        sample_max = float(verdict.get("sample_max_ts"))
        required_start = (0.0 if evidence_since is None
                          else float(evidence_since))
        return (all(math.isfinite(value)
                    for value in (sample_min, sample_max, required_start))
                and required_start <= sample_min <= sample_max <= stamp
                and float(verdict.get("evidence_since")) == required_start)
    except (OSError, TypeError, ValueError, OverflowError):
        return False


def _refresh_bound_calibration_receipt(
    settings: Mapping[str, object], *, evaluator_id: str,
    evidence_since: float,
) -> bool:
    """Reassess the exact judge after this pass and verify a current watermark."""
    bound_id = str(evaluator_id or "").strip()
    try:
        if not bound_id or not math.isfinite(float(evidence_since)):
            return False
        from . import calibration
        calibration.run_assessment(
            verdict_path=calibration._risk_verdict_path(),
            evaluator_id=bound_id, since=float(evidence_since))
        return _fresh_calibration_receipt(
            settings, evaluator_id=bound_id,
            evidence_since=float(evidence_since))
    except (OSError, TypeError, ValueError, OverflowError):
        return False


def _validation_floors(st: dict) -> dict:
    """The ``[self_harness]`` validation floors in ``validate_proposal``'s
    vocabulary -- the ONE source both the cycle pass and the transfer path
    consume, so a floor added here reaches every promotion gate. A floor read
    from settings anywhere else is a drift bug."""
    return dict(
        min_delta=st["min_delta"], min_held_out=st["min_held_out"],
        confidence_z=st["confidence_z"], max_cost_factor=st["max_cost_factor"],
        max_latency_factor=st["max_latency_factor"],
        max_tool_calls_factor=st["max_tool_calls_factor"],
        metamorphic_tolerance=st.get("metamorphic_tolerance", 0.0))


def _normalize_eval_manifest(raw: object) -> dict[str, list[dict[str, str]]]:
    """Normalize one already-read raw corpus snapshot for evaluation.

    Risk-limited cycles hash the raw manifest and evaluate this derived view of
    the *same object*. Reopening the path after authorization would let an
    atomic file swap authorize corpus A while exposing corpus B to the judge.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for key, rows in raw.items():
        if not isinstance(rows, list):
            continue
        cases = []
        for row in rows:
            if isinstance(row, Mapping) and str(row.get("goal") or "").strip():
                cases.append({
                    "goal": str(row["goal"]),
                    "expected": str(row.get("expected") or ""),
                })
        if cases:
            out[str(key)] = cases
    return out


def _effective_eval_index(raw: object) -> dict[str, str]:
    """Validate the usable corpus and index its unique goal/label semantics.

    Non-risk loading remains tolerant, but a sealed family cannot be ambiguous:
    normalized duplicate goals inside one scope, or any semantic goal reused
    across scopes, fail closed before any query is authorized. Cross-scope reuse
    could otherwise expose a task as development evidence in one context and as
    sealed confirmation evidence in another.
    """
    corpus = _normalize_eval_manifest(raw)
    if not corpus:
        raise ValueError("evaluation corpus has no usable cases")
    index: dict[str, str] = {}
    global_identities: set[str] = set()
    for scope, rows in corpus.items():
        seen: set[str] = set()
        for row in rows:
            goal, expected = row["goal"], row["expected"]
            identity = " ".join(
                unicodedata.normalize("NFKC", goal).split()).casefold()
            if (not identity or "\x00" in goal or "\x00" in expected
                    or len(goal) > 32_768 or len(expected) > 32_768):
                raise ValueError("evaluation corpus contains a malformed case")
            if identity in seen:
                raise ValueError(
                    f"evaluation corpus scope {scope!r} contains duplicate goals")
            seen.add(identity)
            # Reusing one semantic task in two scopes can put it in a
            # development split for one context and a sealed split for another.
            # Reject the alias globally, even when its label agrees.
            if identity in global_identities:
                raise ValueError(
                    "evaluation corpus reuses a normalized goal across scopes")
            global_identities.add(identity)
            index[goal] = expected
    return index


def _effective_eval_family(
    index: Mapping[str, str], exposed_cases: list[str],
) -> dict[str, object]:
    """Canonical identity of the exact sealed set the evaluator will expose.

    Corpus key order, row order, ignored metadata, and unrelated model/domain
    scopes cannot mint a new budget for an unchanged holdout.  Labels remain in
    the identity even though scorers receive goals as their lookup key.
    """
    if (not exposed_cases or len(set(exposed_cases)) != len(exposed_cases)
            or any(not isinstance(case, str) or case not in index
                   for case in exposed_cases)):
        raise ValueError("sealed cases must be distinct goals from the corpus")
    return {
        "schema": "maverick-self-harness-effective-family-v2",
        "cases": [
            {"goal": goal, "expected": index[goal]}
            for goal in sorted(exposed_cases)
        ],
    }


def _auto_evaluator(model_id: str, *, corpus_path: str, held_out_frac: float = 0.3,
                    budget=None, judge_samples: int = 1,
                    judge_unknown: bool = False, system_prefix: str = "",
                    verifier_model: str | None = None,
                    strict_judge: bool = False,
                    calibration_evaluator_id: str | None = None,
                    corpus_manifest: Mapping[str, object] | None = None):
    """Build ``(held_in, held_out, score_with, score_without)`` -- the LIVE A/B --
    from a configured eval corpus + the model's own LLM client, so the driver can
    actually PROMOTE rather than only dry-inspect. The candidate ``model_id``
    GENERATES; the same exact run pin, acting in the verifier role, JUDGES with
    a separate held-out rubric (:func:`model_for_role`, kernel rule 2). ``judge_samples``
    > 1 turns on self-consistency judging (majority vote over diverse framings).
    Returns ``None`` (caller stays dry) when there is no corpus, no cases for the
    model, or no usable held-out split. Never raises."""
    try:
        from . import self_harness_eval as ev
        from .llm import LLM, model_for_role
        corpus = (
            _normalize_eval_manifest(corpus_manifest)
            if corpus_manifest is not None
            else ev.load_eval_corpus(corpus_path)
        )
        cases = ev.corpus_cases(corpus, model_id)
        if not cases:
            return None
        held_in, held_out = ev.corpus_split(cases, held_out_frac=held_out_frac)
        if not held_out:
            return None
        judge_model = verifier_model or model_for_role("verifier")
        score_with, score_without = ev.corpus_ab_scorers(
            cases,
            run_fn=ev.llm_runner(
                LLM(model_id), budget=budget, system_prefix=system_prefix),
            judge_fn=ev.llm_judge(LLM(judge_model), budget=budget,
                                  samples=judge_samples, strict=strict_judge,
                                  evaluator_id=calibration_evaluator_id),
            judge_unknown=judge_unknown, detailed=True)
        # Replicate BOTH arms symmetrically.  Averaging only the baseline while a
        # candidate gets one lucky draw biases promotion; strict replication also
        # makes a mid-draw outage indeterminate instead of silently falling back
        # to whichever arm happened to finish first.
        return (held_in, held_out,
                _memo_scorer(score_with, by_line=True, require_all_draws=True),
                _memo_scorer(score_without, require_all_draws=True))
    except Exception:  # pragma: no cover -- a missing provider/corpus stays dry
        log.debug("self-harness auto-evaluator construction failed", exc_info=True)
        return None


def _domain_of_context(context: str) -> str:
    """The ``domain=<d>`` value inside a mining-context string (e.g.
    ``"domain=finance"`` or ``"domain=finance, tool=web_fetch"``), or ``""``
    when the context carries no domain part."""
    for part in (context or "").split(","):
        k, _, v = part.strip().partition("=")
        if k == "domain" and v:
            return v
    return ""


def _context_evaluator(model_id: str, *, corpus_path: str, budget=None,
                       judge_samples: int = 1, held_out_frac: float = 0.3,
                       judge_unknown: bool = False, system_prefix: str = "",
                       verifier_model: str | None = None,
                       strict_judge: bool = False,
                       calibration_evaluator_id: str | None = None,
                       corpus_manifest: Mapping[str, object] | None = None):
    """Build the ``eval_for_context`` seam from the corpus: a domain-scoped
    signature (``"domain=finance"``) is validated against the corpus's
    ``finance``-keyed cases -- its own department's ground truth -- instead of
    the model-wide pool, which under-credits a narrow line. The corpus loader
    has always documented ``{model|domain: [...]}`` keys; this makes the domain
    keys actually reach validation.

    Returns ``None`` when the corpus has no keys beyond the model's (nothing
    scoped to serve -- no LLM clients are built in that case). Otherwise the
    selected run model generates and then judges under the verifier rubric as in
    :func:`_auto_evaluator`, sharing the same ``budget`` pot; per-domain quads
    are cached so repeated signatures don't rebuild them. A domain with no
    usable held-out split yields ``None`` for that domain (the pass keeps its
    defaults). Never raises."""
    try:
        from . import self_harness_eval as ev
        from .llm import LLM, model_for_role
        corpus = (
            _normalize_eval_manifest(corpus_manifest)
            if corpus_manifest is not None
            else ev.load_eval_corpus(corpus_path)
        )
        domain_keys = {k for k in corpus if k != str(model_id)}
        if not domain_keys:
            return None
        run_fn = ev.llm_runner(
            LLM(model_id), budget=budget, system_prefix=system_prefix)
        judge_model = verifier_model or model_for_role("verifier")
        judge_fn = ev.llm_judge(LLM(judge_model), budget=budget,
                                samples=judge_samples, strict=strict_judge,
                                evaluator_id=calibration_evaluator_id)
        cache: dict[str, tuple | None] = {}

        def _for_context(context: str):
            d = _domain_of_context(context)
            if not d or d not in domain_keys:
                return None
            if d not in cache:
                held_in, held_out = ev.corpus_split(
                    ev.corpus_cases(corpus, d), held_out_frac=held_out_frac)
                if not held_out:
                    cache[d] = None
                else:
                    sw, swo = ev.corpus_ab_scorers(
                        ev.corpus_cases(corpus, d), run_fn=run_fn,
                        judge_fn=judge_fn, judge_unknown=judge_unknown,
                        detailed=True)
                    cache[d] = (
                        held_in, held_out,
                        _memo_scorer(sw, by_line=True, require_all_draws=True),
                        _memo_scorer(swo, require_all_draws=True))
            return cache[d]

        return _for_context
    except Exception:  # pragma: no cover -- a missing provider/corpus stays dry
        log.debug("self-harness context-evaluator construction failed", exc_info=True)
        return None


def _eval_budget(settings: dict):
    """A ``Budget`` capping ONE auto-evaluated cycle's LLM spend, from
    ``[self_harness] eval_budget_dollars`` -- or ``None`` (uncapped, the
    historical behavior) when the knob is unset/<=0. A present malformed cap
    raises so the enclosing governed operation refuses to run rather than
    becoming uncapped. The runner and judge share
    the pot (kernel rule 3: an unattended evaluation must not have unbounded
    spend). Dollars is the BINDING cap: the token/tool ceilings are raised far
    above any eval pass so the operator's dollar figure is what trips (``Budget``
    enforces every cap at record time, so its defaults -- sized for one agent
    run -- would otherwise trip first); the default 1h wall clock stays as a hang
    backstop."""
    if settings.get("eval_budget_valid") is False:
        raise ValueError("self-harness evaluation budget policy is invalid")
    try:
        dollars = settings.get("eval_budget_dollars")
        if not dollars or float(dollars) <= 0:
            return None
        from .budget import Budget
        big = 10**9
        return Budget(max_dollars=float(dollars), max_input_tokens=big,
                      max_output_tokens=big, max_tool_calls=big)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "self-harness evaluation budget policy is invalid") from exc


def _build_holdout_authorizer(
    settings: Mapping[str, object], *, corpus_path: str,
    model_id: str, evaluation_system: str,
    verifier_model: str | None = None,
    metamorphic_model: str | None = None,
    held_out_frac: float = 0.3,
    corpus_manifest: Mapping[str, object] | None = None,
):
    """Bind one cycle to an explicitly provisioned cross-cycle holdout ledger."""
    from . import self_harness_eval as ev
    from .llm import model_for_role
    from .self_harness_holdout import (
        AlphaBudget,
        HoldoutQuery,
        HoldoutQueryLedger,
        fingerprint_manifest,
    )

    ledger_path = str(settings.get("holdout_ledger") or "").strip()
    if not ledger_path:
        raise ValueError("risk-limited holdout ledger is not configured")
    raw_manifest = (
        dict(corpus_manifest)
        if corpus_manifest is not None
        else ev._load_raw(corpus_path)
    )
    if not isinstance(raw_manifest, dict) or not raw_manifest:
        raise ValueError("evaluation corpus manifest is unavailable")
    effective_index = _effective_eval_index(raw_manifest)
    ledger = HoldoutQueryLedger(ledger_path)
    ledger_status = ledger.verify()
    # One provisioned ledger is one statistical study. Every exact or
    # overlapping case family in that study shares the same global query/alpha
    # budget; corpus edits cannot mint a fresh allowance. Starting a new study
    # requires the explicit, auditable act of provisioning a new ledger.
    study_digest = fingerprint_manifest({
        "schema": "maverick-self-harness-ledger-study-v1",
        "ledger_id": ledger_status.ledger_id,
    })
    policy = AlphaBudget(
        family_alpha=float(settings.get("holdout_family_alpha", 0.05)),
        query_alpha=float(settings.get("holdout_query_alpha", 0.025)),
        max_queries=int(settings.get("holdout_max_queries", 2)),
    )
    cycle_id = uuid.uuid4().hex
    bound_verifier = str(verifier_model or model_for_role("verifier")).strip()
    if not bound_verifier:
        raise ValueError("verifier model is unavailable")
    metamorphic_enabled = bool(settings.get("metamorphic"))
    bound_metamorphic = ""
    if metamorphic_enabled:
        bound_metamorphic = str(
            metamorphic_model or model_for_role("summarizer")
        ).strip()
        if not bound_metamorphic:
            raise ValueError("metamorphic model is unavailable")
    bound_evaluator_id = ev.judge_evaluator_identity(
        bound_verifier, samples=int(settings.get("judge_samples", 1)),
        strict=True)
    evaluator_epoch = fingerprint_manifest({
        "schema": "maverick-self-harness-evaluator-v3",
        "candidate_model": str(model_id),
        "verifier_model": bound_verifier,
        "runner_protocol": "llm-runner-v1",
        "judge_protocol": "llm-judge-v2-untrusted-json-opaque-verdict",
        "judge_evaluator_id": bound_evaluator_id,
        "judge_samples": int(settings.get("judge_samples", 1)),
        "judge_unknown": metamorphic_enabled,
        "held_out_frac": float(held_out_frac),
        "metamorphic_enabled": metamorphic_enabled,
        "metamorphic_protocol": (
            "llm-paraphraser-v1" if metamorphic_enabled else ""
        ),
        "metamorphic_model": bound_metamorphic,
        "deployed_system_sha256": fingerprint_manifest(str(evaluation_system)),
    })
    counter = 0

    def _authorize(purpose: str, signature: str, cases: list[str]) -> float:
        nonlocal counter
        family_digest = fingerprint_manifest(
            _effective_eval_family(effective_index, cases))
        counter += 1
        query = HoldoutQuery(
            holdout_sha256=study_digest,
            view_sha256=fingerprint_manifest({
                "purpose": str(purpose),
                "effective_family_sha256": family_digest,
                "cases": sorted(cases),
            }),
            query_id=f"{cycle_id}:{counter}",
            cycle_id=cycle_id,
            signature=str(signature),
            evaluator_epoch=evaluator_epoch,
            purpose=str(purpose),
        )
        return ledger.authorize(query, policy).critical_z_two_sided

    return _authorize


def run_self_harness_cycle(  # noqa: C901 - one fail-closed orchestration boundary
    reflexions=None, *, model_id: str | None = None, limit: int = 500,
    project_id: int | None = None, owner: str | None = None,
    retire: bool = True, retire_after_days: float | None = None,
    held_in=None, held_out=None, score_with=None, score_without=None,
    propose_fn=None, controller=None, evaluation_system: str | None = None,
    **pass_kwargs,
):
    """One full DRIVER cycle -- a governed learning pass THEN stale-line
    retirement -- the unit a scheduler (cron / the self-improvement loop) invokes
    to operate the harness end-to-end. Returns ``(report, retired_count)``.

    The pass is :func:`run_self_harness_pass` (mine -> propose -> validate).
    Client-derived reads require exact matter/owner scope and remain offline:
    they do not apply, graduate, demote, or retire runtime guidance. The live
    A/B (``score_with``/``score_without``) is injected, OR -- when
    the caller injects none, ``[self_harness] eval_corpus`` is configured, and
    learning provider egress is authorized -- AUTO-BUILT by
    :func:`_auto_evaluator` (the model generates, a verifier judges) so a
    scheduled ``maverick self-harness run`` actually PROMOTES. With no scorer,
    corpus, or egress authority it is a DRY inspection that writes no NEW guidance, but
    retirement still runs -- so the cycle keeps guidance fresh either way.
    Retirement uses
    ``retire_after_days`` (arg overrides ``[self_harness]`` config; 0/None = off)
    and is scoped to the pass's resolved model. Operational failures return an
    inert result; :class:`Halted` is re-raised so schedulers cannot report an
    interlocked cycle as successful."""
    try:
        from . import self_harness
        from .llm import require_model_allowed

        scope = _learning_scope(project_id, owner)
        if scope is None:
            report = self_harness.SelfHarnessReport(model_id=str(model_id or ""))
            report.skipped.append(
                "reflexion processing requires exact matter and owner scope"
            )
            return report, 0

        if model_id is not None:
            # A caller-selected candidate model is an explicit policy pin. Keep
            # the original corpus key spelling, but reject it before any
            # evaluator can downgrade the violation into a harmless dry pass.
            require_model_allowed(str(model_id))
        st = self_harness.settings() if self_harness.enabled() else None
        provider_egress = _learning_provider_egress_enabled()
        # ONE budget pot for everything this cycle evaluates -- the model-wide
        # A/B, the domain-scoped quads, and the metamorphic paraphraser
        # together, so the operator's cap bounds the whole cycle, not each
        # evaluator.
        eval_budget = _eval_budget(st) if st is not None else None
        # Resolve evaluator identities once and reuse those exact values for
        # holdout authorization and construction. Re-resolving a role between
        # those steps creates a TOCTOU gap: the ledger can authorize judge A
        # while judge B actually sees the sealed cases.
        bound_verifier_model: str | None = None
        bound_metamorphic_model: str | None = None
        calibration_evaluator_id: str | None = None
        # AUTO-WIRE the live A/B from config so a scheduled cycle can actually
        # PROMOTE, not just dry-inspect: if the caller injected no scorers and
        # [self_harness] eval_corpus is set, build the model-generates /
        # verifier-judges evaluator. Unset corpus (the default) -> stays dry, so
        # behavior is unchanged out of the box.
        if (score_with is None and score_without is None and st is not None
                and provider_egress and scope is not None):
            corpus_path = st.get("eval_corpus")
            if corpus_path:
                corpus_path = _scoped_corpus_path(corpus_path, scope=scope)
                from . import self_harness_eval as eval_module
                from .llm import model_for_role
                model_id = model_id or model_for_role("orchestrator")
                # One immutable-in-memory snapshot feeds both the holdout
                # family digest and every evaluator built below. Never reopen
                # a mutable corpus path after spending authorization for it.
                corpus_manifest = eval_module._load_raw(str(corpus_path))
                try:
                    bound_verifier_model = str(
                        model_for_role("verifier")
                    ).strip() or None
                except Exception:
                    log.debug("self-harness verifier identity resolution failed",
                              exc_info=True)
                if (st.get("metamorphic")
                        and pass_kwargs.get("metamorphic_fn") is None):
                    try:
                        bound_metamorphic_model = str(
                            model_for_role("summarizer")
                        ).strip() or None
                    except Exception:
                        log.debug(
                            "self-harness paraphraser identity resolution failed",
                            exc_info=True)
                # Risk-limited evidence must compare the exact deployed prompt
                # snapshot against that snapshot plus the candidate. A generic
                # helper prompt is not a valid baseline for unattended change.
                if st.get("risk_limited") and not str(evaluation_system or "").strip():
                    report = self_harness.SelfHarnessReport(model_id=str(model_id))
                    report.skipped.append(
                        "risk-limited auto-evaluation requires deployed prompt snapshot")
                    return report, 0
                if st.get("risk_limited") and not bound_verifier_model:
                    report = self_harness.SelfHarnessReport(model_id=str(model_id))
                    report.skipped.append(
                        "risk-limited auto-evaluation requires a bound verifier model")
                    return report, 0
                if st.get("risk_limited"):
                    calibration_evaluator_id = eval_module.judge_evaluator_identity(
                        str(bound_verifier_model),
                        samples=int(st.get("judge_samples", 1)), strict=True)
                    # The exact identity is reused by every evaluator and by the
                    # final post-evaluation calibration receipt. A caller cannot
                    # substitute a receipt for some other judge configuration.
                    pass_kwargs["calibration_evaluator_id"] = (
                        calibration_evaluator_id)
                if (st.get("risk_limited") and st.get("metamorphic")
                        and pass_kwargs.get("metamorphic_fn") is None
                        and not bound_metamorphic_model):
                    report = self_harness.SelfHarnessReport(model_id=str(model_id))
                    report.skipped.append(
                        "risk-limited metamorphic evaluation requires a bound "
                        "paraphraser model")
                    return report, 0
                if (st.get("risk_limited")
                        and pass_kwargs.get("metamorphic_fn") is not None
                        and pass_kwargs.get("holdout_authorize") is None):
                    report = self_harness.SelfHarnessReport(model_id=str(model_id))
                    report.skipped.append(
                        "risk-limited injected metamorphic evaluation requires a "
                        "caller-supplied holdout authorizer bound to that transform")
                    return report, 0
                if st.get("risk_limited") and pass_kwargs.get(
                        "holdout_authorize") is None:
                    try:
                        pass_kwargs["holdout_authorize"] = _build_holdout_authorizer(
                            st, corpus_path=str(corpus_path), model_id=str(model_id),
                            evaluation_system=str(evaluation_system),
                            verifier_model=bound_verifier_model,
                            metamorphic_model=bound_metamorphic_model,
                            corpus_manifest=corpus_manifest)
                    except Exception:
                        report = self_harness.SelfHarnessReport(model_id=str(model_id))
                        report.skipped.append(
                            "risk-limited auto-evaluation requires a valid provisioned "
                            "holdout ledger and remaining query budget")
                        return report, 0
                # With metamorphic validation on, paraphrased (non-corpus)
                # goals must be JUDGEABLE, not indeterminate -- the LLM judge
                # doesn't need the expected hint.
                judge_unknown = bool(st.get("metamorphic"))
                built = _auto_evaluator(model_id, corpus_path=corpus_path,
                                        judge_samples=st.get("judge_samples", 1),
                                        budget=eval_budget,
                                        judge_unknown=judge_unknown,
                                        system_prefix=str(evaluation_system or ""),
                                        verifier_model=bound_verifier_model,
                                        strict_judge=bool(st.get("risk_limited")),
                                        calibration_evaluator_id=(
                                            calibration_evaluator_id),
                                        corpus_manifest=corpus_manifest)
                if built is not None:
                    held_in, held_out, score_with, score_without = built
                # Scoped evaluation: the corpus's domain keys validate matching
                # domain-scoped candidates -- independent of whether the
                # model-wide quad built, so a domain-keyed-only corpus still
                # promotes its departments' lines.
                if pass_kwargs.get("eval_for_context") is None:
                    efc = _context_evaluator(model_id, corpus_path=corpus_path,
                                             judge_samples=st.get("judge_samples", 1),
                                             budget=eval_budget,
                                             judge_unknown=judge_unknown,
                                             system_prefix=str(evaluation_system or ""),
                                             verifier_model=bound_verifier_model,
                                             strict_judge=bool(st.get("risk_limited")),
                                             calibration_evaluator_id=(
                                                 calibration_evaluator_id),
                                             corpus_manifest=corpus_manifest)
                    if efc is not None:
                        pass_kwargs["eval_for_context"] = efc
        if (st is not None and st.get("risk_limited")
                and score_with is not None and score_without is not None
                and held_out and pass_kwargs.get("holdout_authorize") is None):
            report = self_harness.SelfHarnessReport(model_id=str(model_id or ""))
            report.skipped.append(
                "risk-limited evaluation requires sealed holdout query authorization")
            return report, 0
        # Metamorphic validation (opt-in, [self_harness] metamorphic): reject a
        # candidate whose lift does not survive PARAPHRASING the held-out cases
        # (overfit to exact wording). Built only when something will actually
        # validate (a scorer exists) and the caller injected no paraphraser.
        if (st is not None and st.get("metamorphic") and score_with is not None
                and provider_egress
                and pass_kwargs.get("metamorphic_fn") is None):
            try:
                from . import self_harness_eval as ev
                from .llm import LLM, model_for_role
                if not bound_metamorphic_model:
                    bound_metamorphic_model = str(
                        model_for_role("summarizer")
                    ).strip() or None
                if not bound_metamorphic_model:
                    raise ValueError("metamorphic model is unavailable")
                pass_kwargs["metamorphic_fn"] = ev.llm_paraphraser(
                    LLM(bound_metamorphic_model), budget=eval_budget)
            except Exception:
                log.debug("self-harness paraphraser construction failed",
                          exc_info=True)
                if st.get("risk_limited"):
                    report = self_harness.SelfHarnessReport(
                        model_id=str(model_id or ""))
                    report.skipped.append(
                        "risk-limited metamorphic evaluation could not construct "
                        "the authorized paraphraser")
                    return report, 0
        report = run_self_harness_pass(
            reflexions, model_id=model_id, limit=limit,
            project_id=project_id, owner=owner,
            held_in=held_in, held_out=held_out, score_with=score_with,
            score_without=score_without, propose_fn=propose_fn,
            controller=controller, **pass_kwargs)
        resolved = (report.model_id or model_id) or None
        # OUTCOME-DRIVEN LIFECYCLE: graduate/demote canaries from the accumulated
        # recall->outcome counters (safe -- only touches canary-flagged lines), and
        # optionally re-measure each line's live A/B lift to demote dead weight
        # (opt-in: a general corpus can under-credit a narrow line).
        if resolved and self_harness.enabled() and scope is None:
            try:
                res = self_harness.review_canaries(resolved)
                report.graduated = list(res.get("graduated", []))
                report.demoted = list(res.get("demoted", []))
            except Exception:  # pragma: no cover -- lifecycle never perturbs a run
                log.debug("self-harness canary review failed", exc_info=True)
            # Relapse recency guard (opt-in): AFTER the canary review, so a
            # line re-probated now gets adjudicated on the NEXT cycle's
            # evidence rather than instantly demoted in the same pass.
            try:
                share = float((st or {}).get("relapse_failure_share") or 0.0)
                if share > 0:
                    report.relapsed = self_harness.review_relapses(
                        resolved, failure_share=share,
                        min_outcomes=int((st or {}).get("relapse_min_outcomes", 5)))
            except Exception:  # pragma: no cover -- lifecycle never perturbs a run
                log.debug("self-harness relapse review failed", exc_info=True)
            if ((st or {}).get("risk_limited") and (st or {}).get("efficacy_review")):
                report.skipped.append(
                    "risk-limited efficacy review requires a separately budgeted live set")
            elif (self_harness.settings().get("efficacy_review")
                    and score_with is not None and score_without is not None):
                try:
                    cases = list(held_out or []) + list(held_in or [])
                    report.demoted += self_harness.review_efficacy(
                        resolved, cases, score_with=score_with,
                        score_without=score_without)
                except Exception:  # pragma: no cover
                    log.debug("self-harness efficacy review failed", exc_info=True)
        retired = 0
        if retire and self_harness.enabled() and scope is None:
            days = retire_after_days
            if days is None:
                days = self_harness.settings().get("retire_after_days", 0.0)
            if days and float(days) > 0:
                retired = self_harness.retire_stale(
                    older_than_days=float(days), model_id=resolved)
        return report, retired
    except Halted:
        raise
    except Exception as exc:  # pragma: no cover -- maintenance stays best-effort
        from .llm import ModelNotAllowedError

        if isinstance(exc, ModelNotAllowedError):
            raise
        log.debug("self-harness cycle failed", exc_info=True)
        from . import self_harness
        return self_harness.SelfHarnessReport(model_id=str(model_id or "")), 0


def _score_of(val) -> object:
    return val.get("success") if isinstance(val, Mapping) else val


def _finite_score(val) -> bool:
    s = _score_of(val)
    return isinstance(s, (int, float)) and math.isfinite(s)


def _avg_scores(a, b):
    """Mean of two scorer results (bare rates or mappings); numeric fields
    present in both are averaged, anything else keeps ``a``'s value.

    Replication reduces measurement variance but does *not* create new unique
    held-out cases.  Detailed results therefore keep the per-case denominator
    and fold replicate outcomes together instead of summing ``samples`` and
    ``attempted`` (which would let three cases masquerade as six independent
    observations at the promotion gate).
    """
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        out = dict(a)
        a_draws = a.get("draws", 1)
        b_draws = b.get("draws", 1)
        if not isinstance(a_draws, int) or isinstance(a_draws, bool) or a_draws < 1:
            a_draws = 1
        if not isinstance(b_draws, int) or isinstance(b_draws, bool) or b_draws < 1:
            b_draws = 1
        total_draws = a_draws + b_draws
        for k in ("success", "cost", "latency", "tool_calls"):
            av, bv = a.get(k), b.get(k)
            if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
                out[k] = (av * a_draws + bv * b_draws) / total_draws
        # Both draws were requested over the same case list. Preserve that
        # unique-case denominator; mismatched counts are marked incomplete and
        # rejected by the validator.
        for k in ("samples", "attempted"):
            av, bv = a.get(k), b.get(k)
            if isinstance(av, int) and isinstance(bv, int) and av == bv:
                out[k] = av
            elif isinstance(av, int) and isinstance(bv, int):
                out[k] = min(av, bv)
                out["complete"] = False
        if isinstance(a.get("outcomes"), (list, tuple)) and isinstance(
                b.get("outcomes"), (list, tuple)):
            if len(a["outcomes"]) == len(b["outcomes"]):
                out["outcomes"] = list(zip(a["outcomes"], b["outcomes"], strict=True))
            else:
                out["outcomes"] = []
                out["complete"] = False
        out["draws"] = total_draws
        if "complete" in a or "complete" in b:
            out["complete"] = (bool(out.get("complete", True))
                               and bool(a.get("complete")) and bool(b.get("complete")))
        if "clean" in a or "clean" in b:
            out["clean"] = bool(a.get("clean")) and bool(b.get("clean"))
        if "budget_exhausted" in a or "budget_exhausted" in b:
            out["budget_exhausted"] = bool(a.get("budget_exhausted")) or bool(
                b.get("budget_exhausted"))
        return out
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (a + b) / 2
    return a


def _memo_scorer(fn, *, draws: int = 2, by_line: bool = False,
                 require_all_draws: bool = False):
    """Cache a scorer on its goals tuple. The BASELINE arm ignores the
    candidate line, yet ``validate_proposal`` re-scores it per candidate --
    with K candidates that is (K-1)x redundant LLM generations per pool, the
    dominant redundant cost of a cycle or transfer sweep.

    Two integrity rules, because a memoized baseline is a FROZEN draw of a
    stochastic measurement that then adjudicates every later candidate:
    (1) never cache an arm that is non-finite (budget-dead pot, broken
    scorer) or DIRTY (the scorer's ``last_clean`` reports a fail-open
    degradation -- an outage 0.0 is indistinguishable from a real 0.0 after
    the fact); (2) average ``draws`` clean draws before freezing, halving the
    variance of the one realization every candidate is compared against
    (the correlated-draw tradeoff the adversarial review flagged)."""
    cache: dict[tuple, object] = {}

    def _clean() -> bool:
        return bool(getattr(fn, "last_clean", True))

    def _mark_clean(value) -> bool:
        if isinstance(value, Mapping) and "clean" in value:
            # Neither channel may overrule the other: the structured payload
            # describes case coverage, while ``last_clean`` carries provider
            # degradation detected outside that payload.
            return bool(value.get("clean")) and _clean()
        return _clean()

    def _indeterminate_like(value):
        if isinstance(value, Mapping):
            out = dict(value)
            out.update(success=float("nan"), samples=0, complete=False, clean=False)
            return out
        return float("nan")

    def _scored(line, goals):
        key = ((str(line), tuple(goals)) if by_line else tuple(goals))
        if key in cache:
            cached = cache[key]
            _scored.last_clean = _mark_clean(cached)
            return cached
        first = fn(line, goals)
        first_clean = _finite_score(first) and _mark_clean(first)
        _scored.last_clean = first_clean
        if not first_clean:
            return first  # indeterminate/dirty: use once, never freeze
        val = first
        for _ in range(max(0, int(draws) - 1)):
            nxt = fn(line, goals)
            if not (_finite_score(nxt) and _mark_clean(nxt)):
                if require_all_draws:
                    _scored.last_clean = False
                    return _indeterminate_like(first)
                return first  # keep the clean draw, don't freeze a mix
            val = _avg_scores(val, nxt)
        cache[key] = val
        _scored.last_clean = _mark_clean(val)
        return val

    _scored.last_clean = True
    return _scored


def run_corpus_harvest(world, *, mode: str | None = None, key: str | None = None,
                       corpus_path: str | None = None,
                       project_id: int | None = None, owner: str | None = None,
                       raise_errors: bool = False) -> int:
    """Driver entry for corpus bootstrapping: mine hindsight-pair candidates
    from the reflexion log + the world's DONE goals and stage ("propose") or
    stage them for ``key`` (default: the orchestrator model). An exact matter
    and owner are mandatory; the corpus itself is stored in that namespace.
    Legacy ``mode="auto"`` is demoted to staging so harvesting never promotes
    client-derived cases into the live evaluation corpus unattended. The
    history limits and the
    secret-redaction pass live here, not in each CLI. Goals already live,
    pending, or operator-rejected are excluded BEFORE the candidate cap so
    they can't starve fresh candidates. Returns how many candidates were
    newly staged/merged; 0 when off/unconfigured. Never raises unless
    ``raise_errors`` -- the interactive CLI sets it so a write failure isn't
    indistinguishable from "nothing to harvest"."""
    try:
        from . import reflexion, self_harness
        from . import self_harness_eval as ev
        from .reflexion import _sanitize_text
        st = self_harness.settings()
        mode = mode or st.get("corpus_harvest", "off")
        corpus_path = corpus_path or st.get("eval_corpus")
        if mode not in ("propose", "auto") or not corpus_path or world is None:
            return 0
        scope = _learning_scope(project_id, owner)
        if scope is None:
            return 0
        matter_id, exact_owner, _owner_scope = scope
        corpus_path = _scoped_corpus_path(corpus_path, scope=scope)
        if key is None:
            from .llm import model_for_role
            key = model_for_role("orchestrator")
        refl = _scope_reflexions(
            reflexion.list_recent(limit=500), scope=scope,
        )
        goals = []
        for goal in world.list_goals(
            status="done", owner=exact_owner, project_id=matter_id,
            limit=200, order="desc",
        ):
            goal_owner = getattr(goal, "owner", None)
            if (
                getattr(goal, "project_id", None) != matter_id
                or goal_owner is None
                or str(goal_owner) != exact_owner
            ):
                continue
            goals.append(goal)
        known = {c["goal"] for c in ev.load_eval_corpus(corpus_path).get(str(key), [])}
        known |= {c["goal"] for c in ev.load_pending(corpus_path).get(str(key), [])}
        known |= set(ev.load_rejected(corpus_path).get(str(key), []))
        # The corpus/pending files are plaintext sidecars; goal content can be
        # sealed at rest and is secret-redacted on every other persistence
        # path -- redact here too, before it leaves the world DB.
        cands = [{"goal": _sanitize_text(c["goal"]),
                  "expected": _sanitize_text(c["expected"])}
                 for c in ev.harvest_corpus_candidates(refl, goals, known=known)]
        # The files hold REDACTED text, so re-check known post-redaction too
        # (a secret-bearing goal changes form between mining and staging).
        cands = [c for c in cands if c["goal"] not in known]
        return ev.stage_candidates(corpus_path, key, cands)
    except Exception:  # pragma: no cover -- harvesting never perturbs a run
        if raise_errors:
            raise
        log.warning("self-harness corpus harvest failed", exc_info=True)
        return 0


def run_corpus_quality(*, key: str | None = None, corpus_path: str | None = None,
                       project_id: int | None = None, owner: str | None = None,
                       samples: int = 2, raise_errors: bool = False) -> list[dict]:
    """Driver entry for the corpus quality probe: measure each live case's
    baseline discriminativeness with the same model-generates /
    verifier-judges seams and eval-budget pot the A/B uses (``key`` is the
    corpus key and, for a model key, the generating model). Rows gain
    ``age_days`` from the raw file's harvest ``added_at`` stamp when present
    (hand-authored rows have none -- age unknown). Returns ``[]`` when
    off/unconfigured. Never raises unless ``raise_errors`` -- the interactive
    CLI sets it."""
    try:
        from . import self_harness
        from . import self_harness_eval as ev
        from .llm import LLM, model_for_role
        st = self_harness.settings()
        corpus_path = corpus_path or st.get("eval_corpus")
        if not corpus_path:
            return []
        scope = _learning_scope(project_id, owner)
        if scope is None:
            return []
        corpus_path = _scoped_corpus_path(corpus_path, scope=scope)
        if key is None:
            key = model_for_role("orchestrator")
        cases = ev.corpus_cases(ev.load_eval_corpus(corpus_path), key)
        if not cases:
            return []
        budget = _eval_budget(st)
        rows = ev.corpus_quality(
            cases,
            run_fn=ev.llm_runner(LLM(str(key)), budget=budget),
            judge_fn=ev.llm_judge(LLM(model_for_role("verifier")), budget=budget,
                                  samples=st.get("judge_samples", 1)),
            samples=samples)
        added: dict[str, float] = {}
        for r in ev._load_raw(corpus_path).get(str(key), []) or []:
            if isinstance(r, dict) and r.get("goal") and r.get("added_at"):
                added[str(r["goal"])] = float(r["added_at"])
        now = time.time()
        for row in rows:
            ts = added.get(row["goal"])
            row["age_days"] = round((now - ts) / 86400.0, 1) if ts else None
        return rows
    except Exception:  # pragma: no cover -- a quality probe never perturbs a run
        if raise_errors:
            raise
        log.warning("self-harness corpus quality probe failed", exc_info=True)
        return []


__all__ = [
    "collect_calibration", "build_prm_examples", "run_self_harness_pass",
    "run_self_harness_cycle", "run_corpus_harvest",
]
