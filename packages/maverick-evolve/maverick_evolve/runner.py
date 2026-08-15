"""Stage 1 wiring: evolve a config against the eval harness, gated by calibration.

Ties the three foundations together into a runnable loop:

  - **development fitness** comes from ``eval_harness.evaluate`` over cases the
    search may reuse, while an optional sealed set confirms only the fixed
    champion once;
  - **the gate** is ``maverick.calibration``: if the verifier has drifted
    (learning frozen), we REFUSE to evolve -- you cannot trust a fitness score
    produced by a miscalibrated judge, and evolving on it would amplify the
    drift. This is the trust thermostat applied to self-improvement;
  - **the search** is config-only (``config_space`` + ``search.evolve``), so a
    candidate can never escape the sandbox.

Dependency-injected: the caller supplies ``agent_factory(config) -> async agent``
(how a config becomes a runnable Lightwork agent), so this is testable without a
live model and stays decoupled from how the kernel instantiates agents.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import secrets
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from statistics import NormalDist

from . import config_space
from .archive import Archive, Candidate
from .eval_harness import EvalCase, evaluate, evaluate_case
from .metaproductive import MetaproductiveArchive, evolve_metaproductive
from .search import evolve

log = logging.getLogger(__name__)

AgentFactory = Callable[[dict], Callable[[str], Awaitable[str]]]
OutputScorer = Callable[[str, str], float]
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_CASE_TEXT = 1_048_576


def _confirmation_request_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _normalized_prompt_identity(prompt: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", prompt).split()).casefold()


@dataclass(frozen=True)
class ConfirmationRequest:
    """One nonce-bound request to expose a sealed confirmation family.

    The externally supplied family and evaluator digests bind semantics that
    Python callables cannot be fingerprinted reliably.  Candidate identities,
    the distinct case count, and a fresh nonce prevent a permit from being
    replayed for another comparison.
    """

    seed_candidate_id: str
    champion_candidate_id: str
    case_family_id: str
    case_snapshot_sha256: str
    evaluator_id: str
    case_count: int
    request_id: str = field(default_factory=lambda: secrets.token_hex(16))
    request_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("seed_candidate_id", self.seed_candidate_id),
            ("champion_candidate_id", self.champion_candidate_id),
            ("case_family_id", self.case_family_id),
            ("case_snapshot_sha256", self.case_snapshot_sha256),
            ("evaluator_id", self.evaluator_id),
        ):
            if not isinstance(value, str) or not _HEX_256.fullmatch(value):
                raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        if (not isinstance(self.case_count, int) or isinstance(self.case_count, bool)
                or self.case_count <= 0):
            raise ValueError("case_count must be a positive integer")
        if not isinstance(self.request_id, str) or not _REQUEST_ID.fullmatch(
                self.request_id):
            raise ValueError("request_id must be a 128-bit lowercase hex nonce")
        payload = {
            "version": 1,
            "request_id": self.request_id,
            "seed_candidate_id": self.seed_candidate_id,
            "champion_candidate_id": self.champion_candidate_id,
            "case_family_id": self.case_family_id,
            "case_snapshot_sha256": self.case_snapshot_sha256,
            "evaluator_id": self.evaluator_id,
            "case_count": self.case_count,
        }
        object.__setattr__(
            self, "request_sha256", _confirmation_request_digest(payload))


@dataclass(frozen=True)
class ConfirmationPermit:
    """A short-lived durable-authority receipt for one exact request."""

    request_sha256: str
    critical_z: float
    authorization_id: str
    ledger_tip_sha256: str
    issued_at: float
    expires_at: float


ConfirmationAuthorize = Callable[[ConfirmationRequest], ConfirmationPermit]


class EvolutionFrozen(RuntimeError):
    """Raised when evolution is refused because verifier calibration is frozen."""


def calibration_frozen() -> bool:
    """Whether the verifier calibration interlock is currently frozen.

    Evolution is a high-risk consumer of the verifier, so an unavailable or
    raising calibration backend is an *unknown* verdict and therefore freezes
    evolution.  The existing ``MAVERICK_LEARNING_FROZEN=0`` override remains
    available for the learning-proof/development harness; no evolve-specific
    bypass is introduced here.
    """
    try:
        from maverick.calibration import learning_frozen
        return bool(learning_frozen())
    except Exception as exc:
        override = os.environ.get("MAVERICK_LEARNING_FROZEN", "").strip().lower()
        if override in {"0", "false", "no", "off"}:
            log.warning(
                "calibration unavailable; honoring explicit "
                "MAVERICK_LEARNING_FROZEN=0 override: %s",
                exc,
            )
            return False
        log.error("calibration unavailable; freezing evolution: %s", exc)
        return True


def strict_calibration_ready(
    *, evaluator_id: str | None = None, max_age_hours: float = 24.0,
) -> bool:
    """Require a fresh, internally coherent positive calibration receipt.

    Unlike ``learning_frozen()``, this risk-contract check never honors the
    development override and never treats absent enforcement/evidence as safe.
    ``max_age_hours`` may tighten, but not extend, the 24-hour production bound.
    """
    try:
        age_hours = float(max_age_hours)
        if (not math.isfinite(age_hours) or age_hours <= 0.0
                or age_hours > 24.0):
            return False
        from maverick import calibration

        bound_id = str(evaluator_id or "").strip()
        if not _HEX_256.fullmatch(bound_id):
            return False
        verdict = calibration._load_verdict()
        settings = calibration._settings()
        if (not isinstance(verdict, Mapping)
                or verdict.get("adequate") is not True
                or verdict.get("schema") != "maverick-calibration-receipt-v2"
                or str(verdict.get("evaluator_id") or "") != bound_id):
            return False
        n = verdict.get("n")
        n_correct = verdict.get("n_correct")
        n_incorrect = verdict.get("n_incorrect")
        if (not isinstance(n, int) or isinstance(n, bool)
                or not isinstance(n_correct, int) or isinstance(n_correct, bool)
                or not isinstance(n_incorrect, int) or isinstance(n_incorrect, bool)
                or n_correct <= 0 or n_incorrect <= 0
                # ``n`` may include adversarial probes while discrimination is
                # computed over natural traffic only.  Charge the support floor
                # to the exact cohort behind that statistic so 18 probes plus
                # one natural example per class cannot mint a receipt.
                or n_correct + n_incorrect
                < max(20, int(settings.get("min_samples", 20)))
                or n_correct + n_incorrect > n):
            return False
        discrimination = float(verdict.get("discrimination"))
        brier = float(verdict.get("brier"))
        stamp = float(verdict.get("ts"))
        sample_min = float(verdict.get("sample_min_ts"))
        sample_max = float(verdict.get("sample_max_ts"))
        now = time.time()
        return (
            math.isfinite(discrimination)
            and max(0.15, float(settings.get("min_discrimination", 0.15)))
            <= discrimination <= 1.0
            and math.isfinite(brier) and 0.0 <= brier <= 1.0
            and all(math.isfinite(value)
                    for value in (stamp, sample_min, sample_max))
            and 0.0 <= now - stamp <= age_hours * 3600.0
            and 0.0 <= now - sample_min <= age_hours * 3600.0
            and sample_min <= sample_max <= stamp
        )
    except (ImportError, OSError, TypeError, ValueError, OverflowError):
        return False


def _confirmation_margin(value: float) -> float:
    """Validate the required one-shot confirmation lift."""
    margin = float(value)
    if not math.isfinite(margin) or margin < 0.0 or margin > 1.0:
        raise ValueError("confirmation_margin must be finite and in [0, 1]")
    return margin


def _snapshot_cases(
    cases: list[EvalCase] | tuple[EvalCase, ...],
) -> tuple[EvalCase, ...]:
    """Copy case fields so caller mutation cannot change an in-flight split."""
    for case in cases:
        if not isinstance(case, EvalCase):
            raise ValueError("evaluation cases must be EvalCase instances")
        if (not isinstance(case.prompt, str) or not case.prompt.strip()
                or len(case.prompt) > _MAX_CASE_TEXT or "\x00" in case.prompt):
            raise ValueError("case prompt must be bounded non-empty text")
        if (case.reference is not None
                and (not isinstance(case.reference, str)
                     or len(case.reference) > _MAX_CASE_TEXT
                     or "\x00" in case.reference)):
            raise ValueError("case reference must be bounded text")
        if case.check is not None and not callable(case.check):
            raise ValueError("case check must be callable")
        if (isinstance(case.weight, bool)
                or not isinstance(case.weight, (int, float))
                or not math.isfinite(float(case.weight))
                or float(case.weight) < 0.0):
            raise ValueError("case weight must be finite and non-negative")
    return tuple(
        EvalCase(
            prompt=case.prompt,
            check=case.check,
            reference=case.reference,
            weight=case.weight,
        )
        for case in cases
    )


def _visible_case_snapshot_digest(cases: tuple[EvalCase, ...]) -> str:
    """Bind all case semantics visible without introspecting trusted callables."""
    return _confirmation_request_digest({
        "version": 1,
        "cases": [
            {
                "prompt": case.prompt,
                "reference": case.reference,
                "weight": float(case.weight),
                "uses_callable_check": case.check is not None,
            }
            for case in cases
        ],
    })


def _prepare_confirmation(
    cases: list[EvalCase] | tuple[EvalCase, ...] | None,
    scorer: OutputScorer | None,
    margin: float,
) -> tuple[EvalCase, ...] | None:
    """Validate and snapshot the optional sealed confirmation boundary."""
    if cases is None:
        if scorer is not None:
            raise ValueError("confirmation_scorer requires confirmation_cases")
        if float(margin) != 0.0:
            raise ValueError("confirmation_margin requires confirmation_cases")
        return None

    _confirmation_margin(margin)
    sealed_cases = _snapshot_cases(cases)
    if not sealed_cases:
        raise ValueError("confirmation_cases must not be empty")
    if any(isinstance(case.weight, bool)
           or not isinstance(case.weight, (int, float))
           for case in sealed_cases):
        raise ValueError("confirmation case weights must be numeric, not boolean")
    weights = [float(case.weight) for case in sealed_cases]
    if any(not math.isfinite(weight) or weight < 0.0 for weight in weights):
        raise ValueError("confirmation case weights must be finite and non-negative")
    total_weight = sum(weights)
    if not math.isfinite(total_weight) or total_weight <= 0.0:
        raise ValueError(
            "confirmation_cases must have finite positive total weight")
    return sealed_cases


def _confirmation_policy(
    sealed_cases: tuple[EvalCase, ...] | None, *, risk_limited: bool,
    minimum_cases: int | None, confidence_z: float | None,
    authorize: ConfirmationAuthorize | None,
    case_family_id: str | None = None,
    evaluator_id: str | None = None,
) -> tuple[int, float]:
    """Validate a confirmation contract before adaptive development begins."""
    requested_minimum = (
        (20 if risk_limited else 0) if minimum_cases is None else minimum_cases)
    requested_z = (
        (1.96 if risk_limited else 0.0) if confidence_z is None else confidence_z)
    if (not isinstance(requested_minimum, int)
            or isinstance(requested_minimum, bool) or requested_minimum < 0):
        raise ValueError("confirmation_min_cases must be a non-negative integer")
    if (isinstance(requested_z, bool) or not isinstance(requested_z, (int, float))
            or not math.isfinite(float(requested_z)) or float(requested_z) < 0.0):
        raise ValueError("confirmation_confidence_z must be finite and non-negative")
    # Calling a run risk-limited is a contract, not a collection of defaults
    # that a zero-valued override may silently weaken. Callers may tighten the
    # evidence floor; development mode remains the explicit escape hatch.
    minimum = max(20, requested_minimum) if risk_limited else requested_minimum
    z = max(1.96, float(requested_z)) if risk_limited else float(requested_z)
    if risk_limited and sealed_cases is None:
        raise ValueError("risk-limited evolution requires sealed confirmation cases")
    if risk_limited and authorize is None:
        raise ValueError(
            "risk-limited evolution requires durable confirmation authorization")
    if risk_limited:
        if (not isinstance(case_family_id, str)
                or not _HEX_256.fullmatch(case_family_id)):
            raise ValueError(
                "risk-limited evolution requires a SHA-256 confirmation family id")
        if (not isinstance(evaluator_id, str)
                or not _HEX_256.fullmatch(evaluator_id)):
            raise ValueError(
                "risk-limited evolution requires a SHA-256 confirmation evaluator id")
    positive_cases = sum(
        1 for case in (sealed_cases or ()) if float(case.weight) > 0.0)
    if risk_limited and sealed_cases is not None:
        positive_prompts = [
            _normalized_prompt_identity(case.prompt)
            for case in sealed_cases if float(case.weight) > 0.0]
        if len(set(positive_prompts)) != len(positive_prompts):
            raise ValueError(
                "risk-limited confirmation requires distinct positive-weight prompts")
    if sealed_cases is not None and positive_cases < minimum:
        raise ValueError(
            f"sealed confirmation has too few positive-weight cases "
            f"({positive_cases} < {minimum})")
    return minimum, z


def _confirmation_permit_z(
    raw: object, request: ConfirmationRequest, *, require_structured: bool,
) -> float:
    """Validate a nonce-bound permit and return its critical value.

    Development callers retain the historical numeric callback contract.  A
    risk-limited run accepts only a structured, fresh receipt tied to the exact
    comparison and an externally anchored ledger tip.
    """
    if not isinstance(raw, ConfirmationPermit):
        if require_structured:
            raise ValueError("risk-limited authorization returned no structured permit")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("confirmation authorization returned an invalid permit")
        critical = float(raw)
        if not math.isfinite(critical) or critical <= 0.0:
            raise ValueError("invalid confirmation permit critical value")
        return critical

    if raw.request_sha256 != request.request_sha256:
        raise ValueError("confirmation permit does not match this request")
    if (not isinstance(raw.authorization_id, str)
            or not 1 <= len(raw.authorization_id) <= 128
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in raw.authorization_id)):
        raise ValueError("confirmation permit authorization id is invalid")
    if (not isinstance(raw.ledger_tip_sha256, str)
            or not _HEX_256.fullmatch(raw.ledger_tip_sha256)):
        raise ValueError("confirmation permit ledger tip is invalid")
    values = (raw.critical_z, raw.issued_at, raw.expires_at)
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(float(value)) for value in values):
        raise ValueError("confirmation permit numeric fields are invalid")
    now = time.time()
    if (float(raw.critical_z) <= 0.0
            or float(raw.issued_at) > now + 5.0
            or float(raw.expires_at) < now
            or float(raw.expires_at) <= float(raw.issued_at)
            or float(raw.expires_at) - float(raw.issued_at) > 3600.0):
        raise ValueError("confirmation permit is expired or outside its validity window")
    return float(raw.critical_z)


def _validate_resumed_config(config: dict, seed_config: dict,
                             space: dict[str, tuple] | None) -> None:
    """Keep persisted candidates inside this run's declared config envelope.

    A hand-edited archive must not introduce a new capability/config key that
    mutation could never have produced. Known mutable knobs must also retain the
    declared numeric type and bounds before an ``agent_factory`` sees them.
    """
    active_space = space or config_space.SPACE
    unexpected = set(config) - (set(seed_config) | set(active_space))
    if unexpected:
        raise ValueError(
            f"archive candidate introduces undeclared config keys: {sorted(unexpected)}")
    for knob, spec in active_space.items():
        if knob not in config:
            continue
        value = config[knob]
        if spec[0] == "int":
            valid_type = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid_type = isinstance(value, (int, float)) and not isinstance(value, bool)
        if (not valid_type or not math.isfinite(float(value))
                or not float(spec[1]) <= float(value) <= float(spec[2])):
            raise ValueError(f"archive candidate has invalid value for {knob!r}")


async def confirm_candidate(
    seed: Candidate,
    champion: Candidate,
    confirmation_cases: list[EvalCase] | tuple[EvalCase, ...],
    agent_factory: AgentFactory,
    *,
    scorer: OutputScorer | None = None,
    margin: float = 0.0,
    min_cases: int = 0,
    confidence_z: float = 0.0,
    authorize: ConfirmationAuthorize | None = None,
    require_authorization: bool = False,
    case_family_id: str | None = None,
    evaluator_id: str | None = None,
) -> Candidate:
    """Confirm an adaptively selected champion exactly once against the seed.

    This function is deliberately separate from the development-score closure
    used by :func:`evolve_with_eval`.  The sealed cases and scorer enter only
    after search has selected a fixed champion.  Both seed and champion receive
    one evaluation over the same immutable case snapshot.  A tie, insufficient
    lift, or confirmation backend error rejects the promotion and returns the
    seed without exposing a score that a later search round could optimize.
    """
    sealed_cases = _prepare_confirmation(confirmation_cases, scorer, margin)
    assert sealed_cases is not None
    margin = _confirmation_margin(margin)
    _confirmation_policy(
        sealed_cases, risk_limited=require_authorization,
        minimum_cases=min_cases, confidence_z=confidence_z,
        authorize=authorize, case_family_id=case_family_id,
        evaluator_id=evaluator_id)
    if champion.id == seed.id:
        return seed

    if (require_authorization
            and not strict_calibration_ready(evaluator_id=evaluator_id)):
        log.error("strict calibration receipt unavailable; keeping seed")
        return seed

    if authorize is not None:
        try:
            if require_authorization:
                assert case_family_id is not None and evaluator_id is not None
                request = ConfirmationRequest(
                    seed_candidate_id=seed.id,
                    champion_candidate_id=champion.id,
                    case_family_id=case_family_id,
                    case_snapshot_sha256=_visible_case_snapshot_digest(sealed_cases),
                    evaluator_id=evaluator_id,
                    case_count=len(sealed_cases),
                )
                permit_z = _confirmation_permit_z(
                    authorize(request), request, require_structured=True)
            else:
                # Backward-compatible development seam. Production mode above
                # deliberately never calls the legacy three-argument callback.
                permit_z = _confirmation_permit_z(
                    authorize(seed.id, champion.id, sealed_cases),  # type: ignore[call-arg]
                    ConfirmationRequest(
                        seed_candidate_id=seed.id,
                        champion_candidate_id=champion.id,
                        case_family_id="0" * 64,
                        case_snapshot_sha256=_visible_case_snapshot_digest(sealed_cases),
                        evaluator_id="0" * 64,
                        case_count=len(sealed_cases),
                    ),
                    require_structured=False,
                )
            confidence_z = max(float(confidence_z), permit_z)
        except Exception as exc:
            log.error("sealed confirmation authorization failed; keeping seed: %s", exc)
            return seed
    elif require_authorization:
        return seed

    try:
        seed_agent = agent_factory(dict(seed.config))
        champion_agent = agent_factory(dict(champion.config))
        seed_outcomes: list[float] = []
        champion_outcomes: list[float] = []
        # Counterbalance arm order across the immutable case order. Both arms
        # still receive one and only one evaluation of every sealed case.
        for index, case in enumerate(sealed_cases):
            if index % 2 == 0:
                seed_score = await evaluate_case(seed_agent, case, scorer=scorer)
                champion_score = await evaluate_case(
                    champion_agent, case, scorer=scorer)
            else:
                champion_score = await evaluate_case(
                    champion_agent, case, scorer=scorer)
                seed_score = await evaluate_case(seed_agent, case, scorer=scorer)
            seed_outcomes.append(seed_score)
            champion_outcomes.append(champion_score)
    except Exception as exc:
        log.error("sealed confirmation failed; rejecting champion: %s", exc)
        return seed

    # Evaluation can outlive the receipt that was fresh before exposure.  A
    # stale or replaced evaluator verdict must not authorize publication merely
    # because it was acceptable when the first case started.
    if (require_authorization
            and not strict_calibration_ready(evaluator_id=evaluator_id)):
        log.error("strict calibration receipt expired during confirmation; keeping seed")
        return seed

    weights = [float(case.weight) for case in sealed_cases]
    total_weight = sum(weights)
    seed_score = sum(weight * value for weight, value in zip(
        weights, seed_outcomes, strict=True)) / total_weight
    champion_score = sum(weight * value for weight, value in zip(
        weights, champion_outcomes, strict=True)) / total_weight
    delta = champion_score - seed_score
    if confidence_z > 0.0:
        # Distribution-free weighted Hoeffding lower bound for paired bounded
        # differences in [-1, 1]. It remains valid for fractional scorers and
        # unequal positive case weights; no normality or variance estimate is
        # borrowed from a small confirmation sample.
        alpha = 2.0 * (1.0 - NormalDist().cdf(float(confidence_z)))
        if not 0.0 < alpha < 1.0:
            log.error("confirmation critical value is numerically unsupported")
            return seed
        normalized = [weight / total_weight for weight in weights if weight > 0.0]
        radius = math.sqrt(2.0 * sum(weight * weight for weight in normalized)
                           * math.log(1.0 / alpha))
        lower = delta - radius
        if lower > margin:
            return champion
        log.info(
            "sealed confirmation lower bound %.6f did not clear margin %.6f; "
            "keeping seed", lower, margin)
        return seed

    # Strict comparison is intentional: at margin=0 a flat confirmation set
    # still rejects an apparent development winner rather than promoting noise.
    if champion_score > seed_score + margin:
        return champion
    log.info("sealed confirmation did not clear required margin; keeping seed")
    return seed


async def evolve_with_eval(
    seed_config: dict,
    cases: list[EvalCase],
    agent_factory: AgentFactory,
    *,
    generations: int = 10,
    scorer: OutputScorer | None = None,
    mutate: Callable[[dict], dict] | None = None,
    rng: random.Random | None = None,
    archive: Archive | None = None,
    space: dict[str, tuple] | None = None,
    seed_score: float | None = None,
    confirmation_cases: list[EvalCase] | tuple[EvalCase, ...] | None = None,
    confirmation_scorer: OutputScorer | None = None,
    confirmation_margin: float = 0.0,
    risk_limited: bool = False,
    confirmation_min_cases: int | None = None,
    confirmation_confidence_z: float | None = None,
    confirmation_authorize: ConfirmationAuthorize | None = None,
    confirmation_family_id: str | None = None,
    confirmation_evaluator_id: str | None = None,
    revalidate_archive: bool = False,
) -> Candidate:
    """Evolve ``seed_config`` to maximize eval-harness fitness, if calibrated.

    Raises :class:`EvolutionFrozen` when the calibration interlock is frozen --
    self-improvement is gated on a trustworthy judge. Otherwise builds the
    fitness function from ``evaluate(agent_factory(config), cases)`` and runs the
    config-only evolutionary search.  When ``confirmation_cases`` is supplied,
    development search cannot see that case snapshot or its scorer: after a
    champion is fixed, the champion must beat the seed once on the sealed set
    by strictly more than ``confirmation_margin`` or the seed is returned.
    ``revalidate_archive`` re-runs every resumed candidate on this invocation's
    development harness before its score can influence sampling or selection;
    persisted scores are observations, never authority.
    """
    if (risk_limited and not strict_calibration_ready(
            evaluator_id=confirmation_evaluator_id)) or (
            not risk_limited and calibration_frozen()):
        raise EvolutionFrozen(
            "verifier calibration is frozen; refusing to evolve against an "
            "untrustworthy fitness signal (run `maverick calibrate`)"
        )
    rng = rng or random.Random()
    development_cases = _snapshot_cases(cases)
    if not development_cases or sum(
            float(case.weight) for case in development_cases) <= 0.0:
        raise ValueError("evolution requires positive-weight development cases")
    sealed_cases = _prepare_confirmation(
        confirmation_cases,
        confirmation_scorer,
        confirmation_margin,
    )
    resolved_min_cases, resolved_confidence_z = _confirmation_policy(
        sealed_cases, risk_limited=risk_limited,
        minimum_cases=confirmation_min_cases,
        confidence_z=confirmation_confidence_z,
        authorize=confirmation_authorize,
        case_family_id=confirmation_family_id,
        evaluator_id=confirmation_evaluator_id)
    # A caller-owned archive is a publication-capable object. Adaptive search
    # with sealed confirmation therefore runs on a detached development copy;
    # rejection cannot leave its winner reachable through caller Archive.best.
    working_archive = (
        Archive.from_dict(archive.to_dict())
        if sealed_cases is not None and archive is not None else archive
    )

    # Capture the seed's development score so a rejected champion returns a
    # semantically complete Candidate without re-running development cases.
    development_seed_score = seed_score

    async def _score(config: dict) -> float:
        nonlocal development_seed_score
        agent = agent_factory(config)
        report = await evaluate(agent, development_cases, scorer=scorer)
        if config == seed_config:
            development_seed_score = report.score
        return report.score

    def _mutate(config: dict) -> dict:
        if mutate is not None:
            return mutate(config)
        return config_space.mutate(config, rng, space=space)

    if revalidate_archive and working_archive is not None:
        working_archive.confirmed_candidate_id = None
        # Validate the complete set before scoring any entry so a malicious
        # late candidate cannot consume evaluation budget and then fail.
        for archived in working_archive.candidates:
            _validate_resumed_config(archived.config, seed_config, space)
        for archived in working_archive.candidates:
            archived.score = await _score(dict(archived.config))
            if archived.config == seed_config:
                seed_score = archived.score

    champion = await evolve(
        seed_config, _mutate, _score,
        generations=generations, archive=working_archive, rng=rng,
        seed_score=seed_score,
    )
    if sealed_cases is None:
        return champion

    seed = Candidate(
        config=dict(seed_config),
        score=float(development_seed_score or 0.0),
    )
    confirmed = await confirm_candidate(
        seed,
        champion,
        sealed_cases,
        agent_factory,
        scorer=confirmation_scorer,
        margin=confirmation_margin,
        min_cases=resolved_min_cases,
        confidence_z=resolved_confidence_z,
        authorize=confirmation_authorize,
        require_authorization=risk_limited,
        case_family_id=confirmation_family_id,
        evaluator_id=confirmation_evaluator_id,
    )
    if confirmed.id == champion.id and working_archive is not None:
        working_archive.mark_confirmed(champion.id)
        if archive is not None:
            committed = Archive.from_dict(working_archive.to_dict())
            archive.capacity = committed.capacity
            archive.candidates = committed.candidates
            archive.confirmed_candidate_id = committed.confirmed_candidate_id
    return Candidate(config=confirmed.config, score=confirmed.score)


async def evolve_metaproductive_with_eval(
    seed_config: dict,
    cases: list[EvalCase],
    agent_factory: AgentFactory,
    *,
    evaluation_budget: int,
    expansion_budget: int = 10,
    evaluations_per_expansion: int = 2,
    min_observations_before_expansion: int = 1,
    best_belief_z: float = 1.96,
    case_family_id: str | None = None,
    allow_fractional_outcomes: bool = False,
    scorer: OutputScorer | None = None,
    mutate: Callable[[dict], dict] | None = None,
    rng: random.Random | None = None,
    archive: MetaproductiveArchive | None = None,
    space: dict[str, tuple] | None = None,
    confirmation_cases: list[EvalCase] | tuple[EvalCase, ...] | None = None,
    confirmation_scorer: OutputScorer | None = None,
    confirmation_margin: float = 0.0,
    risk_limited: bool = False,
    confirmation_min_cases: int | None = None,
    confirmation_confidence_z: float | None = None,
    confirmation_authorize: ConfirmationAuthorize | None = None,
    confirmation_family_id: str | None = None,
    confirmation_evaluator_id: str | None = None,
) -> Candidate:
    """Run task-budgeted clade search, then confirm one frozen champion.

    Unlike :func:`evolve_with_eval`, which fully evaluates every new config and
    samples parents from immediate score, this experimental policy decouples
    expansion from single-task development evaluations. Parent selection uses
    complete-clade evidence, and final development selection uses a conservative
    best-belief bound. ``evaluation_budget`` counts exact agent/case exposures;
    duplicate mutations still consume ``expansion_budget``.

    Binary outcomes are required by default so the Beta/Wilson evidence model
    retains its Bernoulli interpretation. ``allow_fractional_outcomes=True`` is
    an explicit heuristic compatibility mode, not calibrated promotion proof.
    Resuming development evidence requires a stable ``case_family_id`` that the
    caller versions whenever case or evaluator/scorer semantics change.

    The optional confirmation set remains structurally separate: it is never
    passed into the metaproductive archive, mutation policy, or partial evaluator.
    Search fixes one champion before :func:`confirm_candidate` compares that
    champion with the seed exactly once on the sealed snapshot.
    """
    if (risk_limited and not strict_calibration_ready(
            evaluator_id=confirmation_evaluator_id)) or (
            not risk_limited and calibration_frozen()):
        raise EvolutionFrozen(
            "verifier calibration is frozen; refusing metaproductive evolution "
            "against an untrustworthy fitness signal"
        )
    rng = rng or random.Random()
    development_cases = _snapshot_cases(cases)
    if not development_cases:
        raise ValueError("metaproductive evolution requires development cases")
    try:
        development_weights = [float(case.weight) for case in development_cases]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "development case weights must be finite and positive") from exc
    if any(not math.isfinite(weight) or weight <= 0.0
           for weight in development_weights):
        raise ValueError("development case weights must be finite and positive")
    # The metaproductive posterior currently models uniformly sampled Bernoulli
    # tasks.  Silently accepting unequal EvalCase weights would claim weighted
    # evidence while selecting cases and updating beliefs uniformly.  Fail
    # closed until the posterior itself supports weighted observations.
    # Equality is exact after numeric normalization. Even a tiny difference is
    # an operator-declared weighting that this uniformly sampled posterior
    # would silently ignore.
    if any(weight != development_weights[0]
           for weight in development_weights[1:]):
        raise ValueError(
            "metaproductive evolution requires equal development case weights")
    sealed_cases = _prepare_confirmation(
        confirmation_cases, confirmation_scorer, confirmation_margin)
    resolved_min_cases, resolved_confidence_z = _confirmation_policy(
        sealed_cases, risk_limited=risk_limited,
        minimum_cases=confirmation_min_cases,
        confidence_z=confirmation_confidence_z,
        authorize=confirmation_authorize,
        case_family_id=confirmation_family_id,
        evaluator_id=confirmation_evaluator_id)

    def _mutate(config: dict) -> dict:
        if mutate is not None:
            return mutate(config)
        return config_space.mutate(config, rng, space=space)

    async def _evaluate_case(config: dict, case: EvalCase) -> float:
        report = await evaluate(
            agent_factory(config), [case], scorer=scorer)
        outcome = float(report.score)
        if not math.isfinite(outcome) or not 0.0 <= outcome <= 1.0:
            raise ValueError("development evaluator returned an invalid task outcome")
        return outcome

    search_archive = archive or MetaproductiveArchive(
        capacity=max(1, expansion_budget + 1),
        allow_fractional_outcomes=allow_fractional_outcomes,
    )
    champion = await evolve_metaproductive(
        seed_config,
        _mutate,
        _evaluate_case,
        development_cases,
        evaluation_budget=evaluation_budget,
        expansion_budget=expansion_budget,
        evaluations_per_expansion=evaluations_per_expansion,
        min_observations_before_expansion=min_observations_before_expansion,
        best_belief_z=best_belief_z,
        archive=search_archive,
        rng=rng,
        case_family_id=case_family_id,
        allow_fractional_outcomes=allow_fractional_outcomes,
    )
    if sealed_cases is None:
        return champion

    seed = Candidate(config=dict(seed_config))
    if search_archive.root_id in search_archive.nodes:
        seed.score = search_archive.nodes[search_archive.root_id].candidate.score
    return await confirm_candidate(
        seed,
        champion,
        sealed_cases,
        agent_factory,
        scorer=confirmation_scorer,
        margin=confirmation_margin,
        min_cases=resolved_min_cases,
        confidence_z=resolved_confidence_z,
        authorize=confirmation_authorize,
        require_authorization=risk_limited,
        case_family_id=confirmation_family_id,
        evaluator_id=confirmation_evaluator_id,
    )


__all__ = [
    "EvolutionFrozen",
    "calibration_frozen",
    "confirm_candidate",
    "evolve_with_eval",
    "evolve_metaproductive_with_eval",
    "AgentFactory",
    "OutputScorer",
    "ConfirmationAuthorize",
    "ConfirmationPermit",
    "ConfirmationRequest",
]
