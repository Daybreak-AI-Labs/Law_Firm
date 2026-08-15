"""Assessment memory: the assessment flow learns from every one before it.

Every completed assessment is already persisted (:func:`maverick.assessment.
save_session` -> the tenant's assessments dir). This module turns that record
into working memory:

  * :func:`similar` -- past assessments of subjects like this one, with their
    risk ratings, so an intake can say "you've assessed systems like this
    before; the last CRM vendor came out MEDIUM".
  * :func:`suggest_answers` -- per-question suggested answers with confidence
    and provenance, distilled from the top-k similar past sessions of the SAME
    template (a majority vote -- explainable, deterministic, no LLM), so a
    respondent confirms instead of re-typing what the org already knows.
  * :func:`record_session` -- the write hook ``save_session`` calls: besides
    the canonical JSON record, it distills the assessment into the governed
    knowledge plane (collection ``assessments``) when knowledge RAG is
    enabled, so agents and future assessments can recall lessons
    semantically. Fail-open: no knowledge package, nothing breaks.

Similarity is transparent token overlap (Jaccard over subject words, with a
same-template bonus) -- the goal is recall of the org's OWN precedents, not
open-ended semantics; the knowledge plane adds the semantic layer when it's
on. Suggestions are advisory only: nothing here auto-answers an assessment,
and the human reviewer gate is untouched. Gated by ``[assessments] learn``
(default on -- it only reads the org's own records).
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any

log = logging.getLogger(__name__)

KNOWLEDGE_COLLECTION = "assessments"


def enabled() -> bool:
    try:
        from .config import get_assessments
        return bool(get_assessments()["learn"])
    except Exception:  # pragma: no cover -- config never crashes a read
        return True


def _tokens(text: str) -> set[str]:
    return {t for t in (text or "").lower().replace("/", " ").split()
            if len(t) > 2}


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def similar(subject: str, *, assessment_type: str | None = None,
            k: int = 3, exclude_id: str | None = None) -> list[dict]:
    """Past assessments most like ``subject``, best first.

    Each entry: id/type/subject/risk_rating/findings/created_at/similarity.
    Empty when learning is off or there is no history -- never raises."""
    if not enabled():
        return []
    try:
        from .assessment import list_saved
        rows = list_saved()
    except Exception:  # pragma: no cover -- a read miss is just "no history"
        return []
    scored = []
    for r in rows:
        if exclude_id and r.get("id") == exclude_id:
            continue
        sim = _similarity(subject, str(r.get("subject", "")))
        if assessment_type and r.get("type") == assessment_type:
            sim += 0.15
        if sim > 0:
            scored.append({**r, "similarity": round(min(sim, 1.0), 3)})
    scored.sort(key=lambda r: (r["similarity"], r.get("created_at", 0)),
                reverse=True)
    return scored[:k]


def suggest_answers(assessment_type: str, subject: str, *, k: int = 3,
                    min_confidence: float = 0.6) -> dict[str, dict]:
    """Advisory per-question answers from the org's own precedents.

    Majority vote over the top-``k`` similar past sessions of the SAME
    template: ``{question_id: {answer, note, confidence, based_on: [ids]}}``.
    ``confidence`` is the agreement ratio among sessions that answered the
    question; suggestions below ``min_confidence`` are dropped (the 0.6
    default drops a 50/50 tie -- a coin flip is not a suggestion). Advisory
    only -- the caller prefills, the human confirms."""
    if not enabled():
        return {}
    from .assessment import load_saved
    picks = similar(subject, assessment_type=assessment_type, k=k)
    sessions = []
    for p in picks:
        data = load_saved(str(p.get("id", "")))
        if data and data.get("type") == assessment_type:
            sessions.append(data)
    if not sessions:
        return {}
    votes: dict[str, Counter] = {}
    notes: dict[str, str] = {}
    based: dict[str, list[str]] = {}
    for data in sessions:
        for qid, rec in (data.get("answers") or {}).items():
            ans = str((rec or {}).get("answer", "")).strip().lower()
            if not ans:
                continue
            votes.setdefault(qid, Counter())[ans] += 1
            based.setdefault(qid, []).append(str(data.get("id", "")))
            note = str((rec or {}).get("note", "")).strip()
            if note and qid not in notes:
                notes[qid] = note
    out: dict[str, dict] = {}
    for qid, counter in votes.items():
        answer, n = counter.most_common(1)[0]
        confidence = n / sum(counter.values())
        if confidence < min_confidence:
            continue
        out[qid] = {
            "answer": answer,
            "note": notes.get(qid, ""),
            "confidence": round(confidence, 2),
            "based_on": based.get(qid, []),
        }
    return out


def lessons(subject: str, *, k: int = 3) -> list[str]:
    """Semantic recall from the knowledge plane's ``assessments`` collection
    (past findings/ratings distilled at record time). Empty when knowledge
    RAG is off -- the deterministic :func:`similar` path still works."""
    if not enabled():
        return []
    try:
        from .knowledge_admin import open_knowledge_base
        kb = open_knowledge_base()
        if kb is None:
            return []
        hits = kb.search(KNOWLEDGE_COLLECTION, subject, k=k)
        return [str(getattr(h, "text", "")).strip()
                for h in hits if str(getattr(h, "text", "")).strip()]
    except Exception as e:  # noqa: BLE001 -- recall never breaks the caller
        log.debug("assessment_memory: lessons recall failed: %s", e)
        return []


def _distill(payload: dict[str, Any]) -> str:
    """One compact, human-readable record for semantic recall."""
    res = payload.get("result") or {}
    findings = res.get("findings") or []
    inherent = res.get("inherent_risk", res.get("risk_rating", "?"))
    residual = res.get("residual_risk", res.get("risk_rating", "?"))
    lines = [
        f"Assessment {payload.get('id', '?')} ({payload.get('type', '?')}) "
        f"of {payload.get('subject', '?')}: risk {res.get('risk_rating', '?')} "
        f"(inherent {inherent} -> residual {residual}), "
        f"{res.get('answered', 0)}/{res.get('total', 0)} answered, "
        f"{len(findings)} finding(s).",
    ]
    for f in findings[:12]:
        sev = f.get("severity", "?")
        lines.append(f"- [{sev}] {f.get('section', '')}: "
                     f"{f.get('question', f.get('text', ''))} "
                     f"(answer: {f.get('answer', '?')})")
    return "\n".join(lines)


def record_session(payload: dict[str, Any]) -> bool:
    """Distill a saved assessment into the knowledge plane (fail-open).

    Called by ``save_session`` with the same payload it wrote to disk. The
    JSON record stays canonical; this adds the semantic index. Returns True
    only when a knowledge ingest actually happened."""
    if not enabled():
        return False
    try:
        from .knowledge_admin import open_knowledge_base
        kb = open_knowledge_base()
        if kb is None:
            return False
        kb.ingest_text(
            KNOWLEDGE_COLLECTION,
            _distill(payload),
            source=f"assessment:{payload.get('id', '')}",
            subject=str(payload.get("subject", "")),
        )
        return True
    except Exception as e:  # noqa: BLE001 -- memory must never break a save
        log.debug("assessment_memory: knowledge ingest skipped: %s", e)
        return False


__all__ = ["KNOWLEDGE_COLLECTION", "enabled", "lessons", "record_session",
           "similar", "suggest_answers"]
