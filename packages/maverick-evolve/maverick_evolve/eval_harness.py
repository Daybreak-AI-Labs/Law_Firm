"""Stage 0: the trusted fitness function.

You cannot safely evolve an agent without a metric you believe. This scores an
agent (any ``async (prompt) -> str``) against a held-out set of cases. A case
either carries its own ``check`` predicate (ground truth — the strongest signal)
or a ``reference`` answer scored by an injected ``scorer`` (default: substring
containment). Keep ground-truth cases where you can; that is what makes the
fitness signal ungameable enough to evolve against.

Pure + dependency-injected (you supply the agent and, optionally, the scorer),
so it runs in tests without a live model. Pair with ``maverick.calibration`` to
freeze evolution when the judge drifts.
"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass
class EvalCase:
    prompt: str
    check: Callable[[str], bool] | None = None  # ground-truth predicate (preferred)
    reference: str | None = None                # else scored against this
    weight: float = 1.0


@dataclass
class EvalReport:
    n: int
    passed: float          # weighted sum of per-case scores
    total_weight: float

    @property
    def score(self) -> float:
        """Weighted pass rate in [0,1]."""
        return self.passed / self.total_weight if self.total_weight > 0 else 0.0


def _contains_scorer(output: str, reference: str) -> float:
    """Default reference scorer: 1.0 if the reference appears in the output."""
    if not reference:
        return 0.0
    return 1.0 if reference.strip().lower() in (output or "").lower() else 0.0


async def evaluate(
    agent: Callable[[str], Awaitable[str]],
    cases: list[EvalCase],
    *,
    scorer: Callable[[str, str], float] | None = None,
) -> EvalReport:
    """Run ``agent`` over ``cases`` and return a weighted fitness report.

    A case's ``check`` (ground truth) wins when present; otherwise the output is
    scored against ``reference`` via ``scorer`` (default substring containment).
    An agent that raises on a case scores 0 for that case (robustness counts).
    """
    scorer = scorer or _contains_scorer
    weights: list[float] = []
    for case in cases:
        raw_weight = case.weight
        if (isinstance(raw_weight, bool)
                or not isinstance(raw_weight, (int, float))):
            raise ValueError("case weight must be numeric, not boolean")
        weight = float(raw_weight)
        if not math.isfinite(weight) or weight < 0.0:
            raise ValueError("case weight must be finite and non-negative")
        weights.append(weight)
    total = sum(weights)
    if cases and (not math.isfinite(total) or total <= 0.0):
        raise ValueError(
            "evaluation cases must have finite positive total weight")
    passed = 0.0
    total_weight = 0.0
    for case, weight in zip(cases, weights, strict=True):
        total_weight += weight
        score = await evaluate_case(agent, case, scorer=scorer)
        passed += weight * score
    return EvalReport(n=len(cases), passed=passed, total_weight=total_weight)


async def evaluate_case(
    agent: Callable[[str], Awaitable[str]],
    case: EvalCase,
    *,
    scorer: Callable[[str, str], float] | None = None,
) -> float:
    """Evaluate one agent/case exposure and return its bounded outcome.

    DGM's risk-limited confirmation uses this seam to interleave the seed and
    frozen champion case by case. Each arm still receives exactly one exposure
    per sealed case, while alternating order avoids giving one arm every cold or
    every warm evaluator/provider position.
    """
    scorer = scorer or _contains_scorer
    try:
        out = await agent(case.prompt)
    except Exception:
        out = ""
    if case.check is not None:
        checked = case.check(out)
        if not isinstance(checked, bool):
            raise ValueError("ground-truth check must return an exact boolean")
        return 1.0 if checked else 0.0
    if case.reference is not None:
        raw_score = scorer(out, case.reference)
        if isinstance(raw_score, bool):
            raise ValueError("evaluator score must be numeric, not boolean")
        try:
            score = float(raw_score)
        except (TypeError, ValueError) as exc:
            raise ValueError("evaluator score must be numeric") from exc
        # Python's min/max handling of NaN is order-dependent; the historical
        # clamp turned NaN into 1.0 and could manufacture a perfect champion.
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("evaluator score must be finite and in [0, 1]")
        return score
    return 0.0


__all__ = ["EvalCase", "EvalReport", "evaluate", "evaluate_case"]
