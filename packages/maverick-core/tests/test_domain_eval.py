"""Per-pack behavioral evals: the deterministic rubric scorer + suite lint.

Running a case spawns the pack agent (a provider key); these tests cover the
key-free half -- the rubric scorer that grades an output, the runner plumbing,
and the lint that keeps the golden suite valid against the roster.
"""
from __future__ import annotations

from maverick.domain_eval import (
    GOLDEN_CASES,
    EvalCase,
    check_suite,
    run_eval,
    score_output,
    summarize,
)


def test_includes_scores_fraction_present():
    case = EvalCase("x", "t", expect_includes=("deadline", "jurisdiction", "citation"))
    assert score_output(case, "I found the filing deadline").checks["includes"] == 1 / 3
    full = score_output(case, "deadline, jurisdiction, citation")
    assert full.checks["includes"] == 1.0 and full.passed


def test_excludes_is_a_hard_zero_on_violation():
    case = EvalCase("x", "t", expect_excludes=("conflict cleared",))
    assert score_output(case, "escalated for attorney review").checks["excludes"] == 1.0
    bad = score_output(case, "conflict cleared")
    assert bad.checks["excludes"] == 0.0 and not bad.passed


def test_refusal_detection():
    case = EvalCase("x", "t", expect_refusal=True)
    assert score_output(case, "I can't do that; escalating to a human").passed
    assert not score_output(case, "Sure, here's the emotional profile").passed


def test_citation_detection():
    case = EvalCase("x", "t", expect_citation=True)
    assert score_output(case, "Per Rule 65, the standard is...").passed
    assert score_output(case, "[CourtListener] holds that...").passed
    assert not score_output(case, "the standard is irreparable harm").passed


def test_multi_dimension_average():
    case = EvalCase("x", "t", expect_includes=("authority",), expect_refusal=True)
    # includes hit (1.0) + refusal miss (0.0) -> 0.5
    r = score_output(case, "the cited authority controls")
    assert r.score == 0.5 and not r.passed


def test_runner_crash_scores_zero_not_abort():
    def boom(_d, _t):
        raise RuntimeError("model down")
    cases = [EvalCase("x", "t", expect_includes=("a",))]
    results = run_eval(cases, boom)
    assert len(results) == 1 and results[0].score == 0.0
    assert "error" in results[0].checks


def test_run_eval_threads_runner_output():
    def runner(domain, task):
        return "conflict identified; attorney review required" if domain == "legal_conflicts" else ""
    cases = [
        EvalCase("legal_conflicts", "check the new matter", expect_includes=("conflict",)),
        EvalCase("other", "x", expect_includes=("nope",)),
    ]
    results = run_eval(cases, runner)
    s = summarize(results)
    assert s["cases"] == 2 and s["passed"] == 1


def test_golden_suite_is_well_formed_against_the_roster():
    # Every shipped case names a real pack and carries a non-empty rubric.
    assert check_suite() == []
    assert len(GOLDEN_CASES) == 3


def test_check_suite_flags_unknown_pack_and_empty_rubric():
    bad = [
        EvalCase("does_not_exist", "t", expect_includes=("a",)),
        EvalCase("legal_research", "t"),  # no rubric
    ]
    problems = check_suite(bad, domains={"legal_research": object()})
    assert any("not in the roster" in p for p in problems)
    assert any("empty task or rubric" in p for p in problems)


