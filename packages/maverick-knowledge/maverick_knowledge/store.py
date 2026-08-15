"""Vector stores.

:class:`SqliteVectorStore` is a dependency-free brute-force cosine index -- fine
for the per-business corpora the factory produces; pgvector is an opt-in backend
for scale (``build_store`` selects it when configured).
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass
class Match:
    score: float
    text: str
    meta: dict[str, Any]


class VectorStore(Protocol):
    def add(self, collection: str, items: list[tuple[str, str, list[float], dict]]) -> None: ...

    def search(self, collection: str, vector: list[float], k: int = 5) -> list[Match]: ...

    def delete_collection(self, collection: str) -> None: ...

    def collections(self) -> list[str]: ...

    def delete_where(self, collection: str, key: str, value: str) -> int: ...

    def count_where(self, collection: str, key: str, value: str) -> int: ...

    def count(self, collection: str) -> int: ...


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class SqliteVectorStore:
    """Brute-force cosine over vectors stored as JSON in SQLite.

    One row per chunk, scoped by ``collection`` (the domain's knowledge source),
    so retrieval is filtered per domain -- knowledge respects the compartment
    bulkheads. ``:memory:`` (the default) is used by tests.
    """

    def __init__(self, path: str | Path = ":memory:"):
        # Create the parent dir for a file-backed store (e.g. a tenant's
        # ~/.maverick/tenants/<t>/knowledge.db) -- sqlite3.connect won't, and
        # would raise on a missing directory. ":memory:" needs no dir.
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # KnowledgeBase shares one store across threads (ingestion runs under
        # asyncio.to_thread / a worker pool), so allow cross-thread use and
        # serialize access with a lock -- a single sqlite connection is not safe
        # for concurrent use.
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "collection TEXT, id TEXT, text TEXT, vec TEXT, meta TEXT, "
            "PRIMARY KEY (collection, id))"
        )
        self._db.commit()

    def add(self, collection, items) -> None:
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?)",
                [
                    (collection, cid, text, json.dumps(vec), json.dumps(meta))
                    for cid, text, vec, meta in items
                ],
            )
            self._db.commit()

    def search(self, collection, vector, k: int = 5) -> list[Match]:
        if k <= 0:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT text, vec, meta FROM chunks WHERE collection = ?", (collection,)
            ).fetchall()
        scored: list[Match] = []
        for text, vec, meta in rows:
            stored = json.loads(vec)
            # A query embedded with a different model/dim than the corpus would
            # silently score 0.0 against every chunk and return arbitrary
            # results. Surface that misconfiguration instead of guessing.
            if len(stored) != len(vector):
                raise ValueError(
                    f"knowledge: query vector dim {len(vector)} != stored dim "
                    f"{len(stored)} for collection {collection!r}; the corpus was "
                    "embedded with a different embedder/model than this query"
                )
            scored.append(Match(_cosine(vector, stored), text, json.loads(meta)))
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:k]

    def delete_collection(self, collection: str) -> None:
        """Remove all chunks for one collection."""
        with self._lock:
            self._db.execute("DELETE FROM chunks WHERE collection = ?", (collection,))
            self._db.commit()

    def collections(self) -> list[str]:
        """Every collection with at least one chunk (the erasure sweep surface)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT collection FROM chunks ORDER BY collection"
            ).fetchall()
        return [r[0] for r in rows]

    def delete_where(self, collection: str, key: str, value: str) -> int:
        """Delete every chunk whose ``meta[key] == value``. Returns the count.

        The GDPR erasure primitive: a subject's or source's chunks are removed
        by provenance, not by similarity. Meta is matched in Python (no JSON1
        dependency) — collections are per-domain corpora, not billion-row sets.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT id, meta FROM chunks WHERE collection = ?", (collection,)
            ).fetchall()
            doomed = [
                cid for cid, meta in rows
                if (json.loads(meta or "{}")).get(key) == value
            ]
            if doomed:
                self._db.executemany(
                    "DELETE FROM chunks WHERE collection = ? AND id = ?",
                    [(collection, cid) for cid in doomed],
                )
                self._db.commit()
        return len(doomed)

    def count_where(self, collection: str, key: str, value: str) -> int:
        """Chunks whose ``meta[key] == value`` — the erasure-verify primitive."""
        with self._lock:
            rows = self._db.execute(
                "SELECT meta FROM chunks WHERE collection = ?", (collection,)
            ).fetchall()
        return sum(
            1 for (meta,) in rows if (json.loads(meta or "{}")).get(key) == value
        )

    def count(self, collection: str) -> int:
        with self._lock:
            (n,) = self._db.execute(
                "SELECT COUNT(*) FROM chunks WHERE collection = ?", (collection,)
            ).fetchone()
        return n

    def close(self) -> None:
        """Close the underlying SQLite connection (idempotent)."""
        db = getattr(self, "_db", None)
        if db is not None:
            db.close()
            self._db = None

    def __enter__(self) -> SqliteVectorStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _to_pgvector(vec: list[float]) -> str:
    """A pgvector text literal (``[1,2,3]``) -- passed with a ``::vector`` cast so
    the backend needs only ``psycopg`` + the Postgres ``vector`` extension, not
    the optional ``pgvector`` Python package.

    Reject non-finite components: ``repr(float('nan'))`` is the bare token
    ``nan`` (likewise ``inf``/``-inf``), which pgvector's literal parser refuses,
    so an embedding carrying a NaN/Inf (a misbehaving hosted-API response or a
    downstream numeric bug) would otherwise abort the whole add()/search() with
    an opaque Postgres parse error mid-batch. Fail with a domain message first.
    """
    floats = [float(x) for x in vec]
    if not all(math.isfinite(x) for x in floats):
        raise ValueError(
            "knowledge: refusing to store/query a non-finite embedding component "
            "(nan/inf); the embedder produced an invalid vector"
        )
    return "[" + ",".join(repr(x) for x in floats) + "]"


class PgVectorStore:
    """pgvector-backed scale-out vector store -- the opt-in backend for corpora
    too large for the brute-force SQLite index.

    Same surface + collection-scoped compartments as :class:`SqliteVectorStore`,
    but cosine search runs in Postgres via the ``<=>`` operator (with an IVFFlat
    index), so retrieval doesn't scan every row in the process. Vectors are
    passed as text literals cast to ``::vector``; only ``psycopg`` and the
    Postgres ``vector`` extension are required (no numpy / pgvector-python).

    Connection-shared across threads like the SQLite store (ingestion runs on a
    threadpool); a single psycopg connection is NOT thread-safe, so every
    statement is serialized under a lock.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        dim: int = 1024,
        table: str = "knowledge_chunks",
        namespace: str = "default",
    ) -> None:
        try:
            import psycopg
        except ImportError as e:  # pragma: no cover -- exercised only without psycopg
            raise ImportError(
                "pgvector store needs psycopg. From the reviewed checkout run: "
                "python -m pip install -e './packages/maverick-knowledge[pgvector]'"
            ) from e
        import os

        self._dsn = (
            dsn
            or os.environ.get("MAVERICK_KNOWLEDGE_DSN")
            or os.environ.get("MAVERICK_PG_DSN")
            or ""
        )
        if not self._dsn:
            raise RuntimeError(
                "pgvector store requires MAVERICK_KNOWLEDGE_DSN / MAVERICK_PG_DSN "
                "or [knowledge] dsn in config.toml."
            )
        if dim <= 0:
            raise ValueError(f"pgvector store needs a positive dim, got {dim}")
        self._dim = int(dim)
        # Identifier is operator/config-derived, not agent input; still constrain
        # it to a safe charset so it can be interpolated into DDL without an
        # injection surface.
        if not table.replace("_", "").isalnum():
            raise ValueError(f"invalid table name {table!r}")
        self._table = table
        self._namespace = str(namespace or "default")
        self._lock = threading.Lock()
        self._db = psycopg.connect(self._dsn, autocommit=True)
        try:
            self._db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as e:  # pragma: no cover -- perms vary by deployment
            raise RuntimeError(
                "pgvector store: could not enable the 'vector' extension "
                f"({e}). Install pgvector and grant CREATE on the database."
            ) from e
        self._db.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "namespace TEXT, collection TEXT, id TEXT, text TEXT, "
            f"embedding vector({self._dim}), meta JSONB, "
            "PRIMARY KEY (namespace, collection, id))"
        )
        # Migrate a pre-namespacing table in place. Older installs created
        # {table} with PRIMARY KEY (collection, id) and no namespace column, so a
        # shared-DSN upgrade would fail every namespace-scoped query (and the
        # CREATE TABLE IF NOT EXISTS above is a no-op on the existing table). Add
        # the column, assign existing rows to the 'default' workspace, and move
        # the primary key to include namespace so the same id can live in
        # multiple workspaces and ON CONFLICT (namespace, collection, id) upserts
        # resolve. Guarded on the column's absence so it runs once, not per init.
        with self._db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = 'namespace'",
                (self._table,),
            )
            has_namespace = cur.fetchone() is not None
        if not has_namespace:
            self._db.execute(f"ALTER TABLE {self._table} ADD COLUMN namespace TEXT")
            self._db.execute(
                f"UPDATE {self._table} SET namespace = 'default' "
                "WHERE namespace IS NULL"
            )
            self._db.execute(
                f"ALTER TABLE {self._table} ALTER COLUMN namespace SET NOT NULL"
            )
            self._db.execute(
                f"ALTER TABLE {self._table} DROP CONSTRAINT IF EXISTS {self._table}_pkey"
            )
            self._db.execute(
                f"ALTER TABLE {self._table} ADD CONSTRAINT {self._table}_pkey "
                "PRIMARY KEY (namespace, collection, id)"
            )
        # Cosine IVFFlat index -- approximate but the point of the scale backend.
        self._db.execute(
            f"CREATE INDEX IF NOT EXISTS {self._table}_embedding_idx "
            f"ON {self._table} USING ivfflat (embedding vector_cosine_ops) "
            "WITH (lists = 100)"
        )

    def add(self, collection, items) -> None:
        if not items:
            return
        with self._lock, self._db.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {self._table} "
                "(namespace, collection, id, text, embedding, meta) "
                "VALUES (%s, %s, %s, %s, %s::vector, %s::jsonb) "
                "ON CONFLICT (namespace, collection, id) DO UPDATE SET "
                "text = EXCLUDED.text, embedding = EXCLUDED.embedding, "
                "meta = EXCLUDED.meta",
                [
                    (
                        self._namespace,
                        collection,
                        cid,
                        text,
                        _to_pgvector(vec),
                        json.dumps(meta),
                    )
                    for cid, text, vec, meta in items
                ],
            )

    def search(self, collection, vector, k: int = 5) -> list[Match]:
        if k <= 0:
            return []
        if len(vector) != self._dim:
            # Mirror the SQLite store: a query embedded at a different dim than
            # the corpus is a misconfiguration, not an empty result.
            raise ValueError(
                f"knowledge: query vector dim {len(vector)} != store dim "
                f"{self._dim}; the corpus was embedded with a different "
                "embedder/model than this query"
            )
        with self._lock, self._db.cursor() as cur:
            rows = cur.execute(
                f"SELECT text, meta, 1 - (embedding <=> %s::vector) AS score "
                f"FROM {self._table} WHERE namespace = %s AND collection = %s "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (
                    _to_pgvector(vector),
                    self._namespace,
                    collection,
                    _to_pgvector(vector),
                    int(k),
                ),
            ).fetchall()
        # psycopg adapts JSONB to a dict already; tolerate a str just in case.
        return [
            Match(float(score), text, meta if isinstance(meta, dict) else json.loads(meta or "{}"))
            for text, meta, score in rows
        ]

    def delete_collection(self, collection: str) -> None:
        with self._lock:
            self._db.execute(
                f"DELETE FROM {self._table} WHERE namespace = %s AND collection = %s",
                (self._namespace, collection),
            )

    def collections(self) -> list[str]:
        """Every collection with at least one chunk in this namespace."""
        with self._lock, self._db.cursor() as cur:
            rows = cur.execute(
                f"SELECT DISTINCT collection FROM {self._table} "
                "WHERE namespace = %s ORDER BY collection",
                (self._namespace,),
            ).fetchall()
        return [r[0] for r in rows]

    def delete_where(self, collection: str, key: str, value: str) -> int:
        """Delete every chunk whose ``meta[key] == value``. Returns the count.

        JSONB containment (``@>``) keeps the erasure sweep in Postgres.
        """
        with self._lock, self._db.cursor() as cur:
            cur.execute(
                f"DELETE FROM {self._table} "
                "WHERE namespace = %s AND collection = %s AND meta @> %s::jsonb",
                (self._namespace, collection, json.dumps({key: value})),
            )
            return int(cur.rowcount or 0)

    def count_where(self, collection: str, key: str, value: str) -> int:
        with self._lock, self._db.cursor() as cur:
            (n,) = cur.execute(
                f"SELECT COUNT(*) FROM {self._table} "
                "WHERE namespace = %s AND collection = %s AND meta @> %s::jsonb",
                (self._namespace, collection, json.dumps({key: value})),
            ).fetchone()
        return int(n)

    def count(self, collection: str) -> int:
        with self._lock, self._db.cursor() as cur:
            (n,) = cur.execute(
                f"SELECT COUNT(*) FROM {self._table} WHERE namespace = %s AND collection = %s",
                (self._namespace, collection),
            ).fetchone()
        return int(n)

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        db = getattr(self, "_db", None)
        if db is not None:
            db.close()
            self._db = None

    def __enter__(self) -> PgVectorStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class QdrantStore:
    """Qdrant-backed vector store — the certified external engine for clients
    with dedicated retrieval infrastructure or corpora beyond pgvector's
    comfortable range.

    Same protocol + per-collection compartments as the other stores. One Qdrant
    collection per (namespace, knowledge collection) pair, named
    ``{namespace}__{collection}``, so tenant isolation survives a shared
    cluster. Payload carries ``text`` + the chunk ``meta`` (flattened under
    ``meta``), and ``delete_where``/``count_where`` filter on ``meta.{key}`` —
    the same erasure-by-provenance primitive the embedded stores expose.

    Requires the optional ``qdrant-client`` dependency
    (``pip install 'maverick-knowledge[qdrant]'``); imported lazily so the
    package stays dependency-free by default.
    """

    def __init__(self, url: str | None = None, *, api_key: str | None = None,
                 dim: int = 1024, namespace: str = "default") -> None:
        try:
            from qdrant_client import QdrantClient
        except ImportError as e:  # pragma: no cover -- exercised only without the extra
            raise ImportError(
                "qdrant store needs qdrant-client. "
                "Run: pip install 'maverick-knowledge[qdrant]'"
            ) from e
        import os

        self._url = url or os.environ.get("QDRANT_URL") or ""
        if not self._url:
            raise RuntimeError(
                "qdrant store requires QDRANT_URL or [knowledge] url in config.toml.")
        if dim <= 0:
            raise ValueError(f"qdrant store needs a positive dim, got {dim}")
        self._dim = int(dim)
        self._namespace = str(namespace or "default")
        self._client = QdrantClient(
            url=self._url, api_key=api_key or os.environ.get("QDRANT_API_KEY") or None)

    def _cname(self, collection: str) -> str:
        return f"{self._namespace}__{collection}"

    def _ensure(self, collection: str) -> str:
        from qdrant_client import models
        name = self._cname(collection)
        if not self._client.collection_exists(name):
            self._client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=self._dim, distance=models.Distance.COSINE),
            )
        return name

    @staticmethod
    def _point_id(cid: str) -> str:
        """Qdrant point ids must be UUIDs or ints; map arbitrary chunk ids
        deterministically so re-adding the same id upserts, matching the
        other stores' INSERT OR REPLACE semantics."""
        import uuid as _uuid
        return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"maverick-chunk:{cid}"))

    def _meta_filter(self, key: str, value: str):
        from qdrant_client import models
        return models.Filter(must=[models.FieldCondition(
            key=f"meta.{key}", match=models.MatchValue(value=value))])

    def add(self, collection, items) -> None:
        if not items:
            return
        from qdrant_client import models
        name = self._ensure(collection)
        self._client.upsert(
            collection_name=name,
            points=[
                models.PointStruct(
                    id=self._point_id(cid),
                    vector=[float(x) for x in vec],
                    payload={"text": text, "meta": dict(meta or {}), "chunk_id": cid},
                )
                for cid, text, vec, meta in items
            ],
        )

    def search(self, collection, vector, k: int = 5) -> list[Match]:
        if k <= 0:
            return []
        if len(vector) != self._dim:
            raise ValueError(
                f"knowledge: query vector dim {len(vector)} != store dim "
                f"{self._dim}; the corpus was embedded with a different "
                "embedder/model than this query")
        name = self._cname(collection)
        if not self._client.collection_exists(name):
            return []
        hits = self._client.query_points(
            collection_name=name, query=[float(x) for x in vector], limit=int(k),
            with_payload=True,
        ).points
        return [
            Match(
                float(h.score),
                str((h.payload or {}).get("text", "")),
                dict((h.payload or {}).get("meta") or {}),
            )
            for h in hits
        ]

    def delete_collection(self, collection: str) -> None:
        name = self._cname(collection)
        if self._client.collection_exists(name):
            self._client.delete_collection(name)

    def collections(self) -> list[str]:
        prefix = f"{self._namespace}__"
        names = [c.name for c in self._client.get_collections().collections]
        return sorted(n[len(prefix):] for n in names if n.startswith(prefix))

    def delete_where(self, collection: str, key: str, value: str) -> int:
        from qdrant_client import models
        name = self._cname(collection)
        if not self._client.collection_exists(name):
            return 0
        doomed = self.count_where(collection, key, value)
        if doomed:
            self._client.delete(
                collection_name=name,
                points_selector=models.FilterSelector(
                    filter=self._meta_filter(key, value)),
            )
        return doomed

    def count_where(self, collection: str, key: str, value: str) -> int:
        name = self._cname(collection)
        if not self._client.collection_exists(name):
            return 0
        return int(self._client.count(
            collection_name=name, count_filter=self._meta_filter(key, value),
            exact=True,
        ).count)

    def count(self, collection: str) -> int:
        name = self._cname(collection)
        if not self._client.collection_exists(name):
            return 0
        return int(self._client.count(collection_name=name, exact=True).count)

    def close(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover -- close is best-effort
                pass
            self._client = None


def _namespace_from_cfg(cfg: dict) -> str:
    """Stable pgvector namespace preserving per-workspace knowledge isolation."""
    raw = cfg.get("namespace") or cfg.get("tenant") or cfg.get("path") or "default"
    namespace = str(raw)
    digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:16]
    return f"workspace:{digest}"


def build_store(cfg: dict | None = None):
    """Select a vector store from config. Defaults to embedded SQLite; pgvector
    (``[knowledge] store = "pgvector"``) is the opt-in scale backend.

    pgvector reads its DSN from ``[knowledge] dsn`` /
    ``MAVERICK_KNOWLEDGE_DSN`` / ``MAVERICK_PG_DSN`` and its vector width from
    ``[knowledge] dim`` (default 1024 -- matches ``voyage-3``)."""
    cfg = cfg or {}
    backend = str(cfg.get("store", "sqlite")).lower()
    if backend == "pgvector":
        return PgVectorStore(
            dsn=cfg.get("dsn") or None,
            dim=int(cfg.get("dim", 1024)),
            namespace=_namespace_from_cfg(cfg),
        )
    if backend == "qdrant":
        return QdrantStore(
            url=cfg.get("url") or None,
            api_key=cfg.get("api_key") or None,
            dim=int(cfg.get("dim", 1024)),
            namespace=_namespace_from_cfg(cfg),
        )
    return SqliteVectorStore(cfg.get("path") or ":memory:")
