"""The knowledge engine: ingest documents into a per-domain collection and
retrieve relevant chunks.

Ingestion is screened so a poisoned document is caught at the door -- RAG
poisoning is precisely what the agent compartments defend against, so the
knowledge layer scans on the way in rather than only at query time. A configured
Shield does the heavy lifting; a built-in high-signal injection-marker screen
ALWAYS runs too, so the common no-Shield default still rejects the obvious
prompt-injection payloads (mirrors memory_guard, which screens
external writes regardless of whether a Shield is wired).
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass

from .chunk import chunk_text
from .embed import DeterministicEmbedder
from .parse import extract_text
from .store import SqliteVectorStore

log = logging.getLogger(__name__)

# Built-in injection tripwire applied to every ingested chunk, even when no
# Shield is configured. maverick-knowledge is standalone (no maverick-core dep),
# so it can't reuse memory_guard.injection_markers; this is a deliberately small,
# high-signal subset of the SAME phrases -- instruction-override, role-reassign,
# fake role tags, secret-exfiltration, and safety-override. The shell/base64
# patterns memory_guard also carries are intentionally OMITTED here: a knowledge
# base legitimately ingests engineering docs full of `rm -rf` / `curl` / base64,
# and silently dropping those would harm recall. A real Shield (when passed)
# covers the rest.
_INJECTION_RE = re.compile(
    r"\b(?:ignore|disregard|forget)\b.{0,30}\b(?:previous|prior|above|earlier|all)\b"
    r".{0,30}\b(?:instruction|message|context|prompt|rule)"
    r"|\byou\s+are\s+now\b"
    r"|\bnew\s+(?:system\s+)?(?:instruction|prompt|directive|rule)"
    r"|</?\s*(?:system|assistant|developer)\s*>"
    r"|\b(?:reveal|print|show|leak|exfiltrate)\b.{0,40}"
    r"\b(?:system\s+prompt|your\s+instruction|secret|api[\s_-]?key|password|token|credential)"
    r"|\boverride\b.{0,20}\b(?:safety|guard|shield|policy|governance)\b",
    re.IGNORECASE,
)


@dataclass
class Hit:
    score: float
    text: str
    source: str


# Floor for the same-source suffix/prefix trim in _dedup_hits: a shared run
# this long between two chunks of one document is almost certainly the
# chunker's overlap, not coincidence. Shorter repeats (headings, boilerplate
# phrases) are left alone.
_MIN_DEDUP_OVERLAP = 32


def _dedup_hits(hits: list[Hit], *, max_overlap: int) -> list[Hit]:
    """Collapse retrieval redundancy before chunks are rendered into a prompt.

    The chunker overlaps neighbours by ``chunk_overlap`` chars so boundary
    facts stay retrievable -- but when both neighbours rank, the shared region
    would be sent to the model twice. Two passes over the score-sorted hits:

    1. absorb hits whose text is contained in a kept hit (keeping the longer
       text under the better score);
    2. for same-source pairs, trim a duplicated suffix/prefix run
       (>= _MIN_DEDUP_OVERLAP, <= ``max_overlap``) off the lower-ranked chunk.

    Purely lexical and conservative: different sources are never touched.
    """
    kept: list[Hit] = []
    for h in hits:
        absorbed = False
        for j, prev in enumerate(kept):
            # Same-source only: absorbing across documents would render
            # one document's text under another's [source: ...] label — a
            # silent citation corruption (shared boilerplate/license text
            # is a realistic cross-document substring).
            if h.source != prev.source:
                continue
            if h.text and h.text in prev.text:
                absorbed = True
                break
            if prev.text and prev.text in h.text:
                kept[j] = Hit(prev.score, h.text, prev.source)
                absorbed = True
                break
        if not absorbed:
            kept.append(h)
    for i in range(len(kept)):
        for j in range(len(kept)):
            if i == j:
                continue
            a, b = kept[i], kept[j]
            if a.source != b.source or not a.text or not b.text:
                continue
            limit = min(len(a.text), len(b.text), max_overlap)
            for n in range(limit, _MIN_DEDUP_OVERLAP - 1, -1):
                if a.text[-n:] == b.text[:n]:
                    kept[j] = Hit(b.score, b.text[n:].lstrip(), b.source)
                    break
    return [h for h in kept if h.text]


class KnowledgeBase:
    """A per-domain document store.

    ``collection`` is the domain's knowledge source, so a finance agent's
    queries never surface legal's documents -- knowledge respects the same
    bulkheads the compartments enforce.
    """

    def __init__(self, store=None, embedder=None, shield=None,
                 chunk_size: int = 1000, chunk_overlap: int = 200,
                 image_describer=None):
        self.store = store or SqliteVectorStore()
        self.embedder = embedder or DeterministicEmbedder()
        self.shield = shield
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # Optional callable(path) -> str for images / process diagrams (OCR or a
        # vision model). Without it, image uploads are skipped (not read as bytes).
        self.image_describer = image_describer

    def close(self) -> None:
        """Release the backing store's resources (e.g. the SQLite connection).

        No-op for stores that don't expose ``close`` (e.g. a future pgvector
        pool managed elsewhere)."""
        closer = getattr(self.store, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> KnowledgeBase:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _safe(self, text: str) -> bool:
        """Screen a chunk on the way in. The built-in injection-marker tripwire
        ALWAYS runs (even with no Shield wired, the common default), so a poisoned
        document can't ride into prompts via search_formatted. A configured Shield
        runs in addition. Fail-open ONLY on a Shield scanner error, mirroring the
        kernel's shield contract -- the marker screen itself never errors."""
        if _INJECTION_RE.search(text):
            log.warning("knowledge: dropping chunk with injection marker on ingest")
            return False
        if self.shield is None:
            return True
        try:
            # An ingested chunk is untrusted CONTENT (like tool output), so use
            # the indirect-injection / content detector, not the prompt-tuned
            # scan_input.
            verdict = self.shield.scan_output(text)
            return getattr(verdict, "allowed", True)
        except Exception:  # pragma: no cover -- never block ingest on a scan bug
            return True

    def ingest_text(self, collection: str, text: str, source: str = "", *,
                    subject: str = "", trust_tier: int = 2,
                    sensitivity: str = "internal", ingested_by: str = "",
                    extra_meta: dict | None = None) -> int:
        """Chunk, shield-scan, embed and store one document's text. Returns the
        number of chunks stored (poisoned chunks are dropped).

        Every chunk is stamped with provenance so retrieval and governance can
        reason about where a passage came from and whose data it is:

        * ``source``     — document path/URL (the citation).
        * ``subject``    — the data-subject key (e.g. ``slack:U123``) this
          document belongs to, so a GDPR erasure can find and remove it; empty
          for reference material with no subject.
        * ``trust_tier`` — 3 first-party … 0 external/untrusted (the same
          provenance scale memory-guard uses).
        * ``sensitivity``— data-classification label (public/internal/…).
        * ``ingested_by``— the principal that loaded it (audit attribution).
        * ``doc_sha256`` — content hash (dedup + tamper-evidence).
        """
        raw_chunks = list(chunk_text(text, self.chunk_size, self.chunk_overlap))
        chunks = [c for c in raw_chunks if self._safe(c)]
        # Boundary-split evasion guard: an attacker can straddle an injection
        # tripwire across a chunk edge (or pick a small chunk_size) so no single
        # chunk matches the marker even though the full document does. Screen the
        # FULL text unconditionally -- the old `len(chunks) == len(raw_chunks)`
        # guard skipped this whenever any one chunk was also independently
        # dropped, letting a straddled payload's remaining chunks through.
        if not self._safe(text):
            log.warning("knowledge: dropping document with boundary-split "
                        "injection marker on ingest")
            return 0
        if not chunks:
            return 0
        vectors = self.embedder.embed(chunks)
        base_meta = {
            "source": source,
            "subject": subject,
            "trust_tier": int(trust_tier),
            "sensitivity": sensitivity,
            "ingested_by": ingested_by,
            "ingested_at": time.time(),
            "doc_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        if extra_meta:
            base_meta.update(extra_meta)
        items = [
            (uuid.uuid4().hex, c, v, dict(base_meta))
            for c, v in zip(chunks, vectors, strict=False)
        ]
        self.store.add(collection, items)
        return len(items)

    def ingest_path(self, collection: str, path, *, subject: str = "",
                    trust_tier: int = 2, sensitivity: str = "internal",
                    ingested_by: str = "") -> int:
        """Ingest a document from disk (parsed by extension).

        Images / process diagrams go through ``image_describer`` (OCR or a
        vision model); without one they're skipped rather than read as bytes.
        The resulting text is shield-scanned like any other document, and every
        chunk carries the provenance passed here (subject / trust_tier /
        sensitivity / ingested_by)."""
        from .parse import is_image
        if is_image(path):
            if self.image_describer is None:
                log.info("knowledge: skipping image %s (no image_describer set)", path)
                return 0
            try:
                text = self.image_describer(str(path))
            except Exception as e:  # a describer failure must not abort ingestion
                log.warning("knowledge: image describer failed on %s: %s", path, e)
                return 0
        else:
            text = extract_text(path)
        return self.ingest_text(
            collection, text, source=str(path), subject=subject,
            trust_tier=trust_tier, sensitivity=sensitivity, ingested_by=ingested_by)

    # --- governance: erasure + verification over provenance -------------------

    def collections(self) -> list[str]:
        """Every collection the backing store holds (the erasure sweep surface).
        Empty when the store predates the ``collections()`` primitive."""
        fn = getattr(self.store, "collections", None)
        return list(fn()) if callable(fn) else []

    def erase_subject(self, subject: str, collections=None) -> dict[str, int]:
        """Delete every chunk belonging to ``subject`` (GDPR Art. 17).

        Sweeps the given collections (or every collection when ``None``) and
        removes chunks whose provenance ``subject`` matches. Returns
        ``{collection: chunks_removed}`` for the non-empty deletions — the
        evidence a caller records to the signed audit chain.
        """
        if not subject:
            return {}
        cols = list(collections) if collections is not None else self.collections()
        out: dict[str, int] = {}
        for c in cols:
            n = self.store.delete_where(c, "subject", subject)
            if n:
                out[c] = n
        return out

    def erase_source(self, source: str, collections=None) -> dict[str, int]:
        """Delete every chunk from one document ``source`` (retract a document).
        Same shape as :meth:`erase_subject`."""
        if not source:
            return {}
        cols = list(collections) if collections is not None else self.collections()
        out: dict[str, int] = {}
        for c in cols:
            n = self.store.delete_where(c, "source", source)
            if n:
                out[c] = n
        return out

    def count_subject(self, subject: str, collections=None) -> int:
        """Residual chunks for ``subject`` — the erasure-verify primitive
        (should be 0 after :meth:`erase_subject`)."""
        if not subject:
            return 0
        cols = list(collections) if collections is not None else self.collections()
        return sum(self.store.count_where(c, "subject", subject) for c in cols)

    def delete_collection(self, collection: str) -> None:
        """Delete an unapproved or retired collection from the backing store."""
        self.store.delete_collection(collection)

    def _search_vec(self, collection: str, vector, k: int) -> list[Hit]:
        """Store lookup for an already-embedded query; one Hit-wrap site."""
        return [
            Hit(m.score, m.text, m.meta.get("source", ""))
            for m in self.store.search(collection, vector, k)
        ]

    def search(self, collection: str, query: str, k: int = 5) -> list[Hit]:
        return self._search_vec(collection, self.embedder.embed([query])[0], k)

    def search_formatted(self, collections, query: str, k: int = 5) -> str:
        """Search one or more domain collections and render the top-k chunks with
        their sources -- the string a domain agent's ``knowledge_search`` tool
        returns. Merges across collections, then keeps the globally top-k.

        The query is embedded once and the vector reused per collection, and
        redundant chunk content (the chunker's overlap, contained duplicates)
        is collapsed before the k-slice -- absorbed duplicates make room for
        the next-best distinct chunk rather than shrinking the result, and the
        model never pays tokens for the same span twice."""
        vector = self.embedder.embed([query])[0]
        hits: list[Hit] = []
        for c in collections:
            hits.extend(self._search_vec(c, vector, k))
        hits.sort(key=lambda h: h.score, reverse=True)
        hits = _dedup_hits(hits, max_overlap=self.chunk_overlap)[: max(1, k)]
        if not hits:
            return "No relevant documents found in this domain's knowledge base."
        return "\n\n".join(
            f"[source: {h.source or 'unknown'}]\n{h.text}" for h in hits
        )
