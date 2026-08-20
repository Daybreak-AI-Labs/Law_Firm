"""The entity graph: one spine across the record silos, typed time-bounded
edges with record provenance, and the three regulator queries."""
from __future__ import annotations

import time

import pytest
from maverick import entity_graph as eg

MATTER_ID = 101
OWNER = "user:alice"


@pytest.fixture(autouse=True)
def _fresh_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CONFIG", str(tmp_path / "config.toml"))
    from maverick import config
    config.reset_config_cache()
    yield
    config.reset_config_cache()


def _approved_vendor(subject="Acme Corp", decided_by="dpo") -> str:
    """A decided vendor_risk assessment through the REAL store."""
    from maverick.assessment import (
        AssessmentSession,
        decide_assessment,
        save_session,
    )
    s = AssessmentSession()
    s.restart("vendor_risk", subject)
    for q in s.template().questions:
        s.record(q.id, "yes")
    save_session(s)
    decide_assessment(s.id, "approved", decided_by=decided_by,
                      cadence_days=365)
    return s.id


def _dpa_review(vendor="Acme Corp", document="acme-dpa.pdf",
                reviewer="A. Novak"):
    from maverick.privacy_ops import review_dpa
    text = ("Processor shall process personal data only on documented "
            "instructions. Personnel are bound by confidentiality. "
            "Sub-processors require prior written authorisation.")
    return review_dpa(vendor, text, document_name=document,
                      reviewed_by=reviewer)


def _paper_review(vendor="Acme Corp", name="acme-dpa.docx"):
    from maverick.paper_review import review_paper
    from maverick.privacy_ops import save_paper_review
    text = ("DATA PROCESSING AGREEMENT under Article 28 GDPR.\n"
            "Processor may engage sub-processors at its sole discretion.\n")
    review = review_paper(text, vendor=vendor, document_name=name,
                          use_model=False)
    return save_paper_review(review, redline=b"PK-fake", reviewed_by="A. Novak",
                             redline_filename=f"{name}-redline.docx")


# --- the spine ------------------------------------------------------------

def test_entity_resolution_merges_spelling_variants():
    _dpa_review(vendor="Acme Corp.")
    _paper_review(vendor="acme corp")
    _approved_vendor(subject="ACME CORP")
    result = eg.rebuild()
    assert result["enabled"] and result["edges"] > 0
    # Three spellings, one vendor node -- and the dossier sees all three
    # record planes through any spelling.
    d = eg.dossier("Acme Corp")
    assert d["found"]
    kinds = {r["record_type"] for r in d["summary"]["reviews"]}
    assert kinds == {"dpa_review", "paper_review"}
    assert any(x["decision"] == "approved" for x in d["summary"]["decisions"])


def test_no_fuzzy_merging_distinct_vendors_stay_distinct():
    _dpa_review(vendor="Acme Corp")
    _dpa_review(vendor="Acme Corporation GmbH")
    eg.rebuild()
    a = eg.dossier("Acme Corp")
    b = eg.dossier("Acme Corporation GmbH")
    assert a["found"] and b["found"]
    # A wrong merge fabricates lineage; near-names must not collapse.
    assert {r["record_id"] for r in a["summary"]["reviews"]}.isdisjoint(
        {r["record_id"] for r in b["summary"]["reviews"]})


def test_every_edge_carries_record_provenance():
    _dpa_review()
    eg.rebuild()
    hood = eg.neighborhood("vendor", "Acme Corp")
    assert hood["found"] and hood["edges"]
    for e in hood["edges"]:
        assert e["record_type"] and e["record_id"], e
        assert e["valid_from"] > 0


def test_rebuild_is_idempotent_and_disagreement_free():
    _dpa_review()
    first = eg.rebuild()
    second = eg.rebuild()
    assert (first["entities"], first["edges"]) == (
        second["entities"], second["edges"])


# --- regulator query 1: why ------------------------------------------------

def test_why_walks_the_decision_back_to_its_evidence():
    _dpa_review()
    aid = _approved_vendor()
    result = eg.rebuild()
    assert result["edges"]
    out = eg.why("Acme Corp")
    assert out["found"] and out["decision"]
    steps = {c["step"] for c in out["chain"]}
    assert "decision" in steps and "evidence_in_force" in steps
    assert "clause_finding" in steps and "reviewer" in steps
    # Every hop cites a record id a human can pull.
    assert all(c["record_id"] for c in out["chain"])
    assert any(c["record_id"] == aid for c in out["chain"])
    assert "approved by dpo" in out["decision"]["what"]


def test_why_without_a_decision_says_so():
    _dpa_review(vendor="Globex")
    eg.rebuild()
    out = eg.why("Globex")
    assert out["found"] and out["decision"] is None
    assert "no decision" in out["note"]


# --- regulator query 2: point-in-time --------------------------------------

def test_as_of_reconstructs_what_was_known_at_signing():
    _paper_review()                        # v1
    between = time.time() + 0.01
    time.sleep(0.02)
    _paper_review(name="acme-dpa-v2.docx")   # v2 supersedes v1
    eg.rebuild()

    now_d = eg.dossier("Acme Corp")
    current = [r for r in now_d["summary"]["reviews"] if r["current"]]
    assert len(current) == 1               # only v2 is current now

    then_d = eg.dossier("Acme Corp", as_of=between)
    then_ids = {r["record_id"] for r in then_d["summary"]["reviews"]}
    assert len(then_ids) == 1              # at signing time, only v1 existed
    assert then_ids != {current[0]["record_id"]}
    # v1 was superseded later, but it and its clause findings were current at
    # the requested point in time.
    assert all(r["current"] for r in then_d["summary"]["reviews"])
    assert "subprocessors" in then_d["summary"]["open_clause_gaps"]


def test_superseded_review_edge_is_closed_not_deleted():
    _paper_review()
    time.sleep(0.02)
    _paper_review(name="acme-dpa-v2.docx")
    eg.rebuild()
    hood = eg.neighborhood("vendor", "Acme Corp")
    reviews = [e for e in hood["edges"] if e["rel"] == "reviews"
               and e["record_type"] == "paper_review"]
    assert len(reviews) == 2               # history preserved, never erased
    closed = [e for e in reviews if e["valid_to"] is not None]
    assert len(closed) == 1
    assert any(e["rel"] == "supersedes" for e in hood["edges"])


def test_parallel_dpa_documents_do_not_supersede_each_other():
    _dpa_review(document="acme-dpa.pdf")
    time.sleep(0.02)
    _dpa_review(document="acme-security-addendum.pdf")
    eg.rebuild()
    d = eg.dossier("Acme Corp")
    # Different documents run in parallel: both stay current.
    assert sum(1 for r in d["summary"]["reviews"] if r["current"]) == 2


# --- regulator query 3: blast radius ---------------------------------------

def test_blast_radius_from_a_clause_reaches_vendors_and_decisions():
    _paper_review()                        # finds subprocessors conflicting
    _approved_vendor()
    eg.rebuild()
    out = eg.blast_radius("clause", "subprocessors")
    assert out["found"]
    assert any(r["record_type"] == "paper_review" for r in out["records"])
    assert "Acme Corp" in out["vendors"]
    assert any(d["decision"] == "approved" for d in out["decisions"])


def test_blast_radius_unknown_entity_is_empty_not_error():
    eg.rebuild()
    out = eg.blast_radius("clause", "no_such_clause")
    assert out["found"] is False and out["records"] == []


# --- the dossier (retrieval) ------------------------------------------------

def test_dossier_surfaces_open_gaps_only_from_current_reviews():
    _paper_review()                        # v1: subprocessors conflicting
    eg.rebuild()
    d = eg.dossier("Acme Corp")
    assert "subprocessors" in d["summary"]["open_clause_gaps"]
    assert any("redline" in doc for doc in d["summary"]["documents"])
    assert "A. Novak" in d["summary"]["people"]


def test_cross_plane_edges_link_ai_registry_to_assessment():
    from maverick.privacy_ops import register_ai_system
    aid = _approved_vendor(subject="Acme CRM")
    register_ai_system("Acme CRM Copilot", "drafts sales emails",
                       provider="Acme Corp", owner="cio",
                       assessment_id=aid, registered_by="cio")
    eg.rebuild()
    hood = eg.neighborhood("vendor", "Acme Corp")
    rels = {e["rel"] for e in hood["edges"]}
    assert "provided_by" in rels
    two_hop = eg.neighborhood("assessment", aid, depth=1)
    assert any(e["rel"] == "from_assessment" for e in two_hop["edges"])


# --- posture ---------------------------------------------------------------

def test_disabled_knob_makes_everything_a_noop(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[entity_graph]\nenable = false\n", encoding="utf-8")
    monkeypatch.setenv("MAVERICK_CONFIG", str(cfg))
    from maverick import config
    config.reset_config_cache()
    assert eg.rebuild() == {"enabled": False, "entities": 0, "edges": 0}
    assert eg.dossier("Acme Corp")["enabled"] is False
    assert eg.stats() == {"enabled": False}


def test_empty_stores_build_an_empty_graph_without_raising():
    result = eg.rebuild()
    assert result["enabled"] and result["entities"] == 0
    assert eg.dossier("Nobody")["found"] is False
    assert eg.stats()["edges"] == 0


# --- ring two: the episodic plane (exact keys only) -------------------------

def _world(tmp_path):
    from maverick.world_model import WorldModel
    return WorldModel(tmp_path / "world.db")


def test_episodic_ring_derives_goal_agent_and_episode_edges(tmp_path, monkeypatch):
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    w = _world(tmp_path)
    gid = w.create_goal("Review Acme CRM vendor risk", domain="finance_ap")
    ep = w.start_episode(gid)
    w.end_episode(ep, "reviewed", "done", cost_dollars=0.42,
                  input_tokens=100, output_tokens=200)
    eg.rebuild()
    hood = eg.neighborhood("goal", str(gid))
    assert hood["found"]
    rels = {e["rel"] for e in hood["edges"]}
    assert "performed_by" in rels and "part_of" in rels
    agent = next(e for e in hood["edges"] if e["rel"] == "performed_by")
    assert agent["dst"] == {"kind": "agent", "name": "finance_ap"}
    episode = next(e for e in hood["edges"] if e["rel"] == "part_of")
    assert "done" in episode["detail"] and "$0.42" in episode["detail"]
    # The goal node reads like a goal, not a bare number.
    assert "Review Acme CRM" in hood["entity"]["name"]


def test_goal_titles_never_fabricate_vendor_edges(tmp_path, monkeypatch):
    # A goal TITLED with a vendor's name must not link to the vendor entity:
    # titles are prose, and lineage only comes from exact keys.
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    _dpa_review(vendor="Acme Corp")
    w = _world(tmp_path)
    w.create_goal("Acme Corp", domain="privacy_pia")
    eg.rebuild()
    hood = eg.neighborhood("vendor", "Acme Corp")
    assert hood["found"]
    assert all(e["src"]["kind"] != "goal" and e["dst"]["kind"] != "goal"
               for e in hood["edges"])


# --- ring two: the procedural plane (skill lineage) --------------------------

def _distill_skill(
    goal_ids, goal="reconcile vendor invoices", *,
    project_id=MATTER_ID, owner=OWNER,
):
    from maverick.skill.distillation_local import distill_and_save
    trajectories = [
        {"goal": goal, "goal_id": gid, "success": True,
         "tools": ["ledger_read"], "t": 100.0 + gid,
         "project_id": project_id, "owner": owner}
        for gid in goal_ids
    ]
    return distill_and_save(
        trajectories, project_id=project_id, owner=owner,
    )


def test_distiller_stamps_source_goal_ids_into_the_frontmatter(tmp_path, monkeypatch):
    path = _distill_skill([7, 9])
    assert path is not None
    head = path.read_text(encoding="utf-8")
    # Most-recent run first, matching the distiller's selection order.
    assert "source_goal_ids: 9 7" in head


def test_tainted_run_reaches_the_skills_it_taught(tmp_path, monkeypatch):
    """THE memory-integrity query: an episode turns out to be tainted; walk
    episode -> goal -> every learned skill distilled from it."""
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    w = _world(tmp_path)
    project_id = w.create_project("Client matter", owner=OWNER)
    gid = w.create_goal(
        "reconcile vendor invoices", domain="finance_ap", owner=OWNER,
        project_id=project_id,
    )
    ep = w.start_episode(gid)
    w.end_episode(ep, "reconciled", "done", cost_dollars=0.10,
                  input_tokens=10, output_tokens=10)
    path = _distill_skill([gid], project_id=project_id, owner=OWNER)
    assert path is not None
    eg.rebuild(project_id=project_id, owner=OWNER)
    out = eg.blast_radius(
        "goal", str(gid), project_id=project_id, owner=OWNER,
    )
    assert out["found"]
    assert path.stem in out["skills"]
    # And the skill's own neighborhood names its source goal with provenance.
    skill_hood = eg.neighborhood(
        "skill", path.stem, depth=1,
        project_id=project_id, owner=OWNER,
    )
    src = next(e for e in skill_hood["edges"] if e["rel"] == "distilled_from")
    assert src["record_type"] == "skill" and src["record_id"] == path.stem


def test_skill_lineage_reads_only_the_active_tenant_store():
    from maverick.paths import tenant_scope

    with tenant_scope(tenant="tenant-a"):
        a_path = _distill_skill([101], goal="reconcile acme invoices")
        assert a_path is not None and "tenant-a" in a_path.parts
        eg.rebuild(project_id=MATTER_ID, owner=OWNER)
        assert eg.neighborhood(
            "skill", a_path.stem, depth=1,
            project_id=MATTER_ID, owner=OWNER,
        )["found"]

    with tenant_scope(tenant="tenant-b"):
        # Tenant B must neither index nor resolve tenant A's learned skill.
        b_path = _distill_skill([202], goal="review globex contracts")
        assert b_path is not None and "tenant-b" in b_path.parts
        eg.rebuild(project_id=MATTER_ID, owner=OWNER)
        assert not eg.neighborhood(
            "skill", a_path.stem, depth=1,
            project_id=MATTER_ID, owner=OWNER,
        )["found"]
        assert eg.neighborhood(
            "skill", b_path.stem, depth=1,
            project_id=MATTER_ID, owner=OWNER,
        )["found"]

    with tenant_scope(tenant="tenant-a"):
        # The other direction is isolated too; tenant A's existing graph never
        # acquires a tenant B node.
        assert not eg.neighborhood(
            "skill", b_path.stem, depth=1,
            project_id=MATTER_ID, owner=OWNER,
        )["found"]


def test_root_skill_store_is_never_a_lineage_fallback(tmp_path, monkeypatch):
    from maverick.paths import data_dir
    store = data_dir("learned-skills", tenant=None)
    store.mkdir(parents=True, exist_ok=True)
    (store / "old-skill.md").write_text(
        "---\nname: old-skill\ntriggers:\n  - something\n"
        "distilled_at: 2025-01-01T00:00:00Z\nn_examples: 2\n"
        "source: auto-distilled-local-v2\n---\n# What this does\n",
        encoding="utf-8")
    eg.rebuild()
    hood = eg.neighborhood("skill", "old-skill", depth=1)
    assert not hood["found"]


def test_skill_lineage_denies_cross_matter_and_owner(tmp_path):
    first = _distill_skill(
        [11], goal="reconcile first matter invoices",
        project_id=MATTER_ID, owner=OWNER,
    )
    second = _distill_skill(
        [22], goal="review second matter contracts",
        project_id=202, owner="user:bob",
    )
    assert first is not None and second is not None

    eg.rebuild(project_id=MATTER_ID, owner=OWNER)
    assert eg.neighborhood(
        "skill", first.stem, project_id=MATTER_ID, owner=OWNER,
    )["found"]
    assert not eg.neighborhood(
        "skill", second.stem, project_id=MATTER_ID, owner=OWNER,
    )["found"]
    assert not eg.neighborhood("skill", first.stem)["found"]
    assert not eg.neighborhood(
        "skill", first.stem, project_id=MATTER_ID, owner="user:bob",
    )["found"]


def test_scoped_lineage_rejects_a_tampered_cross_matter_source_goal(
    tmp_path, monkeypatch,
):
    from maverick import world_model

    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    world = _world(tmp_path)
    first_matter = world.create_project("First client", owner=OWNER)
    second_owner = "user:bob"
    second_matter = world.create_project("Second client", owner=second_owner)
    first_goal = world.create_goal(
        "first client work", owner=OWNER, project_id=first_matter,
    )
    second_goal = world.create_goal(
        "second client work", owner=second_owner, project_id=second_matter,
    )
    skill = _distill_skill(
        [first_goal], goal="first client work",
        project_id=first_matter, owner=OWNER,
    )
    assert skill is not None
    text = skill.read_text(encoding="utf-8")
    skill.write_text(
        text.replace(
            f"source_goal_ids: {first_goal}",
            f"source_goal_ids: {first_goal} {second_goal}",
        ),
        encoding="utf-8",
    )

    eg.rebuild(project_id=first_matter, owner=OWNER)

    hood = eg.neighborhood(
        "skill", skill.stem, depth=1,
        project_id=first_matter, owner=OWNER,
    )
    source_goal_names = {
        edge["dst"]["name"]
        for edge in hood["edges"]
        if edge["rel"] == "distilled_from"
    }
    assert source_goal_names == {f"#{first_goal} first client work"}
    assert not eg.neighborhood(
        "goal", str(second_goal), project_id=first_matter, owner=OWNER,
    )["found"]
