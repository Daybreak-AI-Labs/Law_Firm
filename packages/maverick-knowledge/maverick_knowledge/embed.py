"""On-box embedding providers for client-matter knowledge.

Only ``local`` and the deterministic test stub exist. Hosted Voyage/Cohere
embedders were removed: an embedding request contains the privileged document
chunks themselves, and a law-firm deployment must not acquire that new egress
path through a config toggle. A configured provider that cannot initialize
fails loudly rather than silently returning plausible but meaningless recall.
"""
from __future__ import annotations

import hashlib
import math
import os
from typing import Protocol


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _l2(vector: list[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in vector))
    return [value / magnitude for value in vector] if magnitude else vector


class DeterministicEmbedder:
    """Dependency-free lexical hashing stub for tests and offline development.

    This is intentionally not advertised as semantic retrieval. Production
    firm deployments should use the local sentence-transformers provider.
    """

    def __init__(self, dim: int = 256):
        if int(dim) <= 0:
            raise ValueError("embedding dimension must be positive")
        self.dim = int(dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            for token in (text or "").lower().split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                bucket = int.from_bytes(digest, "big") % self.dim
                vector[bucket] += 1.0
            vectors.append(_l2(vector))
        return vectors


def build_embedder(cfg: dict | None = None) -> Embedder:
    """Build an on-box embedder and fail closed on every hosted provider.

    ``MAVERICK_EMBED_PROVIDER`` remains an operator override, but it can only
    choose ``local`` or ``deterministic``. Legacy ``hosted``/``cohere`` values
    produce an explicit error even if API keys or old consent flags remain in
    configuration; there is no network implementation left to call.
    """
    cfg = cfg or {}
    provider = str(
        os.environ.get("MAVERICK_EMBED_PROVIDER")
        or cfg.get("embedder", "local")
    ).strip().lower()

    if provider == "deterministic":
        return DeterministicEmbedder(int(cfg.get("dim", 256)))
    if provider in {"hosted", "cohere"}:
        raise RuntimeError(
            f"knowledge: external embedder {provider!r} was removed; "
            "client-matter documents must be embedded on-box"
        )
    if provider != "local":
        raise ValueError(
            f"knowledge: unknown embedder provider {provider!r} "
            "(expected 'local' or 'deterministic')"
        )

    from .local_embed import LocalEmbedder

    model = str(
        os.environ.get("MAVERICK_EMBED_MODEL")
        or cfg.get("model")
        or ""
    ).strip()
    digest = str(
        os.environ.get("MAVERICK_EMBED_MODEL_DIGEST")
        or cfg.get("model_digest")
        or ""
    ).strip()
    embedder = LocalEmbedder(model, digest)

    import importlib.util

    if importlib.util.find_spec("sentence_transformers") is None:
        raise RuntimeError(
            "knowledge: local embedder selected but sentence-transformers is "
            "not installed (install the 'local' extra); deterministic is a "
            "test stub, not a production semantic fallback"
        )
    return embedder


__all__ = ["Embedder", "DeterministicEmbedder", "build_embedder"]
