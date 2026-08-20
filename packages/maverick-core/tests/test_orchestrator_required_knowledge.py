"""Legal profiles that declare RAG never run on missing or unauthenticated matter data."""
from __future__ import annotations

import sqlite3

import pytest
from maverick import orchestrator as orch


class _Shield:
    def scan_output(self, _text):
        return type("Verdict", (), {"allowed": True})()


def _config(path):
    return {
        "enable": True,
        "embedder": "deterministic",
        "path": str(path),
    }


def _seed(path, monkeypatch, *, key: bytes = b"a" * 32, collection="matter:7:legal"):
    from maverick_knowledge import SqliteVectorStore

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", key.hex())
    store = SqliteVectorStore(path)
    store.add(
        collection,
        [("chunk", "authenticated matter fact", [1.0, 0.0], {"source": "facts.pdf"})],
    )
    store.close()


def test_required_knowledge_never_falls_back_to_public_collection(tmp_path, monkeypatch):
    path = tmp_path / "public-only" / "knowledge.db"
    _seed(path, monkeypatch, collection="public:legal")
    monkeypatch.setattr("maverick.config.get_knowledge", lambda: _config(path))

    with pytest.raises(orch.RequiredKnowledgeUnavailable):
        orch._build_knowledge(
            shield=_Shield(),
            matter_id=7,
            required_sources=("legal",),
        )


def test_corrupt_plaintext_required_collection_blocks_open(tmp_path, monkeypatch):
    path = tmp_path / "corrupt" / "knowledge.db"
    _seed(path, monkeypatch)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE chunks SET meta = '{}' WHERE id = 'chunk'")
    conn.commit()
    conn.close()
    monkeypatch.setattr("maverick.config.get_knowledge", lambda: _config(path))

    with pytest.raises(orch.RequiredKnowledgeUnavailable):
        orch._build_knowledge(
            shield=_Shield(),
            matter_id=7,
            required_sources=("legal",),
        )


def test_wrong_key_required_collection_blocks_open(tmp_path, monkeypatch):
    path = tmp_path / "wrong-key" / "knowledge.db"
    _seed(path, monkeypatch, key=b"a" * 32)
    monkeypatch.setenv("MAVERICK_ENCRYPTION_KEY", (b"b" * 32).hex())
    monkeypatch.setattr("maverick.config.get_knowledge", lambda: _config(path))

    with pytest.raises(orch.RequiredKnowledgeUnavailable):
        orch._build_knowledge(
            shield=_Shield(),
            matter_id=7,
            required_sources=("legal",),
        )


def test_authenticated_exact_matter_collection_opens(tmp_path, monkeypatch):
    path = tmp_path / "valid" / "knowledge.db"
    _seed(path, monkeypatch)
    monkeypatch.setattr("maverick.config.get_knowledge", lambda: _config(path))

    knowledge = orch._build_knowledge(
        shield=_Shield(),
        matter_id=7,
        required_sources=("legal",),
    )
    assert knowledge is not None
    knowledge.close()


def test_profile_without_rag_may_proceed_with_knowledge_disabled(monkeypatch):
    monkeypatch.setattr("maverick.config.get_knowledge", lambda: {"enable": False})
    assert orch._build_knowledge(required_sources=()) is None
