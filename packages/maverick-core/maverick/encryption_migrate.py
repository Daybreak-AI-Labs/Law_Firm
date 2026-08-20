"""Seal existing plaintext data after at-rest encryption is enabled.

Enabling at-rest encryption (``[encryption] at_rest``) only seals **new** writes;
rows written before are left plaintext and read back transparently (lazy
migration). This module force-seals that pre-existing plaintext so the whole
store is encrypted, not just new data. It is **idempotent** — already-sealed
values are skipped — so it is safe to re-run.

The reseal happens **in place** and shreds the pre-encryption plaintext residue
(``secure_delete`` + VACUUM). Operators who need a rollback point must create an
authenticated encrypted backup before starting; this module has no plaintext
backup path.

Exposed as ``maverick encryption migrate [--dry-run]``. Operators who need a
rollback point must use the authenticated encrypted backup command first.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

from .crypto_at_rest import (
    EncryptionUnavailable,
    at_rest_enabled,
    is_sealed_str,
    lookup_digest,
    seal_to_str,
    unseal_from_str,
)
from .file_lock import (
    atomic_create_bytes,
    atomic_read_bytes,
    cross_process_lock,
    ensure_private_file,
)

log = logging.getLogger(__name__)

# (table, text-column) pairs that at-rest encryption seals. Names come from this
# fixed allow-set -- never user input -- so the f-string interpolation below is
# injection-free (same discipline as audit/retention.py).
_SEALED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("turns", "content"),
    ("facts", "value"),
    ("messages", "content"),
    ("questions", "question"),
    ("questions", "answer"),
    ("goals", "title"),
    ("goals", "description"),
    ("goals", "result"),
    ("goal_events", "content"),
    ("episodes", "summary"),
    ("episodes", "outcome"),
    ("approvals", "action"),
    ("approvals", "scope"),
    ("approvals", "detail"),
    ("signoffs", "note"),
    ("artifacts", "title"),
    ("artifacts", "content"),
    ("clients", "name"),
    ("projects", "name"),
    ("projects", "description"),
    ("projects", "matter_number"),
    ("projects", "jurisdiction"),
    ("matter_parties", "name"),
    ("attachments", "filename"),
    ("attachments", "path"),
    ("fact_history", "value"),
    ("matter_turns", "content"),
    ("goal_feedback", "note"),
)

_ATTACHMENT_JOURNAL_SUFFIX = ".attachment-migration.journal"
_ATTACHMENT_JOURNAL_VERSION = 1


def _clear_stored_text(value: str) -> str:
    return unseal_from_str(value) if is_sealed_str(value) else value


def _attachment_file_moves(
    conn: sqlite3.Connection,
) -> list[tuple[int, Path, Path, str]]:
    """Plan verified legacy-name -> opaque-digest attachment moves.

    Metadata encryption alone is insufficient when a pre-migration pathname
    still contains the client's original filename. Existing ciphertext is
    authenticated before it is moved, and an occupied destination fails the
    migration instead of being overwritten.
    """
    try:
        rows = conn.execute(
            "SELECT rowid AS rid, path, sha256 FROM attachments"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    from .attachments import read_bytes

    moves: list[tuple[int, Path, Path, str]] = []
    for row in rows:
        stored_path = row["path"]
        digest = str(row["sha256"] or "").lower()
        if not stored_path or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            continue
        source = Path(_clear_stored_text(str(stored_path)))
        opaque_legacy_name = f"{digest}-{int(row['rid'])}"
        if source.name in {digest, opaque_legacy_name} or not os.path.lexists(source):
            continue
        # A goal may hold the same bytes under multiple historical filenames.
        # Include the metadata row id so every legacy file has a deterministic,
        # non-sensitive destination without overwriting another ciphertext.
        destination = source.with_name(opaque_legacy_name)
        if os.path.lexists(destination):
            raise RuntimeError(
                "attachment metadata migration refused an occupied digest path"
            )
        read_bytes(source, digest)
        moves.append((int(row["rid"]), source, destination, digest))
    return moves


def _apply_attachment_file_moves(
    moves: list[tuple[int, Path, Path, str]],
) -> list[tuple[int, Path, Path, str]]:
    from .attachments import read_bytes

    moved: list[tuple[int, Path, Path, str]] = []
    try:
        for move in moves:
            _, source, destination, digest = move
            source.rename(destination)
            moved.append(move)
            read_bytes(destination, digest)
    except Exception:
        _rollback_attachment_file_moves(moved)
        raise
    return moved


def _rollback_attachment_file_moves(
    moved: list[tuple[int, Path, Path, str]],
) -> None:
    for rid, source, destination, _ in reversed(moved):
        try:
            if os.path.lexists(destination) and not os.path.lexists(source):
                destination.rename(source)
        except OSError:
            log.critical(
                "attachment metadata migration could not roll back row %s",
                rid,
                exc_info=True,
            )


def _attachment_journal_path(db_path: Path) -> Path:
    return Path(f"{db_path}{_ATTACHMENT_JOURNAL_SUFFIX}")


def _same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _write_attachment_move_journal(
    db_path: Path,
    moves: list[tuple[int, Path, Path, str]],
) -> Path | None:
    """Durably record rename intent without exposing legacy client filenames."""
    if not moves:
        return None
    journal = _attachment_journal_path(db_path)
    payload = {
        "version": _ATTACHMENT_JOURNAL_VERSION,
        "moves": [
            {
                "row_id": rid,
                "source": str(source),
                "destination": str(destination),
                "sha256": digest,
            }
            for rid, source, destination, digest in moves
        ],
    }
    sealed = seal_to_str(
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
    ).encode("ascii")
    atomic_create_bytes(journal, sealed)
    ensure_private_file(journal)
    return journal


def _read_attachment_move_journal(
    journal: Path,
) -> list[tuple[int, Path, Path, str]]:
    ensure_private_file(journal)
    try:
        token = atomic_read_bytes(journal).decode("ascii")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("attachment migration journal is unreadable") from exc
    if not is_sealed_str(token):
        raise RuntimeError("attachment migration journal is not encrypted")
    try:
        payload = json.loads(unseal_from_str(token))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("attachment migration journal is invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != (
        _ATTACHMENT_JOURNAL_VERSION
    ):
        raise RuntimeError("attachment migration journal has an unknown version")
    raw_moves = payload.get("moves")
    if not isinstance(raw_moves, list) or not raw_moves:
        raise RuntimeError("attachment migration journal has no moves")
    moves: list[tuple[int, Path, Path, str]] = []
    for raw in raw_moves:
        if not isinstance(raw, dict):
            raise RuntimeError("attachment migration journal is malformed")
        rid = raw.get("row_id")
        source_raw = raw.get("source")
        destination_raw = raw.get("destination")
        digest = raw.get("sha256")
        if (
            not isinstance(rid, int)
            or isinstance(rid, bool)
            or rid <= 0
            or not isinstance(source_raw, str)
            or not source_raw
            or not isinstance(destination_raw, str)
            or not destination_raw
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise RuntimeError("attachment migration journal is malformed")
        source = Path(source_raw)
        destination = Path(destination_raw)
        if (
            source.parent != destination.parent
            or destination.name != f"{digest}-{rid}"
            or _same_path(source, destination)
        ):
            raise RuntimeError("attachment migration journal move is invalid")
        moves.append((rid, source, destination, digest))
    return moves


def _place_verified_attachment(
    source: Path,
    destination: Path,
    digest: str,
    *,
    committed: bool,
) -> None:
    from .attachments import read_bytes

    expected = destination if committed else source
    alternate = source if committed else destination
    expected_exists = os.path.lexists(expected)
    alternate_exists = os.path.lexists(alternate)
    if expected_exists == alternate_exists:
        raise RuntimeError("attachment migration file state is ambiguous")
    if alternate_exists:
        read_bytes(alternate, digest)
        alternate.rename(expected)
    read_bytes(expected, digest)


def _recover_attachment_move_journal(
    conn: sqlite3.Connection,
    db_path: Path,
) -> str | None:
    """Repair a crash between attachment rename and the SQLite commit.

    SQLite makes all metadata updates atomic. Therefore every journal row must
    still reference its source (roll the files back) or every row must reference
    its destination (finish the committed move). Mixed state is refused.
    """
    journal = _attachment_journal_path(db_path)
    if not os.path.lexists(journal):
        return None
    moves = _read_attachment_move_journal(journal)
    states: set[str] = set()
    for rid, source, destination, _ in moves:
        row = conn.execute(
            "SELECT path FROM attachments WHERE rowid = ?", (rid,)
        ).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError("attachment migration journal row is missing")
        current = _clear_stored_text(str(row[0]))
        if _same_path(current, source):
            states.add("rolled_back")
        elif _same_path(current, destination):
            states.add("committed")
        else:
            raise RuntimeError("attachment migration journal path does not match DB")
    if len(states) != 1:
        raise RuntimeError("attachment migration journal has mixed DB state")
    state = states.pop()
    committed = state == "committed"
    for _, source, destination, digest in moves:
        _place_verified_attachment(
            source, destination, digest, committed=committed
        )
    journal.unlink()
    return state


def _plaintext_work(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
    report: dict[str, int],
) -> list[tuple[str, str, int, str]]:
    """Scan sealed columns and return the exact plaintext cells to rewrite."""
    work: list[tuple[str, str, int, str]] = []
    for table, col in _SEALED_COLUMNS:
        try:
            rows = conn.execute(
                f"SELECT rowid AS rid, {col} AS val FROM {table}"
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        pending = [
            row for row in rows
            if row["val"] is not None and not is_sealed_str(row["val"])
        ]
        report[f"{table}.{col}"] = len(pending)
        if not dry_run:
            work.extend((table, col, row["rid"], row["val"]) for row in pending)
    try:
        rows = conn.execute(
            "SELECT rowid AS rid, row AS val FROM harness_corpus "
            "WHERE kind IN ('pending', 'rejected')"
        ).fetchall()
    except sqlite3.OperationalError:
        return work
    pending = [
        row for row in rows
        if row["val"] is not None and not is_sealed_str(row["val"])
    ]
    report["harness_corpus.row"] = len(pending)
    if not dry_run:
        work.extend(
            ("harness_corpus", "row", row["rid"], row["val"])
            for row in pending
        )
    return work


def _artifact_title_key_work(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
    report: dict[str, int],
) -> list[tuple[int, str]]:
    """Backfill keyed equality indexes from authenticated artifact titles."""
    try:
        rows = conn.execute(
            "SELECT rowid AS rid, goal_id, title, title_key FROM artifacts"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    pending: list[tuple[int, str]] = []
    for row in rows:
        clear_title = _clear_stored_text(str(row["title"] or ""))
        expected = lookup_digest(
            clear_title, purpose=f"artifact-title:goal:{int(row['goal_id'])}"
        )
        if row["title_key"] != expected:
            pending.append((int(row["rid"]), expected))
    report["artifacts.title_key"] = len(pending)
    return [] if dry_run else pending


def migrate_world_db(
    db_path: Path, *, dry_run: bool = False
) -> dict[str, int]:
    """Seal any remaining plaintext in the world DB's sensitive columns.

    Returns a ``{"table.column": rows_sealed}`` report. Requires at-rest
    encryption to be enabled (so the key is configured); raises
    :class:`EncryptionUnavailable` otherwise, or if the crypto backend / key is
    missing -- this never writes plaintext.

    No plaintext rollback copy is written. Use ``maverick backup create`` to
    produce an authenticated encrypted backup before this operation when needed.
    """
    if not at_rest_enabled():
        raise EncryptionUnavailable(
            "at-rest encryption is not enabled; set [encryption] at_rest = true "
            "(or MAVERICK_ENCRYPT_AT_REST=1) before migrating"
        )
    db_path = Path(db_path)
    if not db_path.exists():
        return {}
    # Encryption migration also owns the one-run legacy upgrade path. In
    # particular, pre-v40 databases need the title_key column before artifact
    # titles can be sealed and their goal-scoped equality keys backfilled.
    # WorldModel serializes and transactionally applies ordered schema changes;
    # automatic plaintext rollback copies are intentionally not produced.
    from .world_model import SCHEMA_VERSION, WorldModel

    if dry_run:
        with sqlite3.connect(str(db_path)) as version_conn:
            row = version_conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
        if row is None or int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(
                "schema upgrade is required before a no-write dry run"
            )
    else:
        world = WorldModel(db_path)
        world.close()
    with cross_process_lock(db_path, strict=True):
        return _migrate_world_db_locked(db_path, dry_run=dry_run)


def _migrate_world_db_locked(
    db_path: Path, *, dry_run: bool
) -> dict[str, int]:
    report: dict[str, int] = {}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if dry_run and os.path.lexists(_attachment_journal_path(db_path)):
            raise RuntimeError(
                "an attachment migration recovery journal requires an apply run"
            )
        # A prior process may have died after moving one or more ciphertext
        # files but before (or just after) the atomic metadata commit. Repair
        # that state before planning new work.
        _recover_attachment_move_journal(conn, db_path)
        # Phase 1 -- read-only scan: find the plaintext rows. No transaction/lock
        # is held after this (default isolation only locks on DML), so the backup
        # below can open its own connection cleanly.
        work = _plaintext_work(conn, dry_run=dry_run, report=report)
        artifact_title_keys = _artifact_title_key_work(
            conn, dry_run=dry_run, report=report
        )
        attachment_moves = _attachment_file_moves(conn)
        report["attachments.files_renamed"] = len(attachment_moves)
        if dry_run or (not work and not artifact_title_keys and not attachment_moves):
            log.info("encryption migrate (dry_run=%s): %s", dry_run, report)
            return report
        # Phase 2 -- durably journal attachment moves, then reseal in place.
        _write_attachment_move_journal(db_path, attachment_moves)
        moved = _apply_attachment_file_moves(attachment_moves)
        path_overrides = {
            rid: str(destination) for rid, _, destination, _ in moved
        }
        rewritten: list[tuple[str, str, int, str]] = []
        rewritten_path_rows: set[int] = set()
        for table, col, rid, val in work:
            if table == "attachments" and col == "path" and rid in path_overrides:
                val = path_overrides[rid]
                rewritten_path_rows.add(rid)
            rewritten.append((table, col, rid, val))
        # A path may already be sealed from an interrupted/operator migration
        # while its legacy-named file still exists. Re-seal the corrected path.
        for rid, new_path in path_overrides.items():
            if rid not in rewritten_path_rows:
                rewritten.append(("attachments", "path", rid, new_path))
        # Zero freed cells as rows are re-sealed in place, so the pre-encryption
        # plaintext can't be recovered from the DB file's free list.
        try:
            conn.execute("PRAGMA secure_delete=ON")
            for table, col, rid, val in rewritten:
                conn.execute(
                    f"UPDATE {table} SET {col} = ? WHERE rowid = ?",
                    (seal_to_str(val), rid),
                )
            for rid, title_key in artifact_title_keys:
                conn.execute(
                    "UPDATE artifacts SET title_key = ? WHERE rowid = ?",
                    (title_key, rid),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            try:
                _recover_attachment_move_journal(conn, db_path)
            except Exception as recovery_error:
                raise RuntimeError(
                    "attachment migration failed and its encrypted recovery "
                    "journal was retained"
                ) from recovery_error
            raise
        # Commit success is not enough: verify DB path/file agreement and only
        # then remove the durable encrypted journal. A crash before this call is
        # recovered forward on the next invocation.
        _recover_attachment_move_journal(conn, db_path)
        _shred_residue(conn, report)
    finally:
        conn.close()
    log.info("encryption migrate (dry_run=%s): %s", dry_run, report)
    return report


def _shred_residue(conn: sqlite3.Connection, report: dict[str, int]) -> None:
    """Make the pre-encryption plaintext unrecoverable from the DB file.

    ``secure_delete=ON`` already zeroed the freed cells as rows were re-sealed in
    place; this additionally flushes + truncates the WAL sidecar (which can still
    hold pre-migration plaintext frames) and VACUUMs to rebuild the file with no
    residual free pages. Best-effort: if the DB is locked (e.g. the agent is
    running) the rebuild is skipped with a warning -- the in-place zeroing stands.
    """
    if not any(report.values()):
        return
    conn.isolation_level = None   # autocommit: VACUUM / checkpoint need no open txn
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as e:
        log.warning(
            "encryption migrate: could not VACUUM/checkpoint to shred residue "
            "(%s); freed pages were still zeroed in place via secure_delete", e,
        )


__all__ = ["migrate_world_db"]
