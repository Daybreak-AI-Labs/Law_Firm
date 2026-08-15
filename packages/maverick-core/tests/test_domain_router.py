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
     {"legal_hold", "legal_lit_hold"}, "legal"),
    ("we were served with a subpoena, how do we respond",
     {"legal_subpoena"}, "legal"),
    ("collect and review the documents for discovery",
     {"legal_ediscovery", "legal_ediscovery_modern_data"}, "legal"),
    ("check the citations and quotations in this brief",
     {"legal_citation"}, "legal"),
    ("research the case law on this issue and write it up",
     {"legal_research"}, "legal"),
    ("draft the motion and supporting memorandum",
     {"legal_briefs", "legal_litigation_mgmt"}, "legal"),
    ("file a trademark application for the client's brand",
     {"legal_trademark"}, "legal"),
    ("docket the upcoming patent prosecution deadlines",
     {"legal_ip_docket", "legal_patent"}, "legal"),
    ("negotiate the data processing agreement with this vendor",
     {"legal_dpa_negotiation", "legal_vendor_contract_review"}, "legal"),
    ("the client had a data breach, what are the notification obligations",
     {"legal_data_breach_legal", "legal_privacy"}, "legal"),
    ("advise on GDPR obligations for the European rollout",
     {"legal_gdpr_dpo", "legal_uk_gdpr", "legal_privacy"}, "legal"),
    ("form a new LLC and draft the operating agreement",
     {"legal_entity_mgmt", "legal_contract_drafting"}, "legal"),
    ("draft the minutes for the annual board meeting",
     {"legal_board", "exec_minutes"}, "legal"),
    ("settle the case and paper the settlement agreement",
     {"legal_settlement", "legal_negotiation"}, "legal"),
    ("what open source licenses are in this codebase",
     {"legal_open_source_license"}, "legal"),
    ("respond to an IRS notice for a client", {"tax_irs_notice"}, "tax"),
    ("abstract the key terms out of this commercial lease",
     {"re_lease_abstraction"}, "real_estate"),
    ("pursue subrogation against the at-fault party",
     {"ins_subro"}, "insurance"),
    ("reconcile the month end general ledger close",
     {"finance_gl_close"}, "finance"),
    ("chase the outstanding client invoices", {"finance_ar"}, "finance"),
    ("is this worker properly classified as a contractor",
     {"hr_contractor_class", "hr_employment_law"}, "hr"),
    ("run the incident response tabletop exercise",
     {"sec_ir_drill", "sec_tabletop_artifacts"}, "security_ops"),
    ("submit a public records request to the agency",
     {"pubsec_records_request", "gov_foia_support"}, "public_sector"),
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


# --- Embedding / hybrid path (validated with an injected fake embedder, since
#     fastembed is optional and absent in CI's lightweight subset) -----------
import re as _re  # noqa: E402

from maverick.domain import DomainProfile  # noqa: E402
from maverick.domain_router import EmbeddingRouter, _blend  # noqa: E402

# A controllable "semantic" embedder: maps synonym sets to shared concept dims,
# so paraphrases that share no surface tokens still embed close (what a real
# sentence-transformer buys us over lexical).
_CONCEPTS = {
    "separation": {"terminate", "termination", "fire", "layoff", "offboard",
                   "offboarding", "rif"},
    "nda": {"nda", "confidentiality", "nondisclosure", "secrecy"},
    "payable": {"invoice", "payable", "vendor", "bill"},
}


def _fake_embed(texts):
    out = []
    for t in texts:
        toks = set(_re.findall(r"[a-z]+", (t or "").lower()))
        out.append([1.0 if (syns & toks) else 0.0 for syns in _CONCEPTS.values()])
    return out


def _toy_domains():
    return {
        "hr_employment_law": DomainProfile(
            name="hr_employment_law", description="employee offboarding and exit",
            persona="You run offboarding and termination logistics for departing staff."),
        "legal_nda_desk": DomainProfile(
            name="legal_nda_desk", description="confidentiality agreements",
            persona="You process NDAs and confidentiality and secrecy obligations."),
        "finance_ap": DomainProfile(
            name="finance_ap", description="accounts payable",
            persona="You match each vendor invoice and stage the payable."),
    }


def test_embedding_router_ranks_paraphrase_without_shared_tokens():
    r = EmbeddingRouter(_toy_domains(), embed_fn=_fake_embed)
    assert r.available
    # "let someone go" shares no surface tokens with the offboarding pack, but
    # the synonym concept ("fire") makes it the top semantic match.
    scores = r.score_all("we need to fire a staff member")
    assert max(scores, key=scores.get) == "hr_employment_law"


def test_embedding_router_unavailable_without_a_model():
    r = EmbeddingRouter(_toy_domains(), embed_fn=lambda _t: None)
    assert not r.available
    assert r.score_all("anything") == {}


def test_blend_reranks_toward_semantic():
    lexical = {"a": 10.0, "b": 1.0}     # lexical loves a
    semantic = {"a": 0.1, "b": 1.0}     # semantic loves b
    blended = _blend(lexical, semantic, alpha=0.8)
    assert blended["b"] > blended["a"]  # high alpha -> semantic wins


def test_blend_with_no_semantic_is_lexical_unchanged():
    lexical = {"a": 3.0, "b": 1.0}
    assert _blend(lexical, {}, alpha=0.6) == lexical


def test_rank_specialists_falls_back_to_lexical_when_no_embedder(monkeypatch):
    # With the embedder forced unavailable, hybrid == the lexical ranking, so
    # the benchmark still holds (no regression from adding the embedding path).
    import maverick.domain_router as dr
    monkeypatch.setattr(dr, "EmbeddingRouter",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    hybrid = [n for n, _ in rank_specialists("review an NDA", k=5, domains=_PACKS)]
    lexical = [n for n, _ in _ROUTER.rank("review an NDA", k=5)]
    assert hybrid == lexical


def test_rank_specialists_caches_explicit_domain_embedding_index(monkeypatch):
    # list_specialists passes an explicit enabled-domain roster; that path must
    # reuse the module cache so repeated user queries do not re-embed every pack.
    import maverick.domain_router as dr

    calls = []

    def counting_embed(texts):
        calls.append(len(texts))
        return [[1.0] for _ in texts]

    class CountingEmbeddingRouter(dr.EmbeddingRouter):
        def __init__(self, domains):
            super().__init__(domains, embed_fn=counting_embed)

    monkeypatch.setattr(dr, "_LEX", None)
    monkeypatch.setattr(dr, "_EMB", None)
    monkeypatch.setattr(dr, "_KEY", None)
    monkeypatch.setattr(dr, "EmbeddingRouter", CountingEmbeddingRouter)

    dr.rank_specialists("review an NDA", k=3, domains=_PACKS)
    dr.rank_specialists("draft a press release", k=3, domains=_PACKS)

    assert calls == [len(_PACKS), 1, 1]
