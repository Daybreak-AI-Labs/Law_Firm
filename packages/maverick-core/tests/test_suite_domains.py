"""Built-in legal-practice domain packs: roster + safety invariants.

Every shipped agent analyzes or drafts legal work but never acts on the world, so
it must load, carry a persona plus a sealed legal compartment, and run under a
read-only, low/medium-risk capability envelope.  This test is the contract that
keeps a new or edited pack from quietly broadening the firm's product or granting a
dangerous tool.

The roster carries no *builder* packs (coding agents with sandbox shell/code_exec) --
the enterprise Product&Engineering suite is not part of the firm's fork. The builder
invariant below is retained anyway so that adding one later is caught by the same
floor: an agent may build, but never modify its own runtime/safety/controls.
"""
from __future__ import annotations

from maverick.domain import (
    builtin_dir,
    lint_profile,
    load_domains,
    render_workflow_prompt,
)

_BUILTIN = load_domains(builtin_dir())


def _by_prefix(pre: str) -> dict:
    return {k: v for k, v in _BUILTIN.items() if k.startswith(pre)}


_EXPECTED_LEGAL_ROSTER = {
    "legal",
    "legal_board",
    "legal_briefs",
    "legal_citation",
    "legal_conflicts",
    "legal_contract_drafting",
    "legal_contract_intake",
    "legal_contract_review",
    "legal_data_breach_legal",
    "legal_dpa_negotiation",
    "legal_ediscovery",
    "legal_employment",
    "legal_entity_mgmt",
    "legal_hold",
    "legal_intake",
    "legal_investigations",
    "legal_km",
    "legal_litigation_mgmt",
    "legal_matter_intake",
    "legal_matter_mgmt",
    "legal_msa_playbook",
    "legal_nda_desk",
    "legal_negotiation",
    "legal_obligations",
    "legal_privacy",
    "legal_privacy_litigation",
    "legal_research",
    "legal_saas_agreement",
    "legal_settlement",
    "legal_subpoena",
    "legal_vendor_contract_review",
}
_LEGAL = {name: pack for name, pack in _BUILTIN.items()
          if name == "legal" or name.startswith("legal_")}
_SUITE = dict(_LEGAL)


def test_suites_present():
    # This fork intentionally ships an exact reviewed legal roster.  A new pack
    # is a product-scope change and must update this explicit contract.
    assert set(_BUILTIN) == _EXPECTED_LEGAL_ROSTER
    for non_law_prefix in ("tax_", "finance_", "sec_", "km_", "hr_", "exec_",
                           "re_", "ins_", "ops_", "mfg_", "util_"):
        assert not _by_prefix(non_law_prefix), non_law_prefix


def test_every_suite_pack_loads_with_persona_and_compartment():
    assert _SUITE, "no suite packs discovered"
    for name, p in _SUITE.items():
        assert p.name == name
        assert p.persona.strip(), f"{name}: empty persona"
        assert p.compartment, f"{name}: missing compartment"


def test_suite_packs_are_read_only_and_safe():
    for name, p in _SUITE.items():
        # A high ceiling is legitimate only for a spawn-router, which holds the
        # privileged PARENT grant so its children can attenuate down from it.
        if p.max_risk == "high":
            assert {"spawn_subagent", "spawn_swarm"} & set(p.allow_tools), (
                f"{name}: high risk but not a spawn-router")
        else:
            assert p.max_risk in ("low", "medium"), f"{name}: risk={p.max_risk!r}"
        cap = p.capability(f"agent:{name}")
        # Most seats read matter files; a few (e.g. tax_law_watch) are purely
        # web/knowledge research and never touch the filesystem. Where read_file
        # IS granted it must actually resolve -- a granted-but-blocked tool is a
        # silently broken pack.
        if "read_file" in p.allow_tools:
            assert cap.permits("read_file") is True, f"{name}: read_file granted but blocked"
        # Mutating / host-control tools must be unreachable (deny + whitelist + ceiling).
        for dangerous in ("shell", "write_file", "code_exec", "computer", "home_assistant"):
            assert cap.permits(dangerous) is False, f"{name}: {dangerous} reachable!"
        assert "shell" in p.deny_tools and "write_file" in p.deny_tools, (
            f"{name}: must explicitly deny shell + write_file"
        )


def test_legal_matter_packs_deny_external_web_search():
    # Legal packs bound to privileged matter data are sealed: they may search the
    # local knowledge base / CourtListener connectors, but must not send model-built
    # queries to external web-search providers.
    for name, p in _LEGAL.items():
        if "legal_matter" not in p.knowledge_sources:
            continue
        cap = p.capability(f"agent:{name}")
        assert "web_search" not in p.allow_tools, f"{name}: web_search allowed for legal matter"
        assert "web_search" in p.deny_tools, f"{name}: web_search must be denied"
        assert cap.permits("web_search") is False, f"{name}: web_search reachable!"


def test_suite_compartments_match_their_prefix():
    # A pack's compartment stays inside the legal namespace, so a Rung-2 seal
    # cannot accidentally target a retired cross-industry suite.
    for name, p in _LEGAL.items():
        assert p.compartment == "legal" or p.compartment.startswith("legal_"), (
            f"{name}: compartment={p.compartment!r}"
        )


# Every built-in pack that is NOT a coding builder (no shell/code_exec in its
# allowlist) is read-only by design. The per-suite tests above cover the suites
# by name; this covers the WHOLE roster so a future edit anywhere that grants a
# drafting agent shell/write is caught.
_BUILDERS = {n: p for n, p in _BUILTIN.items()
             if "code_exec" in p.allow_tools or "shell" in p.allow_tools}
_NON_BUILDERS = {n: p for n, p in _BUILTIN.items() if n not in _BUILDERS}


def test_every_non_builder_pack_is_read_only_and_safe():
    assert set(_NON_BUILDERS) == _EXPECTED_LEGAL_ROSTER
    for name, p in _NON_BUILDERS.items():
        # A high ceiling is legitimate only for a spawn-router (it holds the
        # privileged PARENT grant so children can attenuate down); everything
        # else stays low/medium. Either way the mutators must be unreachable.
        if p.max_risk == "high":
            assert {"spawn_subagent", "spawn_swarm"} & set(p.allow_tools), (
                f"{name}: high risk but not a spawn-router")
        else:
            assert p.max_risk in ("low", "medium"), f"{name}: risk={p.max_risk!r}"
        cap = p.capability(f"agent:{name}")
        # Mutating / host-control tools must be unreachable for a drafting agent.
        for dangerous in ("shell", "write_file", "code_exec", "computer"):
            assert cap.permits(dangerous) is False, f"{name}: {dangerous} reachable!"


def test_every_non_builder_pack_denies_the_readonly_floor():
    # Defense-in-depth: the allowlist already excludes shell/write_file, but an
    # explicit deny is what survives a later allowlist edit. The whole roster
    # carries it, not just the seven suites with bespoke tests.
    for name, p in _NON_BUILDERS.items():
        assert "shell" in p.deny_tools, f"{name}: must explicitly deny shell"
        assert "write_file" in p.deny_tools, f"{name}: must explicitly deny write_file"


def test_every_builder_denies_self_edit():
    # The one floor a coding agent can never relax: an agent may build, never edit
    # its own runtime. The firm's roster ships no builders today, so this is a
    # forward guard -- it starts asserting the moment someone authors one.
    for name, p in _BUILDERS.items():
        cap = p.capability(f"agent:{name}")
        assert "self_edit" in p.deny_tools, f"{name}: builder must deny self_edit"
        assert cap.permits("self_edit") is False, f"{name}: self_edit reachable!"


# --- Roster-wide quality harness (pure: render + lint, no spawn) -------------
# These run over EVERY built-in pack so a future edit anywhere that breaks the
# contract, the tool integrity, or the lint gate is caught immediately.

_GATE_RANK = {None: 0, "review": 1, "approval": 2}


def test_whole_roster_lints_error_free():
    # The roster carries zero lint ERRORs at all times (empty allowlist, bad
    # max_risk, nameless/duplicate workflow steps would each fail).
    for name, p in _BUILTIN.items():
        errors, _ = lint_profile(p)
        assert errors == [], (name, errors)


def test_every_workflow_step_tool_is_in_allowlist():
    # A playbook may only hint tools the pack actually grants -- a step naming a
    # tool outside allow_tools is a dead reference (and a lint warning).
    for name, p in _BUILTIN.items():
        allow = set(p.allow_tools)
        for step in p.workflow:
            stray = [t for t in step.tools if t not in allow]
            assert not stray, f"{name}: step {step.name!r} names non-allowed {stray}"


def test_every_pack_renders_a_persona_and_playbook():
    # Every pack must produce a non-empty persona and, where it has a workflow,
    # render every step into the system prompt -- the spawn path depends on this.
    from maverick.domain_discipline import augment_persona
    for name, p in _BUILTIN.items():
        persona = augment_persona(p.name, p.persona)
        assert persona.strip(), f"{name}: empty persona after augmentation"
        wf = render_workflow_prompt(p.workflow)
        if p.workflow:
            assert "Workflow" in wf, f"{name}: workflow did not render"
            for step in p.workflow:
                assert step.name in wf, f"{name}: step {step.name!r} missing from render"


def test_deliverable_gate_not_lighter_than_playbook():
    # The deliverable's sign-off is never lighter than the human-handoff its own
    # playbook ends on (a 'review' deliverable from an 'approval' final step).
    for name, p in _BUILTIN.items():
        if p.output.deliverable and p.workflow:
            final = p.workflow[-1].gate
            assert _GATE_RANK.get(final, 0) <= _GATE_RANK.get(p.output.gate, 0), (
                f"{name}: output.gate={p.output.gate!r} < final step gate={final!r}")


def test_every_pack_declares_output_and_workflow():
    # Coverage guard: the consumption surface stays complete -- every pack keeps
    # a declared deliverable and a playbook (regression guard for the roster).
    for name, p in _BUILTIN.items():
        assert p.output.deliverable, f"{name}: missing [output] deliverable"
        assert p.workflow, f"{name}: missing [[workflow]] playbook"


_VALID_EFFORTS = {None, "low", "medium", "high", "xhigh", "max"}


def test_every_pack_effort_tier_is_valid():
    # A declared effort tier is always a real level (an invalid one is a lint
    # ERROR, but assert it across the roster too).
    for name, p in _BUILTIN.items():
        assert p.effort in _VALID_EFFORTS, f"{name}: effort={p.effort!r}"


def test_high_stakes_packs_carry_high_effort():
    # Right-sizing sentinels: severe-failure-cost judgment work runs deep. These
    # are the seats whose output goes to a court, a counterparty, or a client.
    for name in ("legal_briefs", "legal_contract_review", "legal_litigation_mgmt",
                 "legal_investigations", "legal_contract_drafting"):
        assert _BUILTIN[name].effort == "high", name


# A pack that names a known SaaS-connector tool must scope the vendor's host in
# allow_hosts, so the egress lock (enterprise.py) lets the connector seat reach
# its system instead of blocking it. Guards the wired packs from drift.
_VENDOR_HOSTS = {
    "billdotcom": "*.bill.com", "coupa": "*.coupa.com", "tipalti": "*.tipalti.com",
    "adp": "*.adp.com", "gusto": "*.gusto.com", "netsuite": "*.netsuite.com",
    "workday": "*.workday.com",
}


def test_vendor_connector_packs_scope_their_egress_hosts():
    for name, p in _BUILTIN.items():
        hosts = set(p.allow_hosts)
        for tool in p.allow_tools:
            for vendor, host in _VENDOR_HOSTS.items():
                if vendor in tool.lower():
                    assert host in hosts, (
                        f"{name}: names {vendor} tool {tool!r} but {host} not in "
                        "allow_hosts -- the egress lock would block the connector")


def test_non_law_workforce_and_physical_world_packs_are_absent():
    retired = ("hr_", "workforce_", "ops_", "mfg_", "util_")
    assert not {name for name in _BUILTIN if name.startswith(retired)}
