"""Round 4 rate-lever tests: localization pre-pass, retry-on-empty, and the
governance-tax parser. Each lever is exercised in isolation with no network and
no provider key."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest


def _load(mod_name: str, rel: str):
    p = Path(__file__).resolve().parents[1] / "benchmarks" / rel
    spec = importlib.util.spec_from_file_location(mod_name, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Deliverable 3: localization pre-pass
# --------------------------------------------------------------------------

def test_localize_extract_terms_dedupes_and_drops_stopwords():
    sb = _load("benchmarks_swe_bench", "swe_bench.py")
    brief = (
        "The `CalcEngine.compute_total` method returns the wrong value when "
        "tallying widget_count. This should return the sum, not the difference."
    )
    terms = sb._localize_extract_terms(brief)
    # Identifiers survive; bare stopwords ('should', 'return', 'value') do not.
    assert "CalcEngine.compute_total" in terms
    assert "widget_count" in terms
    assert "should" not in terms
    assert "return" not in terms
    assert "value" not in terms
    assert len(terms) <= 12
    # No duplicates (case-insensitive).
    lowered = [t.lower() for t in terms]
    assert len(lowered) == len(set(lowered))


def test_localization_context_finds_source_excludes_tests(tmp_path):
    sb = _load("benchmarks_swe_bench", "swe_bench.py")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "engine.py").write_text(
        "class CalcEngine:\n"
        "    def compute_total(self, widget_count):\n"
        "        return widget_count - 1\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_engine.py").write_text(
        "from pkg.engine import CalcEngine\n"
        "def test_compute_total():\n"
        "    assert CalcEngine().compute_total(3) == 3\n",
        encoding="utf-8",
    )
    brief = "`CalcEngine.compute_total` mishandles widget_count; fix compute_total."
    out = sb._maverick_localization_context(brief, tmp_path)
    assert "LIKELY-RELEVANT SOURCE FILES" in out
    assert "pkg/engine.py" in out
    assert "class CalcEngine" in out
    # tests/ is excluded from the source scan.
    assert "tests/test_engine.py" not in out


def test_localization_context_disabled_by_env(tmp_path, monkeypatch):
    sb = _load("benchmarks_swe_bench", "swe_bench.py")
    monkeypatch.setenv("MAVERICK_LOCALIZE", "0")
    assert sb._maverick_localization_context("anything CalcEngine", tmp_path) == ""


def test_localization_context_never_raises_on_bad_workdir():
    sb = _load("benchmarks_swe_bench", "swe_bench.py")
    assert sb._maverick_localization_context("x CalcEngine", Path("/no/such/dir")) == ""
    assert sb._maverick_localization_context("x", None) == ""  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Deliverable 2: retry-on-empty in llm_proposer
# --------------------------------------------------------------------------

class _FakeRow:
    def __init__(self, predicted_patch: str, cost: float):
        self.predicted_patch = predicted_patch
        self.cost_dollars = cost
        self.tokens_out = 0
        self.outcome = "no-diff" if not predicted_patch else "success"
        self.extra = {"run_text": ""}


def _load_governed():
    return _load("benchmarks_swebench_governed", "swebench_governed.py")


def test_retry_on_empty_uses_second_attempt_and_sums_cost(tmp_path, monkeypatch):
    gov = _load_governed()

    # Provider-key guard: pretend a provider is configured.
    monkeypatch.setenv("MAVERICK_PROVIDER_READY", "1")
    monkeypatch.setenv("MAVERICK_RETRY_ON_EMPTY", "1")
    monkeypatch.setenv("MAVERICK_SWEBENCH_FORENSICS", str(tmp_path / "forensics"))

    briefs: list[str] = []
    rows = [_FakeRow("", 1.0), _FakeRow("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n", 2.0)]
    calls = {"n": 0}

    def fake_run_maverick(instance_id, brief, **kwargs):
        briefs.append(brief)
        r = rows[calls["n"]]
        calls["n"] += 1
        return r

    # Inject a stub `swe_bench` module so llm_proposer's `from swe_bench import
    # run_maverick` picks up the fake.
    stub = types.ModuleType("swe_bench")
    stub.run_maverick = fake_run_maverick
    monkeypatch.setitem(sys.modules, "swe_bench", stub)

    # Stub the git/sandbox helpers so nothing touches a real repo.
    monkeypatch.setattr(gov, "_git_head", lambda repo: "basesha")
    monkeypatch.setattr(gov, "_worktree_diff", lambda repo: "")  # force prose-diff path
    monkeypatch.setattr(gov, "_hard_reset", lambda repo, sha: None)
    # The host-exec consent gate fires before these stubs matter; nothing
    # real executes in this test (run_maverick + sandbox are faked).
    monkeypatch.setattr(gov, "_ensure_untrusted_agent_sandbox", lambda: None)
    monkeypatch.setattr(gov, "_point_sandbox_at", lambda repo, require_container=False: (lambda: None))

    inst = gov.Instance(
        instance_id="acme__widget-1", repo_path=tmp_path,
        fail_to_pass=["tests/t.py::test_x"], pass_to_pass=[],
        gold_patch="", brief="fix the widget", test_patch="",
    )
    result = gov.llm_proposer(inst)

    assert calls["n"] == 2, "second attempt must run when the first is empty"
    assert "b\n" in result and result.startswith("--- a/x.py")
    # The retry brief carries the addendum.
    assert gov._RETRY_ON_EMPTY_ADDENDUM in briefs[1]
    assert gov._RETRY_ON_EMPTY_ADDENDUM not in briefs[0]
    # Forensics sidecar cost = attempt1 + attempt2.
    sidecar = tmp_path / "forensics" / "acme__widget-1.json"
    rec = json.loads(sidecar.read_text())
    assert rec["cost_dollars"] == pytest.approx(3.0)


def test_retry_on_empty_disabled_runs_once(tmp_path, monkeypatch):
    gov = _load_governed()
    monkeypatch.setenv("MAVERICK_PROVIDER_READY", "1")
    monkeypatch.setenv("MAVERICK_RETRY_ON_EMPTY", "0")
    monkeypatch.setenv("MAVERICK_SWEBENCH_FORENSICS", str(tmp_path / "forensics"))

    calls = {"n": 0}

    def fake_run_maverick(instance_id, brief, **kwargs):
        calls["n"] += 1
        return _FakeRow("", 1.0)

    stub = types.ModuleType("swe_bench")
    stub.run_maverick = fake_run_maverick
    monkeypatch.setitem(sys.modules, "swe_bench", stub)
    monkeypatch.setattr(gov, "_git_head", lambda repo: "basesha")
    monkeypatch.setattr(gov, "_worktree_diff", lambda repo: "")
    monkeypatch.setattr(gov, "_hard_reset", lambda repo, sha: None)
    # The host-exec consent gate fires before these stubs matter; nothing
    # real executes in this test (run_maverick + sandbox are faked).
    monkeypatch.setattr(gov, "_ensure_untrusted_agent_sandbox", lambda: None)
    monkeypatch.setattr(gov, "_point_sandbox_at", lambda repo, require_container=False: (lambda: None))

    inst = gov.Instance(
        instance_id="acme__widget-2", repo_path=tmp_path,
        fail_to_pass=["tests/t.py::test_x"], pass_to_pass=[],
        gold_patch="", brief="fix", test_patch="",
    )
    out = gov.llm_proposer(inst)
    assert calls["n"] == 1, "retry disabled must not re-run"
    assert out == ""


def test_llm_proposer_default_shell_consent_requires_container_and_restores(tmp_path, monkeypatch):
    gov = _load_governed()
    monkeypatch.setenv("MAVERICK_PROVIDER_READY", "1")
    monkeypatch.delenv("MAVERICK_CONSENT_MODE", raising=False)
    monkeypatch.setenv("MAVERICK_SWEBENCH_FORENSICS", str(tmp_path / "forensics"))

    stub = types.ModuleType("swe_bench")
    stub.run_maverick = lambda *args, **kwargs: _FakeRow("--- a/x.py\n+++ b/x.py\n", 0.0)
    monkeypatch.setitem(sys.modules, "swe_bench", stub)
    monkeypatch.setattr(gov, "_git_head", lambda repo: "basesha")
    monkeypatch.setattr(gov, "_worktree_diff", lambda repo: "")
    monkeypatch.setattr(gov, "_hard_reset", lambda repo, sha: None)
    # The host-exec consent gate fires before these stubs matter; nothing
    # real executes in this test (run_maverick + sandbox are faked).
    monkeypatch.setattr(gov, "_ensure_untrusted_agent_sandbox", lambda: None)

    seen: list[bool] = []

    def fake_point(repo, require_container=False):
        seen.append(require_container)
        return lambda: None

    monkeypatch.setattr(gov, "_point_sandbox_at", fake_point)
    inst = gov.Instance("acme__widget-3", tmp_path, ["tests/t.py::test_x"], [])

    assert gov.llm_proposer(inst).startswith("--- a/x.py")
    assert seen == [True]
    assert "MAVERICK_CONSENT_MODE" not in os.environ


def test_llm_proposer_explicit_consent_does_not_force_container(tmp_path, monkeypatch):
    gov = _load_governed()
    monkeypatch.setenv("MAVERICK_PROVIDER_READY", "1")
    monkeypatch.setenv("MAVERICK_CONSENT_MODE", "ask")
    monkeypatch.setenv("MAVERICK_SWEBENCH_FORENSICS", str(tmp_path / "forensics"))

    stub = types.ModuleType("swe_bench")
    stub.run_maverick = lambda *args, **kwargs: _FakeRow("--- a/y.py\n+++ b/y.py\n", 0.0)
    monkeypatch.setitem(sys.modules, "swe_bench", stub)
    monkeypatch.setattr(gov, "_git_head", lambda repo: "basesha")
    monkeypatch.setattr(gov, "_worktree_diff", lambda repo: "")
    monkeypatch.setattr(gov, "_hard_reset", lambda repo, sha: None)
    # The host-exec consent gate fires before these stubs matter; nothing
    # real executes in this test (run_maverick + sandbox are faked).
    monkeypatch.setattr(gov, "_ensure_untrusted_agent_sandbox", lambda: None)

    seen: list[bool] = []
    monkeypatch.setattr(
        gov, "_point_sandbox_at",
        lambda repo, require_container=False: (seen.append(require_container) or (lambda: None)),
    )
    inst = gov.Instance("acme__widget-4", tmp_path, ["tests/t.py::test_x"], [])

    assert gov.llm_proposer(inst).startswith("--- a/y.py")
    assert seen == [False]
    assert os.environ["MAVERICK_CONSENT_MODE"] == "ask"


# --------------------------------------------------------------------------
# Family circuit breaker: a repo family whose env is broken fails identically
# instance after instance at full paid price (observed live: 3 consecutive
# pytest EMPTYs at best-of-N Opus price). After --max-family-failures paid
# non-resolves in one family, its remaining instances are skipped at $0.
# --------------------------------------------------------------------------

def _write_manifest(tmp_path, ids):
    rows = [
        json.dumps({
            "instance_id": iid, "repo_path": str(tmp_path),
            "fail_to_pass": ["tests/t.py::test_x"], "pass_to_pass": [],
            "brief": "fix it",
        })
        for iid in ids
    ]
    m = tmp_path / "m.jsonl"
    m.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return m


def test_family_key_strips_trailing_issue_number():
    gov = _load_governed()
    assert gov._family("pytest-dev__pytest-10051") == "pytest-dev__pytest"
    assert gov._family("psf__requests-2931") == "psf__requests"


def test_family_breaker_skips_dead_family_but_not_healthy_ones(tmp_path, monkeypatch, capsys):
    gov = _load_governed()
    manifest = _write_manifest(tmp_path, [
        "acme__widget-1", "acme__widget-2", "acme__widget-3",
        "acme__widget-4", "other__pkg-1",
    ])
    # Paid-style proposer that always comes back empty (the dead-env signature).
    attempted: list[str] = []

    def fake_proposer(inst):
        attempted.append(inst.instance_id)
        return ""

    monkeypatch.setitem(gov.PROPOSERS, "fake", fake_proposer)
    # Free pre-gate says "gradable" -- the live failure mode: pre-gate passes,
    # the agent still can't produce anything, money burns.
    monkeypatch.setattr(gov, "_env_pregate", lambda inst, workroot, timeout: "")
    # Spend suffix: a forensics sidecar for the first instance shows its cost
    # inline on the scoreboard, so a paid EMPTY is visible as money in the log.
    forensics = tmp_path / "forensics"
    forensics.mkdir()
    (forensics / "acme__widget-1.json").write_text(
        json.dumps({"cost_dollars": 1.5}), encoding="utf-8")
    monkeypatch.setenv("MAVERICK_SWEBENCH_FORENSICS", str(forensics))

    rc = gov.main([
        "--manifest", str(manifest), "--proposer", "fake",
        "--keys", str(tmp_path), "--ledger", str(tmp_path / "ledger.json"),
        "--max-family-failures", "2",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    # Two paid attempts on the dead family, then the breaker; the healthy
    # family is still attempted.
    assert attempted == ["acme__widget-1", "acme__widget-2", "other__pkg-1"]
    assert out.count("[SKIP]") == 2
    assert "family breaker ($0 spent)" in out
    assert "family-skipped: 2" in out
    assert "[spent ~$1.50]" in out          # cost visible on the paid EMPTY line


def test_family_breaker_never_gates_the_oracle(tmp_path, capsys):
    gov = _load_governed()
    manifest = _write_manifest(tmp_path, [
        "acme__widget-1", "acme__widget-2", "acme__widget-3",
    ])
    # Oracle with no gold patch -> EMPTY on every instance; even with the
    # tightest breaker the oracle must sweep ALL of them (preflight computes
    # the winnable set; skipping would corrupt the denominator).
    rc = gov.main([
        "--manifest", str(manifest), "--proposer", "oracle",
        "--keys", str(tmp_path), "--ledger", str(tmp_path / "ledger.json"),
        "--max-family-failures", "1",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[SKIP]" not in out
    assert out.count("[EMPTY]") == 3
    assert "[spent" not in out              # oracle lines stay suffix-free


# --------------------------------------------------------------------------
# Deliverable 5: governance-tax parser
# --------------------------------------------------------------------------

_CANNED_LOG = """\
==============================================================================
  SWE-BENCH UNDER GOVERNANCE   proposer=llm  n=5
==============================================================================
  [PASS]  acme__a-1                                   resolved under governance
  [FAIL]  acme__b-2                                   gate refused: capability widened
  [CHEAT]  acme__c-3                                  boundary refused: edits tests
  [PASS]  acme__d-4                                   resolved under governance
  [FAIL]  acme__e-5                                   not resolved: candidate 0.500
==============================================================================
"""


def test_governance_tax_parse_log():
    gt = _load("benchmarks_governance_tax", "governance_tax.py")
    counts = gt.parse_log(_CANNED_LOG)
    assert counts == {"resolved": 2, "cheat": 1, "gate_refused": 1}
    text = gt.report(counts, ledger_count=2)
    assert "raw-capability resolves:           4" in text
    assert "governed resolves:                 2" in text
    assert "governance tax:                    2" in text


def test_governance_tax_handles_empty_and_ledger(tmp_path):
    gt = _load("benchmarks_governance_tax", "governance_tax.py")
    assert gt.parse_log("") == {"resolved": 0, "cheat": 0, "gate_refused": 0}
    assert gt.count_ledger(None) is None
    assert gt.count_ledger(str(tmp_path / "nope.json")) is None
    ledger = tmp_path / "l.json"
    ledger.write_text(json.dumps([{"id": "a"}, {"id": "b"}]), encoding="utf-8")
    assert gt.count_ledger(str(ledger)) == 2
    # Malformed ledger -> None, no raise.
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert gt.count_ledger(str(bad)) is None
