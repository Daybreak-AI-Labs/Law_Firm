"""Governed administration of the knowledge plane: erasure, retention, and
verification over the per-domain vector store.

The kernel runs WITHOUT ``maverick-knowledge`` (it is an optional extra), so
every entry point here fails soft: if the package is absent or knowledge RAG
is disabled, the functions return an empty/zero result rather than raising, and
the caller (``maverick erase`` / ``erase-verify`` / DSAR export) simply reports
that no knowledge store participated. When knowledge IS enabled, a subject's
ingested documents are removed by provenance so a right-to-erasure request
reaches the vector store, not just the world model — closing the gap where a
subject's embedded document chunks would otherwise survive erasure.

The subject key mirrors the DSAR/erase convention exactly (``channel:user_id``,
each component percent-quoted) so "a chunk that would be erased is a chunk that
is counted" — the same invariant the world-model verifier relies on.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)


def subject_key(channel: str, user_id: str) -> str:
    """The provenance ``subject`` stamped on a data subject's ingested chunks.

    Identical to :func:`maverick.dsar._fact_subject_token` so knowledge erasure
    and world-model erasure agree on what "this subject's data" means.
    """
    return f"{quote(channel, safe='')}:{quote(user_id, safe='')}"


def open_knowledge_base(*, tenant: str | None = None) -> Any | None:
    """Open the deployment's configured KnowledgeBase, or ``None``.

    ``None`` (not an error) when: maverick-knowledge isn't installed, knowledge
    RAG is disabled in config, or construction fails — the same fail-open
    contract the orchestrator uses so a knowledge misconfig never wedges an
    erase. The caller closes the returned base (it holds a DB handle).
    """
    try:
        from .config import get_knowledge
        kcfg = get_knowledge()
        if not kcfg.get("enable"):
            return None
        if not kcfg.get("path"):
            from .workspace import Workspace
            kcfg = {**kcfg, "path": str(Workspace.current().knowledge_path)}
        from maverick_knowledge import KnowledgeBase, build_embedder, build_store
        return KnowledgeBase(store=build_store(kcfg), embedder=build_embedder(kcfg))
    except Exception as e:  # pragma: no cover -- knowledge is optional
        log.warning("knowledge admin: base unavailable (fail-open): %s", e)
        return None


def erase_subject(channel: str, user_id: str, *,
                  tenant: str | None = None) -> dict[str, int]:
    """Delete a subject's document chunks from every knowledge collection.

    Returns ``{collection: chunks_removed}`` (empty when knowledge is off or
    the subject had no ingested documents) — the evidence the erase command
    folds into its signed audit event.
    """
    kb = open_knowledge_base(tenant=tenant)
    if kb is None:
        return {}
    try:
        return kb.erase_subject(subject_key(channel, user_id))
    except Exception as e:  # pragma: no cover -- never abort an erase on KB error
        log.warning("knowledge admin: erase_subject failed (fail-open): %s", e)
        return {}
    finally:
        _close(kb)


def count_subject(channel: str, user_id: str, *,
                  tenant: str | None = None) -> int:
    """Residual document chunks for a subject across all collections.

    The erasure-verify primitive: should be 0 after :func:`erase_subject`. A
    ``None`` knowledge base contributes 0 (no store to leave residue in).
    """
    kb = open_knowledge_base(tenant=tenant)
    if kb is None:
        return 0
    try:
        return kb.count_subject(subject_key(channel, user_id))
    except Exception as e:  # pragma: no cover
        log.warning("knowledge admin: count_subject failed (fail-open): %s", e)
        return 0
    finally:
        _close(kb)


def _close(kb: Any) -> None:
    try:
        kb.close()
    except Exception:  # pragma: no cover
        pass
