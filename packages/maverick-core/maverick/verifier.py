"""Verifier role: independent second-opinion pass on a proposer's answer.

Karpathy SOTA-review prescription: the recursive multi-agent ceremony
only earns its complexity if there's a real verify step. The current
``revisor`` role exists in prompt strings only -- no code actually runs
a verifier pass before declaring FINAL.

This module gives the orchestrator a single function to call:

    verdict = await verify_proposal(brief, proposal, llm, budget)

The verifier is invoked with a different system prompt + a fresh
budget allocation so its output isn't anchored by the proposer's
context. The verdict is structured:

    verdict.confidence:   float in [0, 1]
    verdict.accepts:      bool (confidence >= threshold)
    verdict.critique:     str (always populated; empty string if accepts)
    verdict.issues:       list[str] (specific problems flagged)

The agent loop uses `accepts` to early-stop, and feeds `critique` back
to the proposer as a revision brief if it doesn't accept. `confidence`
is the disagreement signal that adaptive fanout reads (see
``maverick.tools.spawn``).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from .budget import Budget, BudgetExceeded
from .llm import LLM, model_for_role

log = logging.getLogger(__name__)


# Default disagreement entropy threshold. Above this, fan out to more
# proposers; below, accept the single answer. Tunable via env.
DISAGREEMENT_HIGH = float(os.environ.get("MAVERICK_DISAGREEMENT_HIGH", "0.5"))
VERIFIER_CONFIDENCE_ACCEPT = float(os.environ.get("MAVERICK_VERIFIER_CONFIDENCE", "0.75"))


VERIFIER_SYSTEM = """You are an independent verifier reviewing another agent's answer to a goal.

You have access to no tools. Your job is to read the brief + the proposed final answer and decide:
1. Does the answer actually satisfy the brief? Be strict.
2. Are there factual errors, missing steps, or unsupported claims?
3. Would a careful human accept this?

Respond with a JSON object on a single line:

{"confidence": 0.0-1.0, "accepts": true|false, "critique": "<1-2 sentences>", "issues": ["<short issue>", ...]}

Confidence calibration:
- 0.9-1.0: The answer fully satisfies the brief, no meaningful issues.
- 0.7-0.9: Mostly correct; minor polish would help but it's defensible.
- 0.4-0.7: Significant gaps; a careful reviewer would want revisions.
- 0.0-0.4: Wrong direction or unsupported; reject.

`accepts` should be true iff confidence >= 0.75 AND issues is empty (or only nitpicks).
Output ONLY the JSON. No preamble, no markdown fence.
"""


@dataclass
class VerifierVerdict:
    confidence: float
    accepts: bool
    critique: str
    issues: list[str] = field(default_factory=list)
    raw: str = ""
    # When the structured rubric judge (maverick.reasoning_reward) produced this
    # verdict, its full to_audit_dict() -- the per-dimension rubric, reasoning,
    # and veto -- so a caller can record the legible reward in the learning
    # audit. None for the scalar verifier path.
    reward_audit: dict | None = None

    @classmethod
    def reject(cls, reason: str) -> VerifierVerdict:
        return cls(confidence=0.0, accepts=False, critique=reason, issues=[reason])

    @classmethod
    def accept_unconditionally(cls) -> VerifierVerdict:
        """For trivial cases where verification adds no value (e.g. empty
        brief, sub-second tasks). Skips the LLM call."""
        return cls(confidence=1.0, accepts=True, critique="", issues=[])


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse(text: str) -> VerifierVerdict:
    """Best-effort JSON extraction from the verifier's reply.

    Models sometimes wrap JSON in markdown fences or prefix with prose
    despite the system prompt. We extract the outermost {...} and parse
    it; on any failure we treat the verdict as low-confidence reject so
    the proposer is forced to revise.
    """
    if not text:
        return VerifierVerdict.reject("verifier returned empty response")
    m = _JSON_OBJECT_RE.search(text)
    if m is None:
        return VerifierVerdict.reject("verifier reply contained no JSON object")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        return VerifierVerdict.reject(f"verifier JSON parse failed: {e}")

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    accepts_raw = data.get("accepts", False)
    if isinstance(accepts_raw, str):
        accepts = accepts_raw.lower() in ("true", "yes", "1")
    else:
        accepts = bool(accepts_raw)

    critique = str(data.get("critique", "") or "")
    issues_raw = data.get("issues", []) or []
    issues = [str(x) for x in issues_raw if x]

    # Enforce the confidence floor in code -- VERIFIER_CONFIDENCE_ACCEPT was
    # dead, so the model's raw `accepts` boolean was trusted verbatim. The
    # verifier is the last correctness gate before FINAL, so accepting a
    # low-confidence verdict is a fail-open a miscalibrated/jailbroken verifier
    # could exploit (accepts=true, confidence=0.1). Below the threshold the
    # verdict is forced to a reject with a revision brief.
    if accepts and confidence < VERIFIER_CONFIDENCE_ACCEPT:
        accepts = False
        critique = critique or (
            f"verifier confidence {confidence:.2f} is below the accept "
            f"threshold {VERIFIER_CONFIDENCE_ACCEPT:.2f}"
        )

    return VerifierVerdict(
        confidence=confidence,
        accepts=accepts,
        critique=critique,
        issues=issues,
        raw=text,
    )


async def verify_proposal(
    brief: str,
    proposal: str,
    llm: LLM,
    budget: Budget | None = None,
    *,
    max_tokens: int = 1024,
    proposer_model: str | None = None,
) -> VerifierVerdict:
    """Ask the verifier role to judge a proposer's final answer.

    Uses the run's one explicitly pinned ``provider:model``. Spend lands in
    the passed budget; callers should expect ~$0.005-$0.05 per call.

    The verdict is conservative: any parsing failure / empty response /
    JSON-without-required-fields → reject. This keeps the proposer
    honest -- a flaky verifier can only make the system MORE careful,
    not less.

    Client-matter verification never swaps provider families or fans the
    privileged brief/proposal out to a panel. Cross-family comparison belongs
    in a separate deidentified offline evaluation workflow.
    """
    if not proposal or not proposal.strip():
        return VerifierVerdict.reject("proposal is empty")

    model = model_for_role("verifier")

    user_msg = (
        f"GOAL BRIEF:\n{brief}\n\n"
        f"PROPOSED FINAL ANSWER:\n{proposal}\n\n"
        "Return the verdict JSON."
    )
    try:
        resp = await llm.complete_async(
            system=VERIFIER_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            tools=None,
            budget=budget,
            max_tokens=max_tokens,
            model=model,
        )
    except BudgetExceeded:
        # Budget exhaustion is a control-flow signal for the budget
        # layer, not a verifier outcome — let it propagate.
        raise
    except Exception as e:  # pragma: no cover -- network errors
        # Fail CLOSED, per this module's contract ("any failure →
        # reject; a flaky verifier can only make the system MORE
        # careful, not less"). The previous fail-open (accepts=True)
        # silently disabled the safety gate exactly when the system was
        # least healthy.
        log.warning("verifier LLM call failed: %s; rejecting (fail-closed)", e)
        return VerifierVerdict.reject(f"verifier call failed: {e}")
    return _parse(resp.text)


def _structured_verify_enabled() -> bool:
    """Whether the structured rubric judge (maverick.reasoning_reward) is on."""
    try:
        from . import reasoning_reward
        return bool(reasoning_reward.enabled())
    except Exception:  # pragma: no cover -- never let this gate a run
        return False


def _verdict_from_reward(reward: object) -> VerifierVerdict:
    """Map a structured ``ReasoningReward`` onto the ``VerifierVerdict`` contract.

    ``confidence`` = holistic score; ``accepts`` = the reward's own accept
    decision (holistic bar AND no vetoed dimension); ``issues`` surfaces the
    failing/vetoed facets; ``reward_audit`` carries the full rubric for the
    learning audit.
    """
    dims = getattr(reward, "dimensions", ()) or ()
    issues: list[str] = []
    for d in dims:
        if getattr(d, "vetoes", False) or getattr(d, "score", 1.0) < 0.5:
            crit = getattr(d, "critique", "") or ""
            issues.append(f"{d.name}: {crit}" if crit else str(getattr(d, "name", "")))
    return VerifierVerdict(
        confidence=float(getattr(reward, "score", 0.0)),
        accepts=bool(reward.accepts()),
        critique=str(getattr(reward, "critique", "") or ""),
        issues=[i for i in issues if i],
        raw=str(getattr(reward, "raw", "") or ""),
        reward_audit=reward.to_audit_dict(),
    )


async def verify_proposal_structured(
    brief: str,
    proposal: str,
    llm: LLM,
    budget: Budget | None = None,
    *,
    max_tokens: int = 1024,
    proposer_model: str | None = None,
) -> VerifierVerdict:
    """Verify with the structured rubric judge (Agent-RRM, reasoning_reward).

    Sends ``REASONING_REWARD_SYSTEM`` and parses a per-dimension rubric reward:
    a single failing dimension (e.g. ``safety`` below its veto floor) rejects
    even a high holistic score, and the full rubric is attached to the verdict
    for the learning audit. Any reply that is NOT a compliant rubric (no
    dimensions parsed) falls back to the scalar verifier parse, so this is a
    safe superset of :func:`verify_proposal` with the same VerifierVerdict
    contract and the same single-provider / fail-closed behaviour.
    """
    if not proposal or not proposal.strip():
        return VerifierVerdict.reject("proposal is empty")

    from . import reasoning_reward

    model = model_for_role("verifier")

    user_msg = (
        f"GOAL BRIEF:\n{brief}\n\n"
        f"PROPOSED FINAL ANSWER:\n{proposal}\n\n"
        "Return the rubric JSON."
    )
    try:
        resp = await llm.complete_async(
            system=reasoning_reward.REASONING_REWARD_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            tools=None,
            budget=budget,
            max_tokens=max_tokens,
            model=model,
        )
    except BudgetExceeded:
        raise
    except Exception as e:  # pragma: no cover -- network errors
        log.warning("structured verifier LLM call failed: %s; rejecting (fail-closed)", e)
        return VerifierVerdict.reject(f"verifier call failed: {e}")

    reward = reasoning_reward.parse_structured(resp.text)
    if getattr(reward, "dimensions", ()):  # a compliant, complete rubric reply
        _maybe_audit_reward(reward)
        return _verdict_from_reward(reward)
    if reasoning_reward.looks_structured(resp.text):
        return VerifierVerdict.reject(
            "structured verifier returned an incomplete or invalid rubric"
        )
    # Non-rubric reply (or garbage): behave exactly like the scalar verifier.
    return _parse(resp.text)


def _maybe_audit_reward(reward: object) -> None:
    """Sign a structured reward into the tamper-evident audit chain when opted
    in (``[reasoning_reward] audit_rewards``) -- provable learning: the evidence
    the system learns from becomes a signed row, not just a mutable verdict.
    Ordinary backend failures remain fail-soft; an explicit compliance refusal
    propagates and prevents unaudited reward evidence from entering learning."""
    from . import reasoning_reward

    if not reasoning_reward.audit_rewards_enabled():
        return
    from .audit import EventKind, audit_event

    payload = reward.to_audit_summary()
    try:
        from .matter_context import current_matter_context

        matter = current_matter_context()
    except Exception:  # pragma: no cover - optional outside a governed run
        matter = None
    if matter is not None:
        payload["matter_id"] = matter.matter_id
    audit_event(
        EventKind.VERIFICATION_REWARD,
        agent="verifier",
        **payload,
    )


async def verify_final(
    brief: str,
    proposal: str,
    llm: LLM,
    budget: Budget | None = None,
    *,
    proposer_model: str | None = None,
) -> VerifierVerdict:
    """Verify a FINAL answer with the run's one pinned provider/model.

    The structured rubric and scalar paths may differ in parsing, but both make
    exactly one provider call to the same configured destination.
    """
    if _structured_verify_enabled():
        return await verify_proposal_structured(
            brief, proposal, llm, budget, proposer_model=proposer_model,
        )
    return await verify_proposal(
        brief, proposal, llm, budget, proposer_model=proposer_model,
    )
