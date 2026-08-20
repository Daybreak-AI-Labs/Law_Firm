"""Governance audit surface for the specialist roster.

``maverick domains-lint`` answers "is each pack well-formed?". This answers the
enterprise question: "what can these agents do, and what stops them?" — the
auditable inventory a GRC reviewer, security assessment, or procurement needs.

For each pack it records the governance posture the other modules enforce:
the compartment seal, the capability envelope (risk ceiling + whether any
state-mutating tool is reachable), the hard refusals it carries, the human
sign-off gate on its deliverable, and the reasoning tier. Pure functions: the
CLI renders them, tests assert the roster-wide invariants (e.g. no drafting
agent can reach a shell), and the JSON export feeds an external GRC system.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .domain import (
    DomainProfile,
    available_domains,
    declared_prompt_gate,
    enforced_gate,
    suite_for,
)
from .domain_refusals import refusals_for

# State-mutating / host-control tools a drafting agent must never reach.
_HOST_CONTROL = ("shell", "write_file", "code_exec", "computer", "browser")
_FILE_CODE_MUTATORS = ("apply_patch", "str_replace_editor", "ast_edit")
# Irreversible business actions; a pack denying these shows its segregation-of-
# duties posture ("agents draft; humans post, pay, file, certify").
_IRREVERSIBLE = (
    "release_payment", "wire_transfer", "ach_send", "run_payroll",
    "post_journal_entry", "file_return", "file_tax_return", "file_with_sec",
    "place_trade", "execute_fx_trade", "vendor_master_change", "send_payment",
    "approve_expense", "approve_po", "approve_vendor", "set_credit_limit",
    "send_invoice", "self_edit", "release_payroll_payment",
    "close_period", "edit_chart_of_accounts", "write_off_balance",
    "reimburse", "edit_employee_bank_details", "create_order_instruction",
    "delete_order_instruction", "dispose_asset", "remit_tax",
)


@dataclass
class PackAudit:
    name: str
    suite: str | None
    compartment: str
    max_risk: str | None
    effort: str | None
    is_builder: bool
    reachable_dangerous: list[str] = field(default_factory=list)
    denied_irreversible: list[str] = field(default_factory=list)
    n_refusals: int = 0
    refusals: list[str] = field(default_factory=list)
    # ``human_gate`` remains as a compatibility alias, but now means only a
    # persisted release control.  Prompt-only declarations are separate so a
    # GRC export cannot accidentally claim a control that does not execute.
    human_gate: str | None = None
    declared_prompt_gate: str | None = None
    enforced_gate: str | None = None
    deliverable: str = ""
    consumers: list[str] = field(default_factory=list)
    knowledge_sources: list[str] = field(default_factory=list)


def audit_profile(profile: DomainProfile) -> PackAudit:
    """The governance posture of one pack (pure)."""
    allow = set(profile.allow_tools)
    # Builder status is a roster classification, not something a drafting pack
    # can acquire merely by adding shell/code_exec to its allowlist.
    suite = suite_for(profile.name)
    is_builder = (
        suite == "product_engineering"
        and bool(allow & {"shell", "code_exec"})
    )
    cap = profile.capability(f"agent:{profile.name}")
    dangerous = sorted(
        set(_HOST_CONTROL) | set(_FILE_CODE_MUTATORS) | set(_IRREVERSIBLE)
    )
    reachable = [t for t in dangerous if cap.permits(t)]
    denied = [t for t in _IRREVERSIBLE if t in set(profile.deny_tools)]
    refusals = refusals_for(profile.name, profile.refuse)
    declared = declared_prompt_gate(profile)
    enforced = enforced_gate(profile)
    return PackAudit(
        name=profile.name,
        suite=suite,
        compartment=profile.compartment,
        max_risk=profile.max_risk,
        effort=profile.effort,
        is_builder=is_builder,
        reachable_dangerous=reachable,
        denied_irreversible=denied,
        n_refusals=len(refusals),
        refusals=refusals,
        human_gate=enforced,
        declared_prompt_gate=declared,
        enforced_gate=enforced,
        deliverable=profile.output.deliverable,
        consumers=list(profile.output.consumers),
        knowledge_sources=list(profile.knowledge_sources),
    )


def audit_roster(domains: dict[str, DomainProfile] | None = None) -> list[PackAudit]:
    """Audit every discoverable pack (built-in + operator overrides)."""
    domains = domains if domains is not None else available_domains()
    return [audit_profile(domains[name]) for name in sorted(domains)]


def summarize(audits: list[PackAudit]) -> dict:
    """Roster-wide governance summary -- the headline numbers for an assessor."""
    total = len(audits)
    builders = [a for a in audits if a.is_builder]
    non_builders = [a for a in audits if not a.is_builder]
    gate_rank = {None: 0, "review": 1, "approval": 2}
    return {
        "packs": total,
        "suites": len({a.suite for a in audits if a.suite}),
        "builders": len(builders),
        # The load-bearing safety invariant: no drafting agent reaches a mutator.
        "drafting_agents_reaching_a_mutator": sum(
            1 for a in non_builders if a.reachable_dangerous),
        # Compatibility headline, now intentionally counts *enforced* gates.
        "packs_with_human_gate": sum(1 for a in audits if a.enforced_gate),
        "packs_with_declared_prompt_gate": sum(
            1 for a in audits if a.declared_prompt_gate
        ),
        "packs_with_unenforced_prompt_gate": sum(
            1
            for a in audits
            if gate_rank.get(a.declared_prompt_gate, 0)
            > gate_rank.get(a.enforced_gate, 0)
        ),
        "packs_with_refusals_beyond_universal": sum(
            1 for a in audits if a.n_refusals > 2),
        "packs_with_effort_tier": sum(1 for a in audits if a.effort),
        "packs_with_deliverable": sum(1 for a in audits if a.deliverable),
    }


def to_json(audits: list[PackAudit]) -> dict:
    """A machine-readable audit document for ingestion into a GRC system."""
    return {"summary": summarize(audits), "packs": [asdict(a) for a in audits]}


__all__ = ["PackAudit", "audit_profile", "audit_roster", "summarize", "to_json"]
