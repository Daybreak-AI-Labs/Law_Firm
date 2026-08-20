"""Stateless bridge to the firm's pinned, on-box embedding model.

This module intentionally retains the small API used by reflexion and domain
routing while removing the former FastEmbed download path and tenant-global
vector cache.  Model admission, digest verification, offline environment, and
``trust_remote_code=False`` enforcement live in ``maverick_knowledge``.
Client text and vectors are never persisted here.
"""
from __future__ import annotations

import logging
import math
import os
import threading

log = logging.getLogger(__name__)

_model = None
_model_key: tuple[object, ...] | None = None
_model_lock = threading.Lock()
_warned_keys: set[tuple[object, ...]] = set()


def _configuration() -> tuple[dict, tuple[object, ...]]:
    from ..config import get_knowledge

    cfg = get_knowledge()
    key = (
        bool(cfg.get("enable")),
        str(cfg.get("embedder") or "").strip().lower(),
        str(cfg.get("model") or "").strip(),
        str(cfg.get("model_digest") or "").strip().lower(),
        int(cfg.get("dim") or 0),
        os.environ.get("MAVERICK_EMBED_PROVIDER"),
        os.environ.get("MAVERICK_EMBED_MODEL"),
        os.environ.get("MAVERICK_EMBED_MODEL_DIGEST"),
    )
    return cfg, key


def _get_model():
    global _model, _model_key

    cfg, key = _configuration()
    if not key[0]:
        return None
    if _model is not None and _model_key == key:
        return _model
    with _model_lock:
        if _model is not None and _model_key == key:
            return _model
        try:
            from maverick_knowledge.embed import build_embedder

            candidate = build_embedder(cfg)
        except Exception as exc:
            if key not in _warned_keys:
                log.warning(
                    "local semantic recall unavailable (%s: %s); using lexical recall",
                    type(exc).__name__,
                    exc,
                )
                _warned_keys.add(key)
            return None
        _model = candidate
        _model_key = key
        return candidate


def _have_fastembed() -> bool:
    """Compatibility name: report a verified local embedder, never FastEmbed."""
    return _get_model() is not None


def embed(texts: list[str]) -> list[list[float]] | None:
    if not texts:
        return []
    model = _get_model()
    if model is None:
        return None
    try:
        vectors = model.embed(list(texts))
    except Exception as exc:
        log.warning(
            "local semantic recall failed (%s: %s); using lexical recall",
            type(exc).__name__,
            exc,
        )
        return None
    if len(vectors) != len(texts):
        return None
    return [[float(value) for value in vector] for vector in vectors]


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


__all__ = ["_cosine", "_have_fastembed", "embed"]
