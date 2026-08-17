"""Governed knowledge plane: provenance stamping, erasure by subject/source,
verification counts, and store-layer delete_where/count_where/collections
over the SQLite store."""
from __future__ import annotations

from maverick_knowledge import KnowledgeBase, SqliteVectorStore

# ---- store layer -------------------------------------------------------------


def test_sqlite_delete_where_and_count_where():
    s = SqliteVectorStore(":memory:")
    s.add("c", [
        ("id1", "alice doc", [1.0, 0.0], {"subject": "u:alice", "source": "a.txt"}),
        ("id2", "bob doc", [0.0, 1.0], {"subject": "u:bob", "source": "b.txt"}),
        ("id3", "alice doc 2", [1.0, 1.0], {"subject": "u:alice", "source": "a2.txt"}),
    ])
    assert s.count("c") == 3
    assert s.count_where("c", "subject", "u:alice") == 2
    assert sorted(s.collections()) == ["c"]
    removed = s.delete_where("c", "subject", "u:alice")
    assert removed == 2
    assert s.count_where("c", "subject", "u:alice") == 0
    assert s.count("c") == 1
    # by source too
    assert s.delete_where("c", "source", "b.txt") == 1
    assert s.count("c") == 0


# ---- KnowledgeBase provenance + erasure --------------------------------------

def test_ingest_stamps_provenance():
    s = SqliteVectorStore(":memory:")
    kb = KnowledgeBase(store=s)
    kb.ingest_text("c", "some content here", source="doc.txt",
                   subject="slack:U1", trust_tier=1, sensitivity="confidential",
                   ingested_by="admin@co")
    rows = s._db.execute("SELECT meta FROM chunks WHERE collection='c'").fetchall()
    import json
    meta = json.loads(rows[0][0])
    assert meta["subject"] == "slack:U1"
    assert meta["trust_tier"] == 1
    assert meta["sensitivity"] == "confidential"
    assert meta["ingested_by"] == "admin@co"
    assert meta["source"] == "doc.txt"
    assert len(meta["doc_sha256"]) == 64
    assert "ingested_at" in meta


def test_erase_subject_isolates_and_verifies():
    kb = KnowledgeBase(store=SqliteVectorStore(":memory:"))
    kb.ingest_text("itgrc", "alice's private file", source="a.txt", subject="slack:UA")
    kb.ingest_text("itgrc", "bob's private file", source="b.txt", subject="slack:UB")
    kb.ingest_text("itgrc", "public GDPR reference text", source="gdpr.txt")  # no subject
    assert kb.count_subject("slack:UA") == 1
    removed = kb.erase_subject("slack:UA")
    assert removed == {"itgrc": 1}
    assert kb.count_subject("slack:UA") == 0
    assert kb.count_subject("slack:UB") == 1          # other subject untouched
    assert kb.store.count("itgrc") == 2               # reference material survives


def test_erase_source_retracts_a_document():
    kb = KnowledgeBase(store=SqliteVectorStore(":memory:"))
    kb.ingest_text("c", "doc one text", source="one.txt")
    kb.ingest_text("c", "doc two text", source="two.txt")
    assert kb.erase_source("one.txt") == {"c": 1}
    assert kb.store.count("c") == 1


def test_erase_empty_subject_is_noop():
    kb = KnowledgeBase(store=SqliteVectorStore(":memory:"))
    kb.ingest_text("c", "text", source="x.txt", subject="slack:U1")
    assert kb.erase_subject("") == {}
    assert kb.count_subject("") == 0
    assert kb.store.count("c") == 1
