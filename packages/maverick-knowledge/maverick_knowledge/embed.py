"""Embedding providers.

Config-selected (``hosted`` | ``cohere`` | ``local`` | ``deterministic``).
:func:`build_embedder` **fails loud**: a chosen provider that can't initialize
raises rather than silently degrading to the non-semantic
:class:`DeterministicEmbedder` (which returns plausible-looking but meaningless
retrievals). Opt into the hash embedder explicitly with
``embedder = "deterministic"`` for tests / offline dev.

**The hosted providers are a document-egress path, not a prompt-egress path.**
Indexing sends the documents themselves to a third-party vendor, in full, one
chunk at a time. That is categorically different from the LLM chokepoint:
``maverick.privacy_egress.maybe_redact_egress`` minimizes outbound *prompts*
and is wired into ``llm.py`` only, so it has never applied here; and
``maverick.enterprise.egress_permitted`` returns True whenever enterprise mode
is off, so on a default install nothing stood between a matter's documents and
the vendor. For a practice holding privileged material that is the wrong
default, so this module adds the two things that were missing:

* **Explicit acknowledgement.** ``hosted``/``cohere`` now refuse to build
  unless ``[knowledge] allow_external_embedding = true`` (or
  ``MAVERICK_KNOWLEDGE_ALLOW_EXTERNAL_EMBEDDING=1``). Choosing to send client
  documents to a vendor stays available; making that choice silently does not.
* **A privilege-log entry.** Every batch that leaves records a
  ``knowledge_egress`` audit event -- provider, host, model, chunk count, byte
  count and a SHA-256 commitment over the batch. Bounded metadata only; the
  chunk text is never written to the audit record.

``local`` and ``deterministic`` embed on-box and need neither.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
from typing import Protocol

log = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on", "enable", "enabled", "y", "t"}
#: Providers that ship document text off the box.
EXTERNAL_PROVIDERS = ("hosted", "cohere")


def _host_of(url: str) -> str:
    """Bare host for the audit record (no scheme, path, or credentials)."""
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).hostname or ""
    except ValueError:  # pragma: no cover -- malformed base_url
        return ""


def external_embedding_allowed(cfg: dict | None = None) -> bool:
    """Has the operator acknowledged sending document text to a vendor?

    Off unless explicitly turned on. The env var is the operator escape hatch
    and wins over config, mirroring ``MAVERICK_EMBED_PROVIDER`` above.
    """
    env = os.environ.get("MAVERICK_KNOWLEDGE_ALLOW_EXTERNAL_EMBEDDING")
    if env is not None and env.strip() != "":
        return env.strip().lower() in _TRUE
    return bool((cfg or {}).get("allow_external_embedding", False))


def _refuse_external(provider: str) -> RuntimeError:
    return RuntimeError(
        f"knowledge: the {provider!r} embedder sends document text to a "
        "third-party vendor, which has not been acknowledged. Indexing a "
        "matter ships the documents themselves, not a prompt about them, so "
        "this is off by default. Either set [knowledge] "
        "allow_external_embedding = true (or "
        "MAVERICK_KNOWLEDGE_ALLOW_EXTERNAL_EMBEDDING=1) to accept that, or "
        "use embedder = 'local' to embed on-box with no egress at all."
    )


def record_external_embedding(
    *, provider: str, host: str, model: str, texts: list[str],
) -> None:
    """Write the ``knowledge_egress`` privilege-log entry for one batch.

    Bounded metadata and a content commitment only -- the chunk text is what we
    are recording the *departure* of, so writing it into the audit record would
    copy the exposure rather than document it.

    ``maverick-knowledge`` deliberately declares no dependencies, so the audit
    module is imported lazily and its absence is not an error: the package runs
    standalone, and the kernel's own rule is that it works without its optional
    layers.

    :class:`~maverick.audit.AuditRefused` is allowed to propagate, which stops
    the batch. That is the whole point of recording this one: a refusal means
    the deployment asserts a guarantee that writing would falsify, and shipping
    privileged documents to a vendor with the privilege log knowingly broken is
    the exact combination worth refusing. Incidental failures (disk full, a
    serialization error) are logged and swallowed by ``audit_event`` itself.
    """
    digest = hashlib.sha256()
    total = 0
    for t in texts:
        raw = (t or "").encode("utf-8")
        total += len(raw)
        digest.update(raw)
    try:
        from maverick.audit import EventKind, audit_event
    except ImportError:  # standalone maverick-knowledge, no kernel present
        log.debug("knowledge: audit unavailable; %s embedding egress unrecorded",
                  provider)
        return
    audit_event(
        EventKind.KNOWLEDGE_EGRESS,
        provider=provider,
        host=host,
        model=model,
        chunks=len(texts),
        bytes=total,
        content_sha256=digest.hexdigest(),
    )


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _l2(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


class DeterministicEmbedder:
    """Dependency-free hashing embedder.

    NOT semantic -- a deterministic bag-of-hashed-tokens vector so the pipeline
    runs (and tests pass) with no API key or model download. Same text -> same
    vector; lexically-overlapping texts land near each other, which is enough to
    exercise and verify the retrieval plumbing.
    """

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in (t or "").lower().split():
                h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16)
                v[h % self.dim] += 1.0
            out.append(_l2(v))
        return out


class HostedEmbedder:
    """Voyage / OpenAI-compatible embeddings over HTTP (needs an API key).

    Voyage is Anthropic's recommended embeddings partner; any OpenAI-compatible
    ``/embeddings`` endpoint also works via ``base_url`` + ``model``. ``httpx``
    is imported lazily so the package imports clean without the ``hosted`` extra.
    """

    def __init__(self, model: str, base_url: str, api_key: str, dim: int = 1024):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        # Recorded BEFORE the request: the privilege log has to show the batch
        # that left even when the vendor errors or the connection drops, since
        # the bytes are on the wire either way.
        record_external_embedding(
            provider="hosted", host=_host_of(self.base_url),
            model=self.model, texts=texts,
        )
        r = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=60,
        )
        r.raise_for_status()
        # OpenAI/Voyage-compatible responses are only guaranteed addressable by
        # each item's `index`, not positional order, so reorder before zipping
        # vectors back to their input chunks (a reordered batch would otherwise
        # pair chunk text with the wrong vector and silently corrupt the index).
        data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        vectors = [d["embedding"] for d in data]
        if len(vectors) != len(texts):
            raise ValueError(
                f"embedding API returned {len(vectors)} vectors for "
                f"{len(texts)} inputs; refusing to misalign the index"
            )
        return vectors


class CohereEmbedder:
    """Cohere embeddings (roadmap: 2027 H1 ecosystem — "Cohere embeddings").

    Cohere's ``/v2/embed`` differs from the OpenAI/Voyage shape: it takes
    ``texts`` (not ``input``), requires an ``input_type``
    (search_document / search_query), and returns embeddings nested under
    ``embeddings.float``. ``httpx`` is imported lazily. ``input_type`` defaults
    to ``search_document`` for indexing; pass ``search_query`` at query time for
    Cohere's asymmetric retrieval models.
    """

    def __init__(self, model: str, api_key: str, dim: int = 1024,
                 base_url: str = "https://api.cohere.com/v2",
                 input_type: str = "search_document"):
        self.model = model
        self.api_key = api_key
        self.dim = dim
        self.base_url = base_url.rstrip("/")
        self.input_type = input_type

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        record_external_embedding(
            provider="cohere", host=_host_of(self.base_url),
            model=self.model, texts=texts,
        )
        r = httpx.post(
            f"{self.base_url}/embed",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "texts": texts,
                "input_type": self.input_type,
                "embedding_types": ["float"],
            },
            timeout=60,
        )
        r.raise_for_status()
        body = r.json()
        # v2 nests vectors under embeddings.float; v1 returned a flat list.
        embeddings = body.get("embeddings", body)
        vectors = embeddings["float"] if isinstance(embeddings, dict) else embeddings
        if len(vectors) != len(texts):
            raise ValueError(
                f"embedding API returned {len(vectors)} vectors for "
                f"{len(texts)} inputs; refusing to misalign the index"
            )
        return vectors


def build_embedder(cfg: dict | None = None) -> Embedder:
    """Select an embedder from config. **Fails loud.**

    A configured provider that can't initialize (``hosted`` with no API key,
    ``local`` without the extra) raises rather than silently falling back to the
    non-semantic :class:`DeterministicEmbedder` -- a silent downgrade yields
    plausible-looking but meaningless retrievals a business would unknowingly
    trust. The hash embedder must be opted into: ``embedder = "deterministic"``
    (or ``MAVERICK_EMBED_PROVIDER=deterministic``), used by tests / offline dev.

    The external providers raise for a second reason: sending document text to
    a vendor requires ``allow_external_embedding`` (see the module docstring).
    That check runs before the API-key check, so an operator who has not made
    that decision is told about the decision rather than about a missing key.
    """
    cfg = cfg or {}
    # The env var is an operator escape hatch that wins over config -- e.g. to
    # force offline/deterministic retrieval without editing config.toml.
    provider = str(
        os.environ.get("MAVERICK_EMBED_PROVIDER") or cfg.get("embedder", "hosted")
    ).lower()

    if provider == "deterministic":
        return DeterministicEmbedder(int(cfg.get("dim", 256)))

    if provider in EXTERNAL_PROVIDERS and not external_embedding_allowed(cfg):
        raise _refuse_external(provider)

    if provider == "hosted":
        key = cfg.get("api_key") or os.environ.get("MAVERICK_EMBED_API_KEY", "")
        if not key:
            raise RuntimeError(
                "knowledge: hosted embedder selected but no API key (set "
                "[knowledge] api_key or MAVERICK_EMBED_API_KEY); or set "
                "embedder = 'deterministic' for offline/dev retrieval."
            )
        return HostedEmbedder(
            model=cfg.get("model", "voyage-3"),
            base_url=cfg.get("base_url", "https://api.voyageai.com/v1"),
            api_key=key,
            dim=int(cfg.get("dim", 1024)),
        )

    if provider == "cohere":
        key = cfg.get("api_key") or os.environ.get("COHERE_API_KEY") \
            or os.environ.get("MAVERICK_EMBED_API_KEY", "")
        if not key:
            raise RuntimeError(
                "knowledge: cohere embedder selected but no API key (set "
                "[knowledge] api_key, COHERE_API_KEY, or MAVERICK_EMBED_API_KEY); "
                "or set embedder = 'deterministic' for offline/dev retrieval."
            )
        return CohereEmbedder(
            model=cfg.get("model", "embed-v4.0"),
            api_key=key,
            dim=int(cfg.get("dim", 1024)),
            input_type=cfg.get("input_type", "search_document"),
        )

    if provider == "local":
        import importlib.util
        if importlib.util.find_spec("sentence_transformers") is None:
            raise RuntimeError(
                "knowledge: local embedder selected but sentence-transformers is "
                "not installed (the 'local' extra); or set "
                "embedder = 'deterministic' for offline/dev retrieval."
            )
        from .local_embed import LocalEmbedder  # optional 'local' extra
        return LocalEmbedder(cfg.get("model", "all-MiniLM-L6-v2"))

    raise ValueError(
        f"knowledge: unknown embedder provider {provider!r} "
        "(expected 'hosted', 'local', or 'deterministic')."
    )
