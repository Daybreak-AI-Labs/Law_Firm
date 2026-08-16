"""Strictest-wins union of governance policies.

Pure policy algebra: given several :class:`~maverick.governance.Policy` objects,
produce the one policy that satisfies all of them at once. "Strictest wins"
means deny beats require-human, and the lowest risk floor and lowest dollar
threshold survive -- selecting more policies can only ever tighten the result,
never loosen it.

This lived inside the finance compliance-regime pack until that pack was
removed. Nothing here is finance-specific: it is used to union the profiles in
:mod:`maverick.compliance_profiles` (GDPR, the EU AI Act, and the rest), and it
is the natural place for any future union -- per-jurisdiction rules of
professional conduct, for instance, where a firm admitted in three states must
satisfy the strictest of the three.
"""
from __future__ import annotations

from .governance import Policy
from .safety.tool_risk import risk_rank


def _min_risk(a: str | None, b: str | None) -> str | None:
    """Strictest (lowest) risk floor of two -- the one that pauses/denies more."""
    if a is None:
        return b
    if b is None:
        return a
    return a if risk_rank(a) <= risk_rank(b) else b


def _min_thresholds(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """Per-action lowest (strictest) dollar threshold across two tables."""
    out = dict(a)
    for action, amount in b.items():
        out[action] = min(out[action], amount) if action in out else amount
    return out


def union_policies(policies) -> Policy:
    """Strictest-wins union of policies (deny > require-human; lowest floor/threshold)."""
    deny_actions: set[str] = set()
    require_human_actions: set[str] = set()
    deny_min_risk: str | None = None
    require_human_min_risk: str | None = None
    deny_above: dict[str, float] = {}
    require_human_above: dict[str, float] = {}
    require_fresh_human_approval = False
    for p in policies:
        deny_actions |= set(p.deny_actions)
        require_human_actions |= set(p.require_human_actions)
        deny_min_risk = _min_risk(deny_min_risk, p.deny_min_risk)
        require_human_min_risk = _min_risk(require_human_min_risk, p.require_human_min_risk)
        deny_above = _min_thresholds(deny_above, p.deny_above)
        require_human_above = _min_thresholds(require_human_above, p.require_human_above)
        require_fresh_human_approval = (
            require_fresh_human_approval or p.require_fresh_human_approval
        )
    # An action that is hard-denied need not also be listed as require-human --
    # drop it from both the require-human set AND its per-action threshold, or
    # the compiled policy contradicts itself (a hard-deny plus a require-human
    # floor for the same action).
    require_human_actions -= deny_actions
    require_human_above = {
        action: amount
        for action, amount in require_human_above.items()
        if action not in deny_actions
    }
    return Policy(
        deny_actions=frozenset(deny_actions),
        require_human_actions=frozenset(require_human_actions),
        deny_min_risk=deny_min_risk,
        require_human_min_risk=require_human_min_risk,
        deny_above=deny_above,
        require_human_above=require_human_above,
        require_fresh_human_approval=require_fresh_human_approval,
    )
