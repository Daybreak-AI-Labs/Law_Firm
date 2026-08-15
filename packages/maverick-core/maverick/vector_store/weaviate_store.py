"""Weaviate vector store adapter (roadmap: 2027 H1 ecosystem — "Weaviate vector store").

Embedding-backed memory using Weaviate. Mirrors the Chroma / Qdrant adapter API
(``add(docs)``, ``query(text, top_k)``, ``delete(ids)``, ``count()``) so callers
swap one for another with no code change.

Uses Weaviate's embedded mode by default (no separate server) and a remote
cluster when configured:
  - ``MAVERICK_WEAVIATE_URL``       -> remote HTTP(S) server URL (overrides embedded)
  - ``MAVERICK_WEAVIATE_API_KEY``   -> optional remote API key
  - ``MAVERICK_WEAVIATE_GRPC_HOST`` -> optional gRPC host (defaults to URL host)
  - ``MAVERICK_WEAVIATE_GRPC_PORT`` -> optional gRPC port (defaults to 50051,
    or 443 for a standard HTTPS endpoint)

Vectorization is delegated to Weaviate's configured vectorizer module, so the
collection embeds text server-side — the same "no extra wiring" stance as the
Qdrant adapter's fastembed integration. The module is set on the collection at
creation time (``Configure.Vectorizer``); it defaults to ``text2vec-transformers``
and is overridable via ``MAVERICK_WEAVIATE_VECTORIZER`` (or the ``vectorizer=``
arg) to any ``Configure.Vectorizer`` module enabled on your server
(``text2vec-openai``, ``text2vec-ollama``, ``text2vec-contextionary``, ...).
The chosen module MUST be enabled on the Weaviate server, or ``create`` fails
fast; set ``vectorizer="none"`` only if you supply your own vectors (then
``query()`` semantic search is unavailable). Optional dep behind the
``[weaviate]`` extra.
"""
from __future__ import annotations

import logging
import os
import uuid as _uuid
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def _active_tenant() -> str | None:
    """Tenant scope for vector ids, or ``None`` for legacy single-tenant use."""
    try:
        from ..paths import current_tenant
        return current_tenant()
    except Exception:  # pragma: no cover -- tenancy never blocks vector ops
        return None


def _stored_id(doc_id: str, tenant_id: str | None) -> str:
    """Namespace an id by tenant so one tenant can't delete another's vectors
    -- the same scheme the pgvector adapter uses for its row ids."""
    if tenant_id is None:
        return doc_id
    return f"tenant:{tenant_id}:{doc_id}"


def _uuid_for(doc_id: str) -> str:
    """Weaviate object IDs must be UUIDs; map an arbitrary string id onto a
    stable UUID5 so re-adding the same id upserts rather than duplicates."""
    try:
        return str(_uuid.UUID(doc_id))
    except (ValueError, AttributeError, TypeError):
        return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"maverick:{doc_id}"))


class WeaviateStore:
    """Thin wrapper over weaviate-client v4. Lazy import: the client is heavy."""

    def __init__(
        self,
        collection: str = "Maverick",
        url: str | None = None,
        api_key: str | None = None,
        vectorizer: str | None = None,
    ):
        try:
            import weaviate  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "weaviate-client not installed. Run: python -m pip install -e './packages/maverick-core[weaviate]'"
            ) from e
        import weaviate

        url = url or os.environ.get("MAVERICK_WEAVIATE_URL")
        api_key = api_key or os.environ.get("MAVERICK_WEAVIATE_API_KEY")
        # Server-side embedding module (see module docstring). Default to
        # text2vec-transformers; "none" means the caller supplies vectors.
        self._vectorizer = (
            vectorizer
            or os.environ.get("MAVERICK_WEAVIATE_VECTORIZER")
            or "text2vec-transformers"
        )

        if url:
            self._client = self._connect_remote(weaviate, url, api_key)
        else:
            self._client = weaviate.connect_to_embedded()

        # Weaviate collection names are capitalized GraphQL classes.
        self._collection = collection[:1].upper() + collection[1:]
        self._ensure_collection()

    @staticmethod
    def _connect_remote(weaviate, url: str, api_key: str | None):
        """Connect a configured remote endpoint with the v4 custom helper.

        ``connect_to_local`` accepts a bare local hostname, not a full URL, and
        ``connect_to_weaviate_cloud`` is not a generic self-hosted API-key
        connector. Parse the documented URL once, then supply the HTTP and
        gRPC settings explicitly so self-hosted deployments work with or
        without an API key.
        """
        raw = str(url or "").strip()
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                "MAVERICK_WEAVIATE_URL must be an http(s) URL with a hostname"
            )
        if parsed.username or parsed.password or parsed.path not in {"", "/"} \
                or parsed.params or parsed.query or parsed.fragment:
            raise ValueError(
                "MAVERICK_WEAVIATE_URL must contain only scheme, host, and optional port"
            )
        try:
            http_port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as e:
            raise ValueError("MAVERICK_WEAVIATE_URL contains an invalid port") from e

        grpc_host = os.environ.get("MAVERICK_WEAVIATE_GRPC_HOST", "").strip()
        grpc_host = grpc_host or parsed.hostname
        default_grpc_port = 443 if http_port == 443 else 50051
        raw_grpc_port = os.environ.get("MAVERICK_WEAVIATE_GRPC_PORT", "").strip()
        try:
            grpc_port = int(raw_grpc_port) if raw_grpc_port else default_grpc_port
        except ValueError as e:
            raise ValueError("MAVERICK_WEAVIATE_GRPC_PORT must be an integer") from e
        if not 1 <= grpc_port <= 65535:
            raise ValueError("MAVERICK_WEAVIATE_GRPC_PORT must be between 1 and 65535")

        secure = parsed.scheme.lower() == "https"
        kwargs: dict[str, Any] = {
            "http_host": parsed.hostname,
            "http_port": http_port,
            "http_secure": secure,
            "grpc_host": grpc_host,
            "grpc_port": grpc_port,
            "grpc_secure": secure,
        }
        if api_key:
            from weaviate.classes.init import Auth
            kwargs["auth_credentials"] = Auth.api_key(api_key)
        return weaviate.connect_to_custom(**kwargs)

    def _vectorizer_config(self):
        """Build the ``Configure.Vectorizer`` config for the chosen module, or
        ``None`` to create a vector-less collection (``vectorizer="none"``)."""
        name = (self._vectorizer or "").strip()
        if not name or name.lower() == "none":
            log.warning(
                "weaviate: collection %s created with no vectorizer; semantic "
                "query() is unavailable (supply your own vectors)", self._collection)
            return None
        import weaviate
        try:  # the submodule is usually loaded by `import weaviate` in v4
            configure = weaviate.classes.config.Configure
        except AttributeError:  # pragma: no cover -- import the submodule explicitly
            from weaviate.classes.config import Configure as configure
        builder = getattr(configure.Vectorizer, name.replace("-", "_"), None)
        if builder is None:
            raise ValueError(
                f"unknown weaviate vectorizer {name!r}; set MAVERICK_WEAVIATE_VECTORIZER "
                "to a Configure.Vectorizer module (e.g. text2vec-transformers, "
                "text2vec-openai, text2vec-ollama) or 'none'")
        return builder()

    def _ensure_collection(self) -> None:
        try:
            if not self._client.collections.exists(self._collection):
                vec_cfg = self._vectorizer_config()
                if vec_cfg is None:
                    self._client.collections.create(self._collection)
                else:
                    self._client.collections.create(
                        self._collection, vectorizer_config=vec_cfg)
        except Exception as e:  # pragma: no cover -- backend-specific
            # A usable store requires its collection. Continuing after a failed
            # create only defers the error to an unrelated add/query call.
            raise RuntimeError(
                f"weaviate: could not ensure collection {self._collection!r}"
            ) from e

    def add(
        self,
        documents: list[str],
        *,
        ids: list[str] | None = None,
        metadatas: list[dict] | None = None,
    ) -> None:
        if not documents:
            return
        if ids is None:
            ids = [str(_uuid.uuid4()) for _ in documents]
        if len(ids) != len(documents):
            raise ValueError(f"ids length {len(ids)} != documents length {len(documents)}")
        if metadatas is not None and len(metadatas) != len(documents):
            raise ValueError(
                f"metadatas length {len(metadatas)} != documents length {len(documents)}")
        tenant_id = _active_tenant()
        coll = self._client.collections.get(self._collection)
        with coll.batch.dynamic() as batch:
            for i, doc in enumerate(documents):
                props = {"document": doc}
                if metadatas:
                    props.update(metadatas[i] or {})
                # Stamp the tenant so query()/count() can filter by it. The
                # tenant-namespaced UUID alone only protects delete-by-id;
                # near_text semantic search ignores the id namespace, so
                # without this property one tenant's query would return
                # another tenant's documents.
                if tenant_id is not None:
                    props["_tenant"] = tenant_id
                batch.add_object(
                    properties=props,
                    uuid=_uuid_for(_stored_id(ids[i], tenant_id)),
                )

    @staticmethod
    def _tenant_filter(tenant_id: str | None):
        """A Weaviate v4 ``Filter`` scoping to this tenant, or None (single-tenant)."""
        if tenant_id is None:
            return None
        from weaviate.classes.query import Filter
        return Filter.by_property("_tenant").equal(tenant_id)

    def query(self, text: str, *, top_k: int = 5) -> list[dict]:
        if not text:
            return []
        coll = self._client.collections.get(self._collection)
        near_kwargs: dict = {"query": text, "limit": max(1, min(top_k, 100))}
        tfilter = self._tenant_filter(_active_tenant())
        if tfilter is not None:
            near_kwargs["filters"] = tfilter
        res = coll.query.near_text(**near_kwargs)
        out: list[dict] = []
        for obj in getattr(res, "objects", []) or []:
            props = dict(getattr(obj, "properties", {}) or {})
            document = props.pop("document", "") or ""
            meta = getattr(obj, "metadata", None)
            distance = getattr(meta, "distance", None) if meta else None
            dist_f = float(distance) if isinstance(distance, (int, float)) else None
            score = (1.0 - dist_f) if dist_f is not None else None
            out.append({
                "id": str(getattr(obj, "uuid", "")),
                "document": document,
                "score": score,
                "distance": dist_f,
                "metadata": props or None,
            })
        return out

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        tenant_id = _active_tenant()
        coll = self._client.collections.get(self._collection)
        for doc_id in ids:
            try:
                coll.data.delete_by_id(_uuid_for(_stored_id(doc_id, tenant_id)))
            except Exception as e:  # pragma: no cover -- backend-specific
                log.debug("weaviate: delete %s failed: %s", doc_id, e)

    def count(self) -> int:
        try:
            coll = self._client.collections.get(self._collection)
            # Scope the count to the active tenant (matches pgvector) so one
            # tenant can't read another's document count through the aggregate.
            tfilter = self._tenant_filter(_active_tenant())
            if tfilter is not None:
                res = coll.aggregate.over_all(total_count=True, filters=tfilter)
            else:
                res = coll.aggregate.over_all(total_count=True)
            return int(getattr(res, "total_count", 0) or 0)
        except Exception:
            return 0

    def reset(self) -> None:
        tenant_id = _active_tenant()
        if tenant_id is None:
            # Single-tenant: drop and recreate the whole collection (fast).
            try:
                self._client.collections.delete(self._collection)
            except Exception:
                pass
            self._ensure_collection()
            return
        # Multi-tenant: delete ONLY this tenant's objects. Dropping the shared
        # collection here would wipe EVERY other tenant's vectors too -- the same
        # isolation add()/query()/delete()/count() already enforce via _tenant.
        try:
            coll = self._client.collections.get(self._collection)
            coll.data.delete_many(where=self._tenant_filter(tenant_id))
        except Exception as e:  # pragma: no cover -- backend-specific
            log.debug("weaviate: tenant-scoped reset failed: %s", e)


__all__ = ["WeaviateStore"]
