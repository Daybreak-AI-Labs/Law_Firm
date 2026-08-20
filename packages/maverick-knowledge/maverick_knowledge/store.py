"""Vector stores.

:class:`SqliteVectorStore` is a dependency-free brute-force cosine index --
fine for the per-practice corpora the factory produces.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_SCOPED_COLLECTION_RE = re.compile(
    r"^(?:public:[A-Za-z0-9_.-]{1,120}|matter:[1-9][0-9]*:[A-Za-z0-9_.-]{1,120})$"
)


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
    """Brute-force cosine over an encrypted local SQLite store.

    File-backed stores require Maverick's at-rest encryption and seal chunk
    text, vectors, and provenance metadata before SQLite sees them. Collection
    identifiers remain queryable and must therefore be opaque matter/domain
    namespaces, never client names. ``:memory:`` stays dependency-free for
    package tests and standalone ephemeral use.
    """

    def __init__(self, path: str | Path = ":memory:"):
        file_backed = str(path) != ":memory:"
        self._require_scoped_collections = file_backed
        self._path = Path(path) if file_backed else None
        self._seal = lambda value: value
        self._unseal = lambda value: value
        self._is_sealed = lambda _value: False
        if file_backed:
            target = Path(path)
            try:
                from maverick.crypto_at_rest import (
                    at_rest_enabled,
                    is_sealed_str,
                    seal_to_str,
                    unseal_from_str,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "knowledge: a file-backed store requires maverick-core "
                    "at-rest encryption"
                ) from exc
            if not at_rest_enabled():
                raise RuntimeError(
                    "knowledge: refusing a plaintext file-backed vector store; "
                    "enable at-rest encryption"
                )
            self._seal = seal_to_str
            self._unseal = unseal_from_str
            self._is_sealed = is_sealed_str
            # The directory is the first confidentiality boundary. Prepare or
            # verify it before SQLite can create the DB, journal, WAL, or SHM.
            # A pre-existing permissive caller-selected directory is refused,
            # not silently claimed by changing unrelated ACLs.
            try:
                from maverick.file_lock import (
                    atomic_create_bytes,
                    ensure_private_file,
                    prepare_private_directory,
                )

                prepare_private_directory(target.parent)
                for candidate in self._sqlite_paths():
                    if candidate.exists():
                        ensure_private_file(candidate)
                if not target.exists():
                    atomic_create_bytes(target, b"")
                ensure_private_file(target)
            except Exception as exc:
                raise RuntimeError(
                    "knowledge: could not prepare a private vector-store path"
                ) from exc
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
        if file_backed:
            try:
                self._secure_sqlite_files()
                self._validate_encrypted_rows()
            except Exception as exc:
                self._db.close()
                raise RuntimeError(
                    "knowledge: encrypted vector-store validation failed"
                ) from exc

    def _sqlite_paths(self) -> tuple[Path, ...]:
        if self._path is None:
            return ()
        raw = str(self._path)
        return tuple(
            Path(raw + suffix)
            for suffix in ("", "-wal", "-shm", "-journal")
        )

    def _secure_sqlite_files(self) -> None:
        if self._path is None:
            return
        from maverick.file_lock import ensure_private_file

        for candidate in self._sqlite_paths():
            if candidate.exists():
                ensure_private_file(candidate)

    def _decode_sealed(self, value: object, field: str) -> str:
        text = str(value or "")
        if self._require_scoped_collections and not self._is_sealed(text):
            raise RuntimeError(
                f"knowledge: unsealed {field} is not trusted; run an explicit "
                "authenticated offline migration"
            )
        try:
            return self._unseal(text)
        except Exception as exc:
            raise RuntimeError(f"knowledge: could not authenticate {field}") from exc

    def _decode_row(self, text: object, vec: object, meta: object):
        decoded_text = self._decode_sealed(text, "chunk text")
        try:
            decoded_vec = json.loads(self._decode_sealed(vec, "chunk vector"))
            decoded_meta = json.loads(self._decode_sealed(meta, "chunk metadata"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("knowledge: encrypted row payload is invalid") from exc
        if not isinstance(decoded_vec, list) or not isinstance(decoded_meta, dict):
            raise RuntimeError("knowledge: encrypted row payload has invalid types")
        return decoded_text, decoded_vec, decoded_meta

    def _validate_encrypted_rows(self) -> None:
        """Reject legacy/plaintext/corrupt rows; never auto-promote them."""
        if not self._require_scoped_collections:
            return
        with self._lock:
            rows = self._db.execute("SELECT text, vec, meta FROM chunks").fetchall()
        for row in rows:
            self._decode_row(*row)

    def _assert_scoped_collection(self, collection: str) -> None:
        if self._require_scoped_collections and not _SCOPED_COLLECTION_RE.fullmatch(
            str(collection or "")
        ):
            raise ValueError(
                "knowledge: file-backed collections require an exact "
                "'matter:<id>:<source>' or 'public:<source>' namespace"
            )

    def add(self, collection, items) -> None:
        self._assert_scoped_collection(collection)
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?)",
                [
                    (
                        collection,
                        cid,
                        self._seal(text),
                        self._seal(json.dumps(vec, separators=(",", ":"))),
                        self._seal(json.dumps(meta, separators=(",", ":"))),
                    )
                    for cid, text, vec, meta in items
                ],
            )
            self._db.commit()
        self._secure_sqlite_files()

    def search(self, collection, vector, k: int = 5) -> list[Match]:
        if k <= 0:
            return []
        self._assert_scoped_collection(collection)
        with self._lock:
            rows = self._db.execute(
                "SELECT text, vec, meta FROM chunks WHERE collection = ?", (collection,)
            ).fetchall()
        scored: list[Match] = []
        for text, vec, meta in rows:
            decoded_text, stored, decoded_meta = self._decode_row(text, vec, meta)
            # A query embedded with a different model/dim than the corpus would
            # silently score 0.0 against every chunk and return arbitrary
            # results. Surface that misconfiguration instead of guessing.
            if len(stored) != len(vector):
                raise ValueError(
                    f"knowledge: query vector dim {len(vector)} != stored dim "
                    f"{len(stored)} for collection {collection!r}; the corpus was "
                    "embedded with a different embedder/model than this query"
                )
            scored.append(Match(
                _cosine(vector, stored),
                decoded_text,
                decoded_meta,
            ))
        scored.sort(key=lambda m: m.score, reverse=True)
        return scored[:k]

    def delete_collection(self, collection: str) -> None:
        """Remove all chunks for one collection."""
        with self._lock:
            self._db.execute("DELETE FROM chunks WHERE collection = ?", (collection,))
            self._db.commit()
        self._secure_sqlite_files()

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
                if self._decode_sealed_json_meta(meta).get(key) == value
            ]
            if doomed:
                self._db.executemany(
                    "DELETE FROM chunks WHERE collection = ? AND id = ?",
                    [(collection, cid) for cid in doomed],
                )
                self._db.commit()
        self._secure_sqlite_files()
        return len(doomed)

    def count_where(self, collection: str, key: str, value: str) -> int:
        """Chunks whose ``meta[key] == value`` — the erasure-verify primitive."""
        with self._lock:
            rows = self._db.execute(
                "SELECT meta FROM chunks WHERE collection = ?", (collection,)
            ).fetchall()
        return sum(
            1 for (meta,) in rows
            if self._decode_sealed_json_meta(meta).get(key) == value
        )

    def _decode_sealed_json_meta(self, meta: object) -> dict[str, Any]:
        try:
            decoded = json.loads(self._decode_sealed(meta, "chunk metadata"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("knowledge: encrypted metadata is invalid") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("knowledge: encrypted metadata has invalid type")
        return decoded

    def validate_collection(self, collection: str, *, require_nonempty: bool = True) -> int:
        """Authenticate every row in one required matter collection."""
        self._assert_scoped_collection(collection)
        with self._lock:
            rows = self._db.execute(
                "SELECT text, vec, meta FROM chunks WHERE collection = ?",
                (collection,),
            ).fetchall()
        if require_nonempty and not rows:
            raise RuntimeError("knowledge: required matter collection is empty")
        for row in rows:
            self._decode_row(*row)
        return len(rows)

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


def build_store(cfg: dict | None = None):
    """Select a vector store from config: the embedded SQLite store."""
    cfg = cfg or {}
    return SqliteVectorStore(cfg.get("path") or ":memory:")
