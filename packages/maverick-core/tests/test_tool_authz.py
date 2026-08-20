"""Non-agent tool dispatch is gated, and the gate is honest about its limits.

``Agent._run_tool`` runs eleven gates. Three other dispatch sites existed and
only checked ``action_tool_policy_error`` -- no shield, no governance policy,
and no audit row -- on a path bound at ``automation_queue`` that reaches every
registered connector.

The audit row is the load-bearing part. ``policy_envelope`` in the attestation
bundle asserts that *no recorded action violated the envelope*. An unrecorded
dispatch satisfies that trivially, which makes the claim unfalsifiable rather
than merely incomplete -- and an unfalsifiable governance claim is worth less
than none, because a hostile auditor has nothing to attack.
"""

from __future__ import annotations

import ast

import pytest
from maverick import dispatch_contract, tool_authz


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / "home"))


def _records(monkeypatch) -> list[tuple]:
    """Capture audit writes through the package entry point the repo patches."""
    import maverick.audit

    seen: list[tuple] = []
    monkeypatch.setattr(maverick.audit, "record",
                        lambda kind, **kw: seen.append((kind, kw)) or True)
    return seen


# -- the gate denies, and records the denial -------------------------------

def test_unclassified_tool_is_denied(monkeypatch) -> None:
    seen = _records(monkeypatch)
    denial = tool_authz.authorize("totally_made_up_tool", {}, origin="flow")
    assert denial and "unclassified" in denial
    kinds = [k for k, _ in seen]
    assert any("governance_denied" in str(k) for k in kinds), kinds


def test_denial_records_the_origin_so_an_auditor_can_tell_paths_apart(
        monkeypatch) -> None:
    seen = _records(monkeypatch)
    tool_authz.authorize("made_up", {}, origin="workflow", goal_id=42)
    payloads = [kw for _, kw in seen]
    assert any(p.get("origin") == "workflow" for p in payloads), payloads
    assert any(p.get("goal_id") == 42 for p in payloads), payloads


def test_shield_block_is_enforced_and_recorded(monkeypatch) -> None:
    from maverick import shield_policy

    monkeypatch.setattr(tool_authz, "_record_denial", tool_authz._record_denial)
    monkeypatch.setattr(shield_policy, "scan_block", lambda text: "prompt injection")
    monkeypatch.setattr("maverick.flow.ir.action_tool_policy_error", lambda t: "")
    seen = _records(monkeypatch)
    denial = tool_authz.authorize("http_get", {"url": "x"}, origin="flow")
    assert denial and "Shield" in denial
    assert any("prompt injection" in str(kw) for _, kw in seen)


def test_governance_deny_is_enforced(monkeypatch) -> None:
    from maverick import governance

    monkeypatch.setattr("maverick.flow.ir.action_tool_policy_error", lambda t: "")
    monkeypatch.setattr("maverick.shield_policy.scan_block", lambda text: None)
    monkeypatch.setattr(
        governance, "evaluate",
        lambda action, **kw: governance.Verdict(
            decision=governance.Decision.DENY, reason="blocked by org",
            rule="deny_actions"))
    _records(monkeypatch)
    denial = tool_authz.authorize("wire_transfer", {}, origin="flow")
    assert denial and "DENIED by org policy" in denial


def test_require_human_is_refused_not_silently_downgraded(monkeypatch) -> None:
    """A flow node runs unattended; there is nobody to ask.

    Letting a require-human action auto-run because no approver is reachable is
    the precise failure this gate exists to stop.
    """
    from maverick import governance

    monkeypatch.setattr("maverick.flow.ir.action_tool_policy_error", lambda t: "")
    monkeypatch.setattr("maverick.shield_policy.scan_block", lambda text: None)
    monkeypatch.setattr(
        governance, "evaluate",
        lambda action, **kw: governance.Verdict(
            decision=governance.Decision.REQUIRE_HUMAN, reason="high value",
            rule="require_human_min_risk"))
    _records(monkeypatch)
    denial = tool_authz.authorize("wire_transfer", {}, origin="flow")
    assert denial is not None
    assert "cannot run unattended" in denial


# -- the gate allows, and records the dispatch -----------------------------

def test_an_allowed_dispatch_is_recorded_on_the_chain(monkeypatch) -> None:
    """The missing row that made policy_envelope unfalsifiable."""
    from maverick import governance

    monkeypatch.setattr("maverick.flow.ir.action_tool_policy_error", lambda t: "")
    monkeypatch.setattr("maverick.shield_policy.scan_block", lambda text: None)
    monkeypatch.setattr(
        governance, "evaluate",
        lambda action, **kw: governance.Verdict(
            decision=governance.Decision.ALLOW, reason="", rule=""))
    seen = _records(monkeypatch)
    assert tool_authz.authorize("http_get", {"url": "x"}, origin="flow") is None
    kinds = [str(k) for k, _ in seen]
    assert any("tool_call" in k for k in kinds), kinds
    payload = next(kw for k, kw in seen if "tool_call" in str(k))
    assert payload["name"] == "http_get"
    assert payload["origin"] == "flow"


def test_params_are_truncated_in_the_audit_summary(monkeypatch) -> None:
    from maverick import governance

    monkeypatch.setattr("maverick.flow.ir.action_tool_policy_error", lambda t: "")
    monkeypatch.setattr("maverick.shield_policy.scan_block", lambda text: None)
    monkeypatch.setattr(
        governance, "evaluate",
        lambda action, **kw: governance.Verdict(
            decision=governance.Decision.ALLOW, reason="", rule=""))
    seen = _records(monkeypatch)
    tool_authz.authorize("http_get", {"blob": "x" * 5000}, origin="flow")
    payload = next(kw for k, kw in seen if "tool_call" in str(k))
    assert len(payload["input_summary"]) <= 201


# -- the gate states its own limits ----------------------------------------

def test_gate_summary_publishes_what_it_does_not_check() -> None:
    """An unstated absence in a governance product is read optimistically."""
    s = tool_authz.gate_summary()
    assert s["enforced"] and s["not_enforced_needs_an_agent"]
    missing = " ".join(s["not_enforced_needs_an_agent"])
    for expected in ("capability", "autonomy", "quarantine", "hooks"):
        assert expected in missing, expected


# -- the CI gate -----------------------------------------------------------

def test_dispatch_contract_is_clean_on_the_real_tree() -> None:
    hits, inspected = dispatch_contract.scan()
    # The firm-only prune removed hundreds of nonlegal dispatch modules while
    # retaining a substantial scanner scope and the known-site controls below.
    assert inspected >= 350, inspected
    assert dispatch_contract.violations(hits) == []


def test_dispatch_contract_still_sees_every_known_site() -> None:
    """Anti-blindness: if the detector stops matching, a clean run means nothing."""
    hits, _ = dispatch_contract.scan()
    files = {h["file"] for h in hits}
    assert "packages/maverick-core/maverick/agent.py" in files
    assert "packages/maverick-core/maverick/flow/execution.py" in files
    assert len(hits) >= 3, hits


def test_dispatch_contract_uses_repository_paths_on_every_os(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    source = root / "packages" / "demo" / "agent.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def go(tools):\n    return tools.run('fixed', {})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dispatch_contract, "REPO_ROOT", root)

    assert dispatch_contract.scan_file(source)[0]["file"] == (
        "packages/demo/agent.py"
    )


def test_dispatch_contract_ignores_generated_package_copies(
    tmp_path, monkeypatch,
) -> None:
    root = tmp_path / "repo"
    generated = root / "packages" / "demo" / "build" / "lib" / "rogue.py"
    generated.parent.mkdir(parents=True)
    generated.write_text(
        "def go(reg):\n    return reg.run('stripe_charge', {})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dispatch_contract, "REPO_ROOT", root)
    monkeypatch.setattr(dispatch_contract, "SEARCH_ROOTS", ("packages",))

    hits, inspected = dispatch_contract.scan()
    assert hits == []
    assert inspected == 0


def test_dispatch_contract_flags_a_new_ungated_site(tmp_path) -> None:
    """The committed mutant: a fifth dispatch site added without a gate."""
    f = tmp_path / "rogue.py"
    f.write_text("def go(reg):\n    return reg.run('stripe_charge', {})\n",
                 encoding="utf-8")
    hits = dispatch_contract.scan_file(f)
    assert len(hits) == 1
    assert hits[0]["gated"] is False
    assert dispatch_contract.violations(hits) == hits


def test_dispatch_contract_accepts_a_gated_site(tmp_path) -> None:
    f = tmp_path / "ok.py"
    f.write_text(
        "def go(reg):\n"
        "    if denial := authorize('stripe_charge', {}, origin='flow'):\n"
        "        return denial\n"
        "    return reg.run('stripe_charge', {})\n",
        encoding="utf-8")
    hits = dispatch_contract.scan_file(f)
    assert len(hits) == 1 and hits[0]["gated"] is True
    assert dispatch_contract.violations(hits) == []


def test_gating_a_different_function_does_not_launder_the_dispatch(tmp_path) -> None:
    """Scope is the enclosing function, not the module.

    'authorize is called somewhere in this file' is not a statement about this
    call, and accepting it would make the gate trivially satisfiable.
    """
    f = tmp_path / "sneaky.py"
    f.write_text(
        "def elsewhere():\n"
        "    authorize('x', {}, origin='flow')\n"
        "\n"
        "def go(reg):\n"
        "    return reg.run('stripe_charge', {})\n",
        encoding="utf-8")
    hits = dispatch_contract.scan_file(f)
    assert len(hits) == 1 and hits[0]["gated"] is False


def test_dispatch_contract_refuses_an_empty_scope(monkeypatch, capsys) -> None:
    monkeypatch.setattr(dispatch_contract, "SEARCH_ROOTS", ("no_such_root",))
    assert dispatch_contract.main(["--ci"]) == 2
    assert "inspected 0 files" in capsys.readouterr().err


def test_dispatch_contract_refuses_to_pass_if_it_has_gone_blind(
        monkeypatch, capsys) -> None:
    """A clean tree and a broken detector must not look the same."""
    monkeypatch.setattr(dispatch_contract, "scan", lambda roots=None: ([], 900))
    assert dispatch_contract.main(["--ci"]) == 2
    assert "no longer recognises" in capsys.readouterr().err


# -- the wiring actually landed at both call sites -------------------------

def test_the_live_dispatch_site_calls_the_gate() -> None:
    """flow/execution.py is the production path -- bound at automation_queue."""
    path = "packages/maverick-core/maverick/flow/execution.py"
    src = (dispatch_contract.REPO_ROOT / path).read_text(encoding="utf-8")
    names = {
        (c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", ""))
        for c in ast.walk(ast.parse(src)) if isinstance(c, ast.Call)
    }
    assert "authorize" in names, path


def test_workflow_run_has_no_production_caller() -> None:
    """The premise of workflow.py's exemption, checked rather than asserted.

    Workflow.run takes a caller-supplied registry, so its tools need not be in
    the deployment's risk classifier and the flow gate would reject every
    in-process registry instead of protecting anything. That is only acceptable
    while nothing in production calls it. The moment something does, this fails
    and the site must be gated -- which is the point at which gating it would
    actually mean something.
    """
    assert dispatch_contract.unreachable_exemption_broken() == []


def test_the_unreachable_exemption_check_can_actually_fail(monkeypatch) -> None:
    """Negative control: an exemption that cannot rot cannot be trusted."""
    monkeypatch.setattr(
        dispatch_contract, "UNREACHABLE_EXEMPT",
        {"packages/maverick-core/maverick/workflow.py": ("Agent", "run")})
    assert dispatch_contract.unreachable_exemption_broken() != []
