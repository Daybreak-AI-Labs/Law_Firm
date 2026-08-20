"""Specialist routing: lexical retrieval over the firm's pack roster.

The router is a deterministic pre-filter that narrows the roster to a relevant
shortlist for a task, so the orchestrator picks from ~10 candidates instead of
guessing a suite and browsing it. These tests are the routing BENCHMARK: a
labeled set of realistic task phrasings (no pack-name leakage) with the set of
acceptable specialists for each, asserting recall@10 -- the pre-filter goal --
and suite accuracy stay above a floor, so a future pack/persona edit can't
silently degrade routing.
"""
from __future__ import annotations

from maverick.domain import builtin_dir, load_domains, suite_for
from maverick.domain_router import DomainRouter, rank_specialists

_PACKS = load_domains(builtin_dir())
_ROUTER = DomainRouter(_PACKS)

# (task phrased as a lawyer would, acceptable specialist set, expected suite).
# Multiple packs are acceptable on purpose: several seats are legitimately right
# for one task, so exact top-1 would understate the router.
_CASES: list[tuple[str, set[str], str]] = [
    ("review this NDA and flag the liability cap and indemnity clauses",
     {"legal_contract_review", "legal_nda_desk"}, "legal"),
    ("run a conflicts check before we take on this new client",
     {"legal_conflicts"}, "legal"),
    ("open a new matter and collect what we need from the client",
     {"legal_matter_intake", "legal_intake", "legal_matter_mgmt"}, "legal"),
    ("put a litigation hold in place and notify the custodians",
     {"legal_hold"}, "legal"),
    ("we were served with a subpoena, how do we respond",
     {"legal_subpoena"}, "legal"),
    ("collect and review the documents for discovery",
     {"legal_ediscovery"}, "legal"),
    ("check the citations and quotations in this brief",
     {"legal_citation"}, "legal"),
    ("research the case law on this issue and write it up",
     {"legal_research"}, "legal"),
    ("draft the motion and supporting memorandum",
     {"legal_briefs", "legal_litigation_mgmt"}, "legal"),
    ("negotiate the data processing agreement with this vendor",
     {"legal_dpa_negotiation", "legal_vendor_contract_review"}, "legal"),
    ("the client had a data breach, what are the notification obligations",
     {"legal_data_breach_legal", "legal_privacy"}, "legal"),
    ("form a new LLC and draft the operating agreement",
     {"legal_entity_mgmt", "legal_contract_drafting"}, "legal"),
    ("draft the minutes for the annual board meeting",
     {"legal_board"}, "legal"),
    ("settle the case and paper the settlement agreement",
     {"legal_settlement", "legal_negotiation"}, "legal"),
    ("review this vendor services agreement before signature",
     {"legal_vendor_contract_review", "legal_contract_review"}, "legal"),
    ("draft and negotiate the software subscription agreement",
     {"legal_saas_agreement", "legal_contract_drafting"}, "legal"),
    ("analyze this employee handbook and discrimination claim",
     {"legal_employment"}, "legal"),
    ("extract the client's notice dates and renewal obligations",
     {"legal_obligations", "legal_contract_review"}, "legal"),
    ("prepare research for a VA disability appeal",
     {"legal", "legal_research", "legal_briefs"}, "legal"),
    ("draft a custody motion in this family-law matter",
     {"legal", "legal_briefs", "legal_litigation_mgmt"}, "legal"),
    ("research probate procedure for the estate petition",
     {"legal", "legal_research", "legal_briefs"}, "legal"),
    ("review the residential purchase contract and closing documents",
     {"legal", "legal_contract_review", "legal_contract_drafting"}, "legal"),
    ("analyze exposure in this privacy class action",
     {"legal_privacy_litigation", "legal_privacy"}, "legal"),
    ("prepare an interview and evidence plan for the internal investigation",
     {"legal_investigations"}, "legal"),
]


def _metrics():
    n = len(_CASES)
    hit1 = rec10 = suite1 = 0
    for query, acceptable, suite in _CASES:
        ranked = [name for name, _ in _ROUTER.rank(query, k=10)]
        if ranked and ranked[0] in acceptable:
            hit1 += 1
        if any(name in acceptable for name in ranked):
            rec10 += 1
        if ranked and suite_for(ranked[0]) == suite:
            suite1 += 1
    return n, hit1, rec10, suite1


def test_recall_at_10_meets_the_prefilter_floor():
    # The pre-filter's job: a valid specialist is in the shortlist it surfaces.
    n, _, rec10, _ = _metrics()
    assert rec10 / n >= 0.80, f"recall@10 = {rec10}/{n}"


def test_top1_and_suite_accuracy_meet_floor():
    n, hit1, _, suite1 = _metrics()
    assert hit1 / n >= 0.50, f"hit@1 = {hit1}/{n}"
    assert suite1 / n >= 0.60, f"suite@1 = {suite1}/{n}"


def test_rank_is_deterministic_and_bounded():
    a = _ROUTER.rank("review this NDA", k=5)
    b = _ROUTER.rank("review this NDA", k=5)
    assert a == b              # pure / stable
    assert len(a) <= 5
    assert all(s > 0 for _, s in a)  # no zero-score padding


def test_offroster_query_returns_nothing():
    # A query with no roster vocabulary returns an empty shortlist, not noise.
    assert _ROUTER.rank("zzzqqq xqzptl", k=10) == []


def test_module_level_cache_matches_fresh_index():
    ranked = rank_specialists("draft a press release", k=5, domains=_PACKS)
    assert [n for n, _ in ranked][:3] == [
        n for n, _ in _ROUTER.rank("draft a press release", k=5)][:3]


def test_module_level_router_never_initializes_an_embedding_backend(monkeypatch):
    """Roster routing must remain lexical and never acquire a model."""
    import maverick.domain_router as dr

    monkeypatch.setattr(dr, "_LEX", None)
    monkeypatch.setattr(dr, "_KEY", None)
    ranked = dr.rank_specialists("review an NDA", k=5, domains=_PACKS)
    assert ranked == _ROUTER.rank("review an NDA", k=5)
