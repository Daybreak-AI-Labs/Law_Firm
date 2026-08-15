"""Pack output contract: the *consumption* side of a domain pack -- what the
specialist delivers, to whom, how often, and what sign-off it needs. Additive
to the schema, so a pack without an ``[output]`` block behaves exactly as
before (a prose result with no declared consumer)."""
from __future__ import annotations

from maverick.domain import (
    DomainProfile,
    OutputContract,
    WorkflowStep,
    _coerce_output,
    available_domains,
    declared_prompt_gate,
    deliverable_release_allowed,
    enforced_gate,
    lint_profile,
    load_domain,
    overlay_profile,
)


class TestOutputSchema:
    def test_absent_block_is_empty_prose_contract(self, tmp_path):
        # The default: no [output] => today's behaviour, a prose deliverable.
        f = tmp_path / "p.toml"
        f.write_text('name = "p"\nallow_tools = ["read_file"]\n')
        prof = load_domain(f)
        assert prof.output == OutputContract()
        assert prof.output.shape == "prose"
        assert prof.output.consumers == []
        assert prof.output.gate is None

    def test_loads_output_block_from_toml(self, tmp_path):
        f = tmp_path / "p.toml"
        f.write_text(
            'name = "p"\n'
            'allow_tools = ["read_file"]\n'
            '[output]\n'
            'shape = "forecast"\n'
            'deliverable = "13-week cash forecast"\n'
            'consumers = ["fpa_analyst", "treasurer"]\n'
            'cadence = "weekly"\n'
            'gate = "review"\n'
        )
        out = load_domain(f).output
        assert out.shape == "forecast"
        assert out.deliverable == "13-week cash forecast"
        assert out.consumers == ["fpa_analyst", "treasurer"]
        assert out.cadence == "weekly"
        assert out.gate == "review"

    def test_coerce_is_forgiving(self):
        # Non-table => empty; stray non-list consumers => []; missing keys default.
        assert _coerce_output("nonsense") == OutputContract()
        out = _coerce_output({"shape": "table", "consumers": "fpa"})
        assert out.shape == "table"
        assert out.consumers == []          # a string is not a list of roles
        assert out.deliverable == ""        # missing key defaults
        assert out.gate is None

    def test_malformed_output_does_not_break_discovery(self, tmp_path):
        # A scalar where a table is expected must not raise at load time.
        f = tmp_path / "p.toml"
        f.write_text('name = "p"\nallow_tools = ["read_file"]\noutput = "oops"\n')
        prof = load_domain(f)             # must not raise
        assert prof.output == OutputContract()


class TestOutputOverlay:
    def test_overlay_preserves_base_contract(self):
        # Overlaying an unrelated field must NOT wipe the base's deliverable.
        base = DomainProfile(
            name="x", allow_tools=["read_file"], max_risk="low",
            output=OutputContract(shape="forecast", deliverable="cash forecast",
                                  consumers=["fpa_analyst"]),
        )
        merged = overlay_profile(base, {"description": "Tuned for ACME."})
        assert merged.description == "Tuned for ACME."          # patched
        assert merged.output.shape == "forecast"                # inherited
        assert merged.output.deliverable == "cash forecast"     # inherited
        assert merged.output.consumers == ["fpa_analyst"]       # inherited

    def test_overlay_can_patch_output(self):
        base = DomainProfile(name="x", allow_tools=["read_file"],
                             output=OutputContract(shape="prose"))
        merged = overlay_profile(base, {"output": {"shape": "report",
                                                   "consumers": ["risk_officer"]}})
        assert merged.output.shape == "report"
        assert merged.output.consumers == ["risk_officer"]


class TestLintOutput:
    def _ok_base(self, **kw):
        return DomainProfile(
            name="x", persona="x" * 250, allow_tools=["read_file"],
            deny_tools=["shell"], max_risk="low", knowledge_sources=["x"],
            description="d", **kw,
        )

    def test_valid_contract_has_no_output_warning(self):
        p = self._ok_base(output=OutputContract(
            shape="forecast", deliverable="cash forecast",
            consumers=["fpa_analyst"], gate="review"))
        _, warnings = lint_profile(p)
        assert not any("output" in w for w in warnings)

    def test_unknown_shape_warns(self):
        p = self._ok_base(output=OutputContract(shape="hologram"))
        _, warnings = lint_profile(p)
        assert any("output.shape" in w for w in warnings)

    def test_unknown_gate_warns(self):
        p = self._ok_base(output=OutputContract(deliverable="d",
                                                consumers=["x"], gate="rubber-stamp"))
        _, warnings = lint_profile(p)
        assert any("output.gate" in w for w in warnings)

    def test_deliverable_without_consumers_warns(self):
        p = self._ok_base(output=OutputContract(deliverable="a report"))
        _, warnings = lint_profile(p)
        assert any("no consumers" in w for w in warnings)

    def test_intermediate_gate_is_labeled_prompt_only(self):
        p = self._ok_base(
            workflow=[
                WorkflowStep("approve inputs", gate="approval"),
                WorkflowStep("draft output"),
            ],
        )
        _, warnings = lint_profile(p)
        assert any("prompt-declared only" in warning for warning in warnings)


class TestEffectiveReleaseGate:
    def test_terminal_playbook_gate_is_enforced_without_output_gate(self):
        p = DomainProfile(
            name="generated",
            allow_tools=["read_file"],
            max_risk="low",
            workflow=[
                WorkflowStep("draft"),
                WorkflowStep("human approval", gate="approval"),
            ],
            output=OutputContract(deliverable="forecast", consumers=["cfo"]),
        )

        assert declared_prompt_gate(p) == "approval"
        assert enforced_gate(p) == "approval"
        assert deliverable_release_allowed(p, None) is False
        assert deliverable_release_allowed(p, {"decision": "rejected"}) is False
        assert deliverable_release_allowed(p, {"decision": "approved"}) is True

    def test_intermediate_gate_is_not_misreported_as_enforced(self):
        p = DomainProfile(
            name="generated",
            allow_tools=["read_file"],
            max_risk="low",
            workflow=[
                WorkflowStep("human approval", gate="approval"),
                WorkflowStep("draft"),
            ],
        )

        assert declared_prompt_gate(p) == "approval"
        assert enforced_gate(p) is None
        assert deliverable_release_allowed(p, None) is True


class TestBuiltinContract:
    def test_legal_briefs_declares_its_deliverable(self):
        # The proof pack: a drafted brief declares its consumption side.
        out = available_domains()["legal_briefs"].output
        assert out.shape == "prose"
        assert out.deliverable == "draft brief with table of authorities"
        assert "attorney" in out.consumers
        assert out.cadence == "on-demand"
        assert out.gate == "review"


class TestFinanceSuiteContracts:
    """The finance suite declares contracts across its towers, so the persona
    inbox is populated (not just the one proof pack)."""

    def _finance_with_contract(self):
        return {n: p for n, p in available_domains().items()
                if n.startswith("finance_") and (p.output.deliverable or p.output.consumers)}

    def test_many_finance_packs_declare_deliverables(self):
        declared = self._finance_with_contract()
        assert len(declared) >= 8, f"only {len(declared)} finance contracts"

    def test_declared_finance_contracts_lint_clean(self):
        for name, p in self._finance_with_contract().items():
            errors, warnings = lint_profile(p)
            assert not errors, (name, errors)
            assert not [w for w in warnings if "output" in w], (name, warnings)

    def test_consumer_roles_stay_a_consistent_vocabulary(self):
        # A bounded, shared role set keeps the inbox's role filter meaningful --
        # guard against a typo'd / one-off role drifting in.
        allowed = {"controller", "fpa_analyst", "treasurer", "tax_analyst",
                   "auditor", "risk_officer", "credit_officer", "cfo",
                   "accounting_manager", "ir_lead", "internal_auditor",
                   "tax_manager", "payroll_manager"}
        roles = {r for p in self._finance_with_contract().values() for r in p.output.consumers}
        assert roles and roles <= allowed, f"unexpected roles: {roles - allowed}"


class TestInsuranceSuiteContracts:
    """The insurance suite declares contracts across claims, underwriting,
    reinsurance, actuarial, and compliance -- so the inbox covers ins_ too."""

    _ALLOWED = {"underwriter", "actuary", "claims_adjuster", "claims_manager",
                "reinsurance_analyst", "compliance_officer", "agency_manager",
                "siu_investigator", "premium_auditor", "risk_officer", "controller"}

    def _with_contract(self):
        return {n: p for n, p in available_domains().items()
                if n.startswith("ins_") and (p.output.deliverable or p.output.consumers)}

    def test_many_insurance_packs_declare_deliverables(self):
        assert len(self._with_contract()) >= 3

    def test_declared_insurance_contracts_lint_clean(self):
        for name, p in self._with_contract().items():
            errors, warnings = lint_profile(p)
            assert not errors, (name, errors)
            assert not [w for w in warnings if "output" in w], (name, warnings)

    def test_insurance_roles_stay_a_consistent_vocabulary(self):
        roles = {r for p in self._with_contract().values() for r in p.output.consumers}
        assert roles and roles <= self._ALLOWED, f"unexpected roles: {roles - self._ALLOWED}"


class TestLegalSuiteContracts:
    """The legal suite is the practice: every seat declares who consumes its
    work product, so a drafted brief, memo or redline lands in a real inbox
    instead of nowhere."""

    def _with_contract(self):
        return {n: p for n, p in available_domains().items()
                if n.startswith("legal_") and (p.output.deliverable or p.output.consumers)}

    def test_every_legal_pack_declares_a_deliverable(self):
        packs = {n: p for n, p in available_domains().items() if n.startswith("legal_")}
        missing = sorted(n for n, p in packs.items() if not p.output.deliverable)
        assert not missing, f"legal packs with no declared deliverable: {missing}"
        assert len(self._with_contract()) >= 76

    def test_declared_legal_contracts_lint_clean(self):
        for name, p in self._with_contract().items():
            errors, warnings = lint_profile(p)
            assert not errors, (name, errors)
            assert not [w for w in warnings if "output" in w], (name, warnings)

    # Internal-workflow seats: their output is firm-internal routing or
    # knowledge upkeep, not a work product that leaves the office, so they
    # carry no sign-off gate. Everything else does.
    _UNGATED = {"legal_intake", "legal_km"}

    def test_every_legal_deliverable_routes_to_a_human_reviewer(self):
        # Nothing a legal seat drafts is self-approving: the consumption side
        # always names at least one human role, and every work product that
        # reaches a client, a counterparty or a court carries a gate. This is
        # the fork's central guarantee -- the attorney reviews the work.
        for name, p in self._with_contract().items():
            assert p.output.consumers, f"{name}: deliverable with no consumer"
            if name in self._UNGATED:
                continue
            assert p.output.gate in ("review", "approval"), f"{name}: gate={p.output.gate!r}"

    def test_the_ungated_allowlist_stays_honest(self):
        # Guard the exception list itself: if one of these grows a gate, or a
        # new pack is quietly added to the set, this fails rather than letting
        # the allowlist rot into a hole in the rule above.
        packs = available_domains()
        for name in self._UNGATED:
            assert name in packs, f"{name}: ungated allowlist names a missing pack"
            assert packs[name].output.gate is None, (
                f"{name}: now carries a gate -- drop it from _UNGATED")
