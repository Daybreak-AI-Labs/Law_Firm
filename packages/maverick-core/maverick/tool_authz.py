"""One authorization gate for tool dispatch outside the agent loop.

``Agent._run_tool`` runs an eleven-gate chain before any tool executes. It is
genuinely hardened -- but it is not the only path to the tool registry. A census
found four dispatch sites and exactly one of them gated:

===============================================  ==========================
site                                             status
===============================================  ==========================
``agent.py`` (``self.tools.run``)                the full eleven-gate chain
``flow/execution.py`` (``reg.run``)              partial: tool-policy only
``workflow.py`` (``registry.run``)               partial: tool-policy only
===============================================  ==========================

The flow path is live -- bound at ``automation_queue`` -- and reaches every
registered connector, including high-risk ones (stripe, gmail, s3, salesforce,
sap, workday). It checked ``action_tool_policy_error`` (unsafe list plus a
risk-classification requirement) and nothing else: no shield scan, no
governance policy, and **no audit row**.

That last omission is the one that matters most for the platform's central
claim. The audit record asserts that *no recorded action violated the policy
envelope*. An action dispatched through an ungated path is
not recorded, so it satisfies that sentence trivially. The claim was not false
so much as **unfalsifiable**, and an unfalsifiable governance claim is worth
less than none: a hostile auditor cannot attack it, which is precisely why it
cannot be trusted.

This module does not attempt to relocate the agent's chain -- that chain reads
agent context (quarantine, per-agent capability attenuation, autonomy servo,
rehearsal twin, hooks) that does not exist at a flow node. It applies the
subset that is meaningful without an agent, and, critically, it records the
dispatch on the same signed chain so the envelope claim has something to be
false about.

Deliberately NOT covered here, so nobody reads this as more than it is:
per-agent capability attenuation, the autonomy servo, the rehearsal twin,
compartment quarantine, and PreToolUse hooks. Those need an agent. A flow node
is therefore governed less tightly than an agent tool call, and
:func:`gate_summary` states that in words for the operator rather than leaving
it to be inferred from an absence.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: Dispatch sites permitted to call the registry directly, enforced by
#: ``maverick.dispatch_contract``. Everything else must come through here.
SANCTIONED_DISPATCH = (
    "packages/maverick-core/maverick/agent.py",
    "packages/maverick-core/maverick/tool_authz.py",
)


def _audit_summary(value: Any, limit: int = 200) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def authorize(
    tool: str,
    params: dict | None = None,
    *,
    origin: str,
    goal_id: int | None = None,
    principal: str | None = None,
    risk: str | None = None,
) -> str | None:
    """Authorize a non-agent tool dispatch. ``None`` allows; a string denies.

    The returned string is the operator/model-facing refusal and is deliberately
    non-leaky -- it names the rule, never the policy internals.

    ``origin`` identifies the dispatch site (``"flow"``, ``"workflow"``) and
    lands in the audit row, so an auditor reading the chain can tell an agent
    tool call apart from a flow action rather than having to infer it.

    Ordered so that deny wins and the cheapest check runs first.
    """
    params = params or {}

    # 1. Tool policy: unsafe-by-construction, or not risk-classified by an
    #    operator. Runtime repeats this even though the graph validator ran it,
    #    because a forged or stale graph must not bypass validation.
    from .flow.ir import action_tool_policy_error

    if policy_error := action_tool_policy_error(tool):
        _record_denial(tool, origin, goal_id, "tool_policy", policy_error)
        return f"ERROR: {policy_error}"

    # 2. Shield: the tool name and its rendered params are model- or
    #    template-derived, so they are attacker-reachable. This is the same
    #    screen the agent applies at its tool sink.
    from . import shield_policy

    payload = f"{tool} {params}"
    if (reason := shield_policy.scan_block(payload)) is not None:
        _record_denial(tool, origin, goal_id, "shield", reason)
        return f"ERROR: BLOCKED by Shield: {reason}"

    # 3. Org governance policy (deny / require-human). Default-open for
    #    non-enterprise installs, exactly as on the agent path.
    try:
        from .governance import Decision, evaluate

        verdict = evaluate(tool, risk=risk)
        if verdict.decision is Decision.DENY:
            _record_denial(tool, origin, goal_id, verdict.rule, verdict.reason)
            return f"ERROR: DENIED by org policy ({verdict.rule}): {verdict.reason}"
        if verdict.decision is Decision.REQUIRE_HUMAN:
            # A flow node runs unattended by construction; there is nobody to
            # ask. Refusing is the only honest outcome -- silently downgrading
            # a require-human action to auto-run is the failure this gate is
            # here to prevent.
            _record_denial(tool, origin, goal_id, verdict.rule,
                           "requires human approval; a flow node runs unattended")
            return (
                f"ERROR: BLOCKED by org policy ({verdict.rule}): this action "
                "requires human sign-off and cannot run unattended in a flow. "
                "Route it through an agent node or an approval step."
            )
    except ImportError:  # pragma: no cover -- governance is optional
        pass

    # 4. Record the dispatch on the signed chain. This is what makes the
    #    audit record's policy-envelope claim falsifiable for flow actions.
    from .audit import EventKind, audit_event

    audit_event(
        EventKind.TOOL_CALL, agent=f"{origin}:action", goal_id=goal_id,
        name=tool, origin=origin, principal=principal,
        input_summary=_audit_summary(params),
    )
    return None


def _record_denial(tool: str, origin: str, goal_id: int | None,
                   rule: str, reason: str) -> None:
    from .audit import EventKind, audit_event

    audit_event(
        EventKind.GOVERNANCE_DENIED, agent=f"{origin}:action", goal_id=goal_id,
        tool=tool, origin=origin, rule=rule, reason=reason,
    )


def gate_summary() -> dict[str, list[str]]:
    """What this gate does and does not check, for the operator and the docs.

    Published rather than implied. An absence in a governance product is a
    claim, and an unstated absence is the kind a reader fills in optimistically.
    """
    return {
        "enforced": [
            "tool policy (unsafe list + operator risk classification)",
            "shield scan of the tool name and rendered params",
            "org governance policy (deny)",
            "org governance policy (require-human -> refused, not downgraded)",
            "signed audit row for both dispatch and denial",
        ],
        "not_enforced_needs_an_agent": [
            "per-agent capability attenuation",
            "per-call tool-token exchange",
            "autonomy servo (run-trust risk ceiling)",
            "rehearsal twin / learned guardrails",
            "compartment quarantine seals",
            "PreToolUse hooks",
        ],
    }


__all__ = ["authorize", "gate_summary", "SANCTIONED_DISPATCH"]
