"""``[knowledge] model``/``dim`` default per embedder, not to Voyage's.

Both keys used to default to the hosted provider's values whichever embedder
was selected, so ``embedder = "local"`` with nothing else set resolved to
``SentenceTransformer("voyage-3")`` -- not a model id that exists -- and
advertised 1024 dimensions for a 384-dim MiniLM. ``build_embedder`` has its own
``all-MiniLM-L6-v2`` fallback, but it could never fire: ``get_knowledge`` had
already filled the key in.

That combination is worse than a plain bug here. The hosted embedders now
refuse to run without an explicit acknowledgement that document text goes to a
vendor, and the refusal points the operator at ``local`` -- so the recommended
safe path was the broken one, and a firm following the advice would have got a
knowledge base that fails on first index.

The dim half is not cosmetic either: the vector store raises on a dimension
mismatch rather than returning garbage, so a wrong default reads as a broken
knowledge base rather than as a bad setting.
"""
from __future__ import annotations

import pytest
from maverick import config


def _knowledge(monkeypatch, section: dict) -> dict:
    monkeypatch.setattr(config, "load_config", lambda *a, **k: {"knowledge": section})
    return config.get_knowledge()


@pytest.mark.parametrize(
    ("embedder", "model", "dim"),
    [
        ("hosted", "voyage-3", 1024),
        ("cohere", "embed-v4.0", 1024),
        ("local", "all-MiniLM-L6-v2", 384),
        ("deterministic", "", 256),
    ],
)
def test_model_and_dim_follow_the_selected_embedder(
    monkeypatch, embedder: str, model: str, dim: int,
):
    resolved = _knowledge(monkeypatch, {"enable": True, "embedder": embedder})
    assert resolved["model"] == model
    assert resolved["dim"] == dim


def test_local_never_resolves_to_a_hosted_model_name(monkeypatch):
    """The specific regression: the on-box path must not get a vendor model id."""
    resolved = _knowledge(monkeypatch, {"enable": True, "embedder": "local"})
    assert "voyage" not in resolved["model"]
    assert resolved["model"] == "all-MiniLM-L6-v2"


def test_local_default_builds_a_usable_local_embedder(monkeypatch):
    """End-to-end: the resolved config is what build_embedder actually gets."""
    pytest.importorskip("sentence_transformers")
    from maverick_knowledge.embed import build_embedder

    monkeypatch.delenv("MAVERICK_EMBED_PROVIDER", raising=False)
    resolved = _knowledge(monkeypatch, {"enable": True, "embedder": "local"})
    emb = build_embedder(resolved)
    assert emb.model_name == "all-MiniLM-L6-v2"


def test_an_explicit_model_and_dim_still_win(monkeypatch):
    resolved = _knowledge(monkeypatch, {
        "enable": True, "embedder": "local", "model": "bge-small-en", "dim": 512,
    })
    assert resolved["model"] == "bge-small-en"
    assert resolved["dim"] == 512


def test_unset_embedder_keeps_the_historical_hosted_defaults(monkeypatch):
    """No embedder key is still 'hosted', so this must not shift underneath."""
    resolved = _knowledge(monkeypatch, {"enable": True})
    assert resolved["embedder"] == "hosted"
    assert (resolved["model"], resolved["dim"]) == ("voyage-3", 1024)


def test_an_unknown_embedder_falls_back_to_the_hosted_defaults(monkeypatch):
    """build_embedder raises on an unknown provider; this must not raise first."""
    resolved = _knowledge(monkeypatch, {"enable": True, "embedder": "nonsense"})
    assert (resolved["model"], resolved["dim"]) == ("voyage-3", 1024)
