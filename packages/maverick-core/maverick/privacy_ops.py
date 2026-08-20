"""Privacy operations record types: the "nimble OneTrust" data plane.

Four privacy record types over one tenant-scoped JSON store, each pairing a
deterministic, explainable engine with a lifecycle the Privacy workspace can
review — the same design as :mod:`maverick.assessment` (records are governance
artifacts meant to be exported and audited, not secrets):

* **DPA reviews** — a Data Processing Agreement checked clause-by-clause
  against the GDPR Art. 28(3) requirements (documented instructions,
  confidentiality, Art. 32 security, sub-processor authorization, data-subject
  assistance, breach notification, deletion/return, audit rights) plus
  transfer-safeguard and retention checks. Detection is keyword-heuristic and
  honest: ``present`` / ``unclear`` / ``missing`` with the matched excerpt, so
  a reviewer can verify every call. Residual risk falls as required clauses
  are found; inherent risk reflects the exposure the document itself declares
  (transfers, special categories, broad scopes).
* **AI systems registry** — an inventory of AI systems classified against the
  EU AI Act's tiers (prohibited / high / limited / minimal) using the Act's
  own category tests (Annex III high-risk areas, Art. 5 prohibited practices,
  Art. 50 transparency triggers), each classification carrying its matched
  signals and the tier's obligations. Screening aid, not legal advice.
* **RoPA / data inventory** — Art. 30(1) records of processing activities.
  ``draft_from_assessment`` turns a completed PIA into a draft RoPA entry
  (answers map onto Art. 30 fields), which is the system-of-record moment:
  assessments feed the inventory instead of dying in a filing cabinet.
* **DSAR requests** — a tracker with statutory due dates (30 days) over the
  REAL fulfillment machinery: access/portability exports run
  :func:`maverick.dsar.export_subject_data`; erasure records a structured
  argument vector for an authenticated operator workflow (destructive
  fulfillment stays deliberate and no shell command is rendered).

Gated by ``[privacy_ops] enable`` (default on — local record stores, already
behind the workspace's operate floor). Everything fails soft and nothing here
auto-approves, auto-erases, or bypasses the human gates.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_PRIVACY_JSON_BYTES = 16 * 1024 * 1024
_MAX_DOCX_ENTRIES = 1024
_MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_DOCX_DOCUMENT_XML_BYTES = 8 * 1024 * 1024
_MAX_DOCX_COMPRESSION_RATIO = 200
_MAX_DOCUMENT_INPUT_BYTES = 16 * 1024 * 1024
_MAX_PDF_INPUT_BYTES = _MAX_DOCUMENT_INPUT_BYTES
_MAX_PDF_OBJECTS = 4096
_MAX_PDF_STREAMS = 256
_MAX_PDF_COMPRESSED_STREAM_BYTES = 4 * 1024 * 1024
_MAX_PDF_EXPANDED_STREAM_BYTES = 8 * 1024 * 1024
_MAX_PDF_AGGREGATE_STREAM_BYTES = 16 * 1024 * 1024
_MAX_PDF_DECODED_TEXT_BYTES = 2 * 1024 * 1024
_MAX_PDF_TEXT_OPERATORS = 100_000
_MAX_PDF_CONTENT_TOKENS = 250_000
_MAX_AUDIT_PENDING_RECEIPTS = 128
_MAX_AUDIT_DELIVERIES_PER_FLUSH = 16
_RECORD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def _unsafe_path_alias(path: Path, info: os.stat_result) -> bool:
    """POSIX symlink or Windows junction/reparse point."""
    is_junction = getattr(path, "is_junction", None)
    return (
        stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)
        or bool(callable(is_junction) and is_junction())
    )


def _component_identity(info: os.stat_result) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        getattr(info, "st_file_attributes", None),
        getattr(info, "st_reparse_tag", None),
    )


def _file_identity(info: os.stat_result) -> tuple:
    """Fields stable across path-stat and descriptor-stat on Windows/POSIX."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        getattr(info, "st_mtime_ns", None),
    )


def _validated_private_directory(
    directory: Path,
    *,
    must_exist: bool,
) -> tuple[Path, tuple[tuple, ...]]:
    """Validate every lexical component without following path aliases."""
    from .file_lock import private_path_is_restricted

    absolute = Path(os.path.abspath(os.fspath(directory)))
    parts = absolute.parts
    cursor = Path(parts[0])
    identities: list[tuple] = []
    found_final = False
    for index, part in enumerate(parts):
        if index:
            cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise PermissionError(
                f"privacy store path could not be verified: {cursor}"
            ) from exc
        if _unsafe_path_alias(cursor, info):
            raise PermissionError(
                f"privacy store path aliases are not permitted: {cursor}"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise PermissionError(
                f"privacy store component is not a directory: {cursor}"
            )
        identities.append(_component_identity(info))
        found_final = index == len(parts) - 1
    if must_exist and not found_final:
        raise FileNotFoundError(absolute)
    if found_final and not private_path_is_restricted(absolute, 0o700):
        raise PermissionError(
            f"privacy store directory is not private: {absolute}"
        )
    return absolute, tuple(identities)


def _read_private_json(path: Path, *, root: Path) -> dict:
    """No-follow, handle-bound JSON read from a verified private directory."""
    from .file_lock import private_path_is_restricted

    root, components_before = _validated_private_directory(root, must_exist=True)
    lexical = Path(os.path.abspath(os.fspath(path)))
    if lexical.parent != root:
        raise PermissionError("privacy state path escaped its store")
    before = lexical.lstat()
    if (
        _unsafe_path_alias(lexical, before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise PermissionError("privacy state is not a single-link regular file")
    if before.st_size > _MAX_PRIVACY_JSON_BYTES:
        raise ValueError("privacy state exceeds the size limit")
    if not private_path_is_restricted(lexical, 0o600):
        raise PermissionError("privacy state file is not private")

    root_fd: int | None = None
    fd: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            if os.name != "nt":
                root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                root_flags |= getattr(os, "O_NOFOLLOW", 0)
                root_fd = os.open(root, root_flags)
                if _component_identity(os.fstat(root_fd)) != components_before[-1]:
                    raise PermissionError("privacy store root changed during open")
                fd = os.open(lexical.name, flags, dir_fd=root_fd)
            else:
                # Windows CRT descriptors have no openat. The verified component
                # identities are rechecked before returning any bytes; final
                # reparse points are rejected from both path and handle metadata.
                fd = os.open(lexical, flags)
        except OSError as exc:
            # A record that existed at the lstat boundary but cannot be opened
            # through the no-follow handle boundary changed underneath us.
            # POSIX reports a leaf swap to a symlink as ELOOP (a plain
            # OSError), while Windows generally exposes the later identity
            # mismatch as PermissionError. Normalize both hosts to the same
            # fail-closed API instead of misclassifying the race as corrupt
            # JSON in _RecordStore._read_path.
            raise PermissionError("privacy state changed during open") from exc
        opened = os.fstat(fd)
        if (
            _unsafe_path_alias(lexical, opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _file_identity(opened) != _file_identity(before)
        ):
            raise PermissionError("privacy state changed during open")
        chunks: list[bytes] = []
        remaining = _MAX_PRIVACY_JSON_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_PRIVACY_JSON_BYTES:
            raise ValueError("privacy state exceeds the size limit")
        after = os.fstat(fd)
        current = lexical.lstat()
        _, components_after = _validated_private_directory(
            root,
            must_exist=True,
        )
        if (
            _file_identity(after) != _file_identity(opened)
            or _file_identity(current) != _file_identity(before)
            or components_after != components_before
            or not private_path_is_restricted(lexical, 0o600)
        ):
            raise PermissionError("privacy state changed during read")
    finally:
        if fd is not None:
            os.close(fd)
        if root_fd is not None:
            os.close(root_fd)
    value = json.loads(raw.decode("utf-8", errors="strict"))
    if not isinstance(value, dict):
        raise ValueError("privacy state is not a JSON object")
    return value

# --------------------------------------------------------------------------- #
# Config gate + shared store
# --------------------------------------------------------------------------- #

def enabled() -> bool:
    try:
        from .config import load_config
        return bool((load_config().get("privacy_ops") or {})
                    .get("enable", True))
    except Exception:  # pragma: no cover -- config never crashes a read
        return True


class RecordConflict(RuntimeError):
    """A caller tried to replace a stale privacy record revision."""


class AuditOutboxError(RuntimeError):
    """A privacy mutation cannot preserve a valid bounded audit outbox."""


class PrivacyStateError(RuntimeError):
    """A present governed privacy record cannot be trusted or recovered."""


def _validated_audit_queue(record: dict) -> list[dict]:
    """Return a shape- and size-validated copy of an embedded outbox."""
    raw = record.get("_audit_pending")
    if raw is None:
        return []
    if isinstance(raw, dict):
        queue = [raw]
    elif isinstance(raw, list):
        queue = list(raw)
    else:
        raise AuditOutboxError("privacy audit outbox has invalid shape")
    if len(queue) > _MAX_AUDIT_PENDING_RECEIPTS:
        raise AuditOutboxError("privacy audit outbox exceeds receipt cap")
    event_ids: set[str] = set()
    for receipt in queue:
        if not isinstance(receipt, dict):
            raise AuditOutboxError("privacy audit outbox receipt is not an object")
        event_id = receipt.get("event_id")
        action = receipt.get("action")
        actor = receipt.get("actor")
        prepared_at = receipt.get("prepared_at")
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
            raise AuditOutboxError("privacy audit receipt has invalid event id")
        if event_id in event_ids:
            raise AuditOutboxError("privacy audit outbox has duplicate event id")
        event_ids.add(event_id)
        if not isinstance(action, str) or not 1 <= len(action) <= 64:
            raise AuditOutboxError("privacy audit receipt has invalid action")
        if not isinstance(actor, str) or not 1 <= len(actor) <= 256:
            raise AuditOutboxError("privacy audit receipt has invalid actor")
        if isinstance(prepared_at, bool):
            raise AuditOutboxError("privacy audit receipt has invalid timestamp")
        try:
            timestamp = float(prepared_at)
        except (TypeError, ValueError) as exc:
            raise AuditOutboxError(
                "privacy audit receipt has invalid timestamp"
            ) from exc
        if not math.isfinite(timestamp) or timestamp <= 0:
            raise AuditOutboxError("privacy audit receipt has invalid timestamp")
        digest = receipt.get("record_sha256")
        if digest is not None and (
            not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise AuditOutboxError("privacy audit receipt has invalid record digest")
        revision = receipt.get("revision")
        if revision is not None and (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise AuditOutboxError("privacy audit receipt has invalid revision")
        status = receipt.get("status")
        if status is not None and (
            not isinstance(status, str) or len(status) > 64
        ):
            raise AuditOutboxError("privacy audit receipt has invalid status")
    return queue


def _validate_document_evidence_record(record: dict) -> None:
    """Verify bounded provenance metadata before a DPA record is served."""
    evidence = record.get("document_evidence")
    if evidence is None:
        return
    if not isinstance(evidence, dict):
        raise PrivacyStateError("document evidence is invalid")
    if evidence.get("schema") != "maverick.document-extraction-evidence.v1":
        raise PrivacyStateError("document evidence schema is invalid")
    for key in (
        "source_binding_sha256",
        "document_sha256",
        "extracted_text_sha256",
        "binding_sha256",
    ):
        if not isinstance(evidence.get(key), str) or re.fullmatch(
            r"[0-9a-f]{64}", evidence[key]
        ) is None:
            raise PrivacyStateError("document evidence digest is invalid")
    bounded_strings = {
        "source": 64,
        "resolved_mime": 255,
        "method": 64,
        "scope": 64,
        "confidence": 32,
    }
    for key, limit in bounded_strings.items():
        value = evidence.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= limit:
            raise PrivacyStateError("document evidence metadata is invalid")
    size = evidence.get("document_size_bytes")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 <= size <= _MAX_DOCUMENT_INPUT_BYTES
    ):
        raise PrivacyStateError("document evidence size is invalid")
    if not isinstance(evidence.get("review_required"), bool):
        raise PrivacyStateError("document evidence review policy is invalid")
    streams = evidence.get("referenced_streams")
    if streams is not None and (
        not isinstance(streams, int)
        or isinstance(streams, bool)
        or not 0 <= streams <= _MAX_PDF_STREAMS
    ):
        raise PrivacyStateError("document evidence stream count is invalid")
    bound = dict(evidence)
    claimed = bound.pop("binding_sha256")
    if _canonical_sha256(bound) != claimed:
        raise PrivacyStateError("document evidence binding does not verify")
    if (
        record.get("extraction_confidence") != evidence["confidence"]
        or record.get("review_required") is not evidence["review_required"]
        or (
            evidence["confidence"] == "untrusted"
            and evidence["review_required"] is not True
        )
    ):
        raise PrivacyStateError("document evidence policy binding is invalid")


class _RecordStore:
    """Tenant-scoped, atomic JSON records with per-record strict locking.

    Every mutation is a locked read-modify-atomic-replace and increments the
    monotonic ``revision``. That prevents torn reads and lost updates across the
    dashboard, workers, and multiple server processes; ``expected_revision``
    adds compare-and-swap for callers that reviewed a specific version.
    """

    def __init__(self, name: str, prefix: str) -> None:
        self.name = name
        self.prefix = prefix

    def _dir(self) -> Path:
        from .paths import data_dir
        return data_dir(self.name)

    def new_id(self) -> str:
        return f"{self.prefix}-{uuid.uuid4().hex[:10]}"

    def _path(self, record_id: str) -> Path | None:
        record_id = str(record_id or "")
        if (
            not _RECORD_ID_RE.fullmatch(record_id)
            or not record_id.startswith(f"{self.prefix}-")
        ):
            return None
        base, _ = _validated_private_directory(
            self._dir(),
            must_exist=False,
        )
        return base / f"{record_id}.json"

    def _read_path(self, path: Path) -> dict | None:
        try:
            value = _read_private_json(path, root=path.parent)
            expected_id = path.stem
            if (
                value.get("id") != expected_id
                or not _RECORD_ID_RE.fullmatch(expected_id)
                or not expected_id.startswith(f"{self.prefix}-")
            ):
                raise PermissionError(
                    "privacy record identity does not match its store path"
                )
            try:
                _validated_audit_queue(value)
            except AuditOutboxError as exc:
                raise PermissionError(
                    "privacy record has an invalid audit outbox"
                ) from exc
            self._revision(value)
            _validate_document_evidence_record(value)
            return value
        except PermissionError:
            raise
        except FileNotFoundError:
            return None
        except PrivacyStateError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise PrivacyStateError(
                "privacy record is unreadable or corrupt") from exc

    @staticmethod
    def _revision(record: dict | None) -> int:
        if record is None or "revision" not in record:
            return 0
        revision = record.get("revision")
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise PermissionError("privacy record revision is invalid")
        return revision

    @staticmethod
    def _expected_revision(value: int | None) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("expected privacy record revision is invalid")
        return value

    @classmethod
    def _write_path(
        cls,
        path: Path,
        record: dict,
        previous: dict | None,
        *,
        bump_revision: bool = True,
    ) -> dict:
        from .file_lock import atomic_write_text

        saved = dict(record)
        prior_revision = cls._revision(previous)
        saved["revision"] = prior_revision + (1 if bump_revision else 0)
        logical_record = {
            key: value for key, value in saved.items()
            if key != "_audit_pending"
        }
        record_sha256 = hashlib.sha256(json.dumps(
            logical_record,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")).hexdigest()
        # Bind each durable outbox receipt to the exact logical version and
        # status committed by the mutation that created it.  A later retry
        # must not accidentally describe the record's newer state.
        pending_items = _validated_audit_queue(saved)
        if pending_items:
            stamped: list[dict] = []
            for item in pending_items:
                receipt = dict(item)
                receipt.setdefault("revision", saved["revision"])
                receipt.setdefault("status", str(saved.get("status") or ""))
                receipt.setdefault("record_sha256", record_sha256)
                stamped.append(receipt)
            saved["_audit_pending"] = stamped
            _validated_audit_queue(saved)
        else:
            saved.pop("_audit_pending", None)
        try:
            rendered = json.dumps(
                saved,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise PrivacyStateError("privacy record is not valid bounded JSON") from exc
        if len(rendered.encode("utf-8")) > _MAX_PRIVACY_JSON_BYTES:
            raise PrivacyStateError("privacy record exceeds the 16 MiB JSON limit")
        atomic_write_text(
            path,
            rendered,
            mode=0o600,
        )
        return saved

    def save(self, record: dict, *, expected_revision: int | None = None) -> dict:
        """Atomically create/replace a record, optionally with CAS."""
        from .file_lock import cross_process_lock, ensure_private_directory

        path = self._path(str(record.get("id") or ""))
        if path is None:
            raise ValueError("invalid privacy record id")
        ensure_private_directory(path.parent)
        _validated_private_directory(path.parent, must_exist=True)
        with cross_process_lock(path, strict=True):
            current = self._read_path(path)
            current_revision = self._revision(current)
            expected = self._expected_revision(expected_revision)
            if expected is not None and current_revision != expected:
                raise RecordConflict(
                    f"privacy record changed (expected revision "
                    f"{expected_revision}, found {current_revision})"
                )
            return self._write_path(path, record, current)

    def load(self, record_id: str) -> dict | None:
        from .file_lock import cross_process_lock

        path = self._path(record_id)
        if path is None:
            return None
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if _unsafe_path_alias(path, info):
            raise PermissionError("privacy record path alias is not permitted")
        with cross_process_lock(path, strict=True):
            return self._read_path(path)

    def list(self) -> list[dict]:
        from .file_lock import cross_process_lock

        d, _ = _validated_private_directory(self._dir(), must_exist=False)
        if not d.exists():
            return []
        out = []
        for p in d.glob("*.json"):
            info = p.lstat()
            if _unsafe_path_alias(p, info):
                raise PermissionError("privacy record path alias is not permitted")
            with cross_process_lock(p, strict=True):
                record = self._read_path(p)
            if record is not None:
                out.append(record)
        return sorted(out, key=lambda r: r.get("created_at", 0), reverse=True)

    def list_bounded(self, *, limit: int) -> list[dict]:
        """Read at most ``limit`` records after validating every store path.

        Exact collection-wide ``created_at`` ordering requires deserializing
        every legacy JSON record because the timestamp is stored only inside
        the document.  A bounded read instead ranks the fully validated paths
        by protected-file modification time, reads only that candidate window,
        and preserves the established ``created_at`` order within the window.
        Unbounded :meth:`list` retains its exact historical semantics.
        """
        from .file_lock import cross_process_lock, private_path_is_restricted

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("privacy record list limit must be non-negative")
        d, _ = _validated_private_directory(self._dir(), must_exist=False)
        if not d.exists():
            return []
        candidates: list[tuple[Path, os.stat_result]] = []
        for path in d.glob("*.json"):
            try:
                info = path.lstat()
            except OSError as exc:
                raise PermissionError(
                    f"privacy record path could not be inspected: {path}"
                ) from exc
            record_id = path.stem
            if (
                _unsafe_path_alias(path, info)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not _RECORD_ID_RE.fullmatch(record_id)
                or not record_id.startswith(f"{self.prefix}-")
                or not private_path_is_restricted(path, 0o600)
            ):
                raise PermissionError(
                    "privacy record scan found an invalid record path"
                )
            if info.st_size > _MAX_PRIVACY_JSON_BYTES:
                raise PrivacyStateError(
                    "privacy record payload exceeds the size limit"
                )
            candidates.append((path, info))
        candidates.sort(
            key=lambda item: (-item[1].st_mtime_ns, item[0].name),
        )
        out = []
        for path, _info in candidates[:limit]:
            with cross_process_lock(path, strict=True):
                record = self._read_path(path)
            if record is not None:
                out.append(record)
        return sorted(out, key=lambda r: r.get("created_at", 0), reverse=True)

    def iter_record_ids(
        self,
        *,
        start_after: str = "",
        limit: int = 100,
    ):
        """Yield a rotating, bounded window of validated record identities.

        Directory enumeration may inspect names, but this method deliberately
        performs no record reads and acquires no per-record locks.  Callers can
        therefore put a hard budget on expensive/contended reads while a cursor
        prevents a large store's lexical prefix from starving later records.
        """
        d, _ = _validated_private_directory(self._dir(), must_exist=False)
        if not d.exists():
            return
        paths = sorted(
            (path for path in d.iterdir() if path.name.endswith(".json")),
            key=lambda path: path.name,
        )
        if not paths:
            return
        cursor = str(start_after or "")
        pivot = next(
            (index for index, path in enumerate(paths) if path.name > cursor),
            len(paths),
        )
        ordered = paths[pivot:] + paths[:pivot]
        for path in ordered[:max(0, int(limit))]:
            try:
                info = path.lstat()
            except OSError as exc:
                raise PermissionError(
                    f"privacy record path could not be inspected: {path}"
                ) from exc
            record_id = path.stem
            if (
                _unsafe_path_alias(path, info)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not _RECORD_ID_RE.fullmatch(record_id)
                or not record_id.startswith(f"{self.prefix}-")
            ):
                raise PermissionError(
                    "privacy record scan found an invalid record path"
                )
            yield path.name, record_id

    def update(
        self,
        record_id: str,
        mutate,
        *,
        expected_revision: int | None = None,
        metadata_only: bool = False,
    ) -> dict | None:
        """Run ``mutate`` under the per-record lock and publish if changed.

        ``metadata_only`` is reserved for durable audit-outbox bookkeeping: it
        atomically rewrites the receipt without changing the logical record
        revision that the signed event and client CAS are bound to.
        """
        from .file_lock import cross_process_lock, ensure_private_directory

        path = self._path(record_id)
        if path is None:
            return None
        ensure_private_directory(path.parent)
        _validated_private_directory(path.parent, must_exist=True)
        with cross_process_lock(path, strict=True):
            record = self._read_path(path)
            if record is None:
                return None
            current_revision = self._revision(record)
            expected = self._expected_revision(expected_revision)
            if expected is not None and current_revision != expected:
                raise RecordConflict(
                    f"privacy record changed (expected revision "
                    f"{expected_revision}, found {current_revision})"
                )
            updated = dict(record)
            mutate(updated)
            if updated == record:
                return record
            return self._write_path(
                path,
                updated,
                record,
                bump_revision=not metadata_only,
            )


_DPA = _RecordStore("dpa_reviews", "DPA")
_AI = _RecordStore("ai_registry", "AI")
_ROPA = _RecordStore("ropa", "ROPA")
_DSAR = _RecordStore("dsar_requests", "DSAR")
_INCIDENT = _RecordStore("privacy_incidents", "INC")
_PAPER = _RecordStore("paper_reviews", "PAPR")


def _actor_label(actor: str = "") -> str:
    """Bounded, collision-resistant durable label for an exact identity."""
    identity = str(actor or "")
    if len(identity) <= 256:
        return identity
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{identity[:183]}#sha256:{digest}"


def _pending_audit(action: str, actor: str = "") -> dict:
    """A durable, non-PII outbox receipt embedded in the mutated record."""
    return {
        "event_id": uuid.uuid4().hex,
        "action": str(action)[:64],
        "actor": _actor_label(actor) or "system",
        "prepared_at": time.time(),
    }


def _queue_audit(record: dict, action: str, actor: str = "") -> None:
    """Append an event to a record's durable outbox without losing a racer."""
    queue = _validated_audit_queue(record)
    if len(queue) >= _MAX_AUDIT_PENDING_RECEIPTS:
        # Never discard an older receipt to make a new mutation appear
        # auditable. The surrounding locked update aborts without publishing
        # any of the caller's changes.
        raise AuditOutboxError("privacy audit outbox is full")
    queue.append(_pending_audit(action, actor))
    record["_audit_pending"] = queue


def _audit_mutation(
    store: _RecordStore,
    record_type: str,
    record: dict,
    *,
    _status: dict | None = None,
) -> dict:
    """Flush an embedded audit outbox receipt, retaining it on failure.

    The mutation and ``_audit_pending`` are published in the same atomic record
    write. The signed audit append then runs while holding that record's strict
    cross-process lock. Only a successful ``audit_record()`` removes the receipt;
    false/exception leaves a durable retryable marker. Delivery is intentionally
    **at least once**: a crash after append but before receipt removal can append
    the same stable ``event_id`` again, allowing consumers to deduplicate without
    ever losing the mutation's audit event.
    """
    record_id = str(record.get("id") or "")
    outcome: dict | None = None

    def _flush(current: dict) -> None:
        nonlocal outcome
        queue = _validated_audit_queue(current)
        if _status is not None:
            _status["had_pending"] = bool(queue)
        if not queue:
            outcome = current
            return
        remaining = list(queue)
        for pending in queue[:_MAX_AUDIT_DELIVERIES_PER_FLUSH]:
            try:
                from .audit import EventKind
                from .audit import record as audit_record
                from .paths import current_tenant_id

                ok = audit_record(
                    EventKind.PRIVACY_RECORD_CHANGED,
                    agent=str(pending.get("actor") or "system"),
                    event_id=str(pending.get("event_id") or ""),
                    occurred_at=float(pending.get("prepared_at") or 0.0),
                    actor=str(pending.get("actor") or "system"),
                    tenant=current_tenant_id() or "",
                    record_type=record_type,
                    action=str(pending.get("action") or ""),
                    record_id=record_id,
                    revision=int(
                        pending.get("revision")
                        or _RecordStore._revision(current)
                    ),
                    status=str(
                        pending.get("status")
                        or current.get("status")
                        or ""
                    ),
                    record_sha256=str(pending.get("record_sha256") or ""),
                )
                if not ok:
                    raise RuntimeError("audit writer refused privacy event")
            except Exception as exc:
                log.error(
                    "privacy mutation audit pending for %s/%s (%s)",
                    record_type,
                    pending.get("action"),
                    type(exc).__name__,
                )
                break
            remaining.pop(0)
        if remaining:
            current["_audit_pending"] = remaining
        else:
            current.pop("_audit_pending", None)
        outcome = current

    saved = store.update(record_id, _flush, metadata_only=True)
    if saved is None:
        raise PrivacyStateError("privacy record disappeared during audit commit")
    return saved


_AUDIT_RETRY_STATE_LOCK = threading.Lock()
_AUDIT_RETRY_CURSORS: dict[str, str] = {}
_AUDIT_RETRY_STORE_OFFSET = 0


def retry_pending_audits(*, limit: int = 100) -> int:
    """Retry a rotating bounded window of durable audit outbox receipts.

    ``limit`` budgets record reads and per-record lock acquisitions, not merely
    successful audit appends.  Persistent cursors rotate within each store and
    the starting store rotates between calls, preventing a large or unavailable
    first store from monopolizing every retry pass.
    """
    global _AUDIT_RETRY_STORE_OFFSET

    cleared = 0
    visited = 0
    visit_limit = max(1, int(limit))
    stores = (
        ("dpa_review", _DPA),
        ("ai_system", _AI),
        ("ropa", _ROPA),
        ("dsar", _DSAR),
        ("incident", _INCIDENT),
        ("paper_review", _PAPER),
    )
    with _AUDIT_RETRY_STATE_LOCK:
        start = _AUDIT_RETRY_STORE_OFFSET % len(stores)
        order = tuple(range(start, len(stores))) + tuple(range(start))
        iterators = {
            index: iter(stores[index][1].iter_record_ids(
                start_after=_AUDIT_RETRY_CURSORS.get(stores[index][0], ""),
                limit=visit_limit,
            ))
            for index in order
        }
        active = set(order)
        while active and visited < visit_limit:
            progressed = False
            for index in order:
                if index not in active or visited >= visit_limit:
                    continue
                try:
                    filename, record_id = next(iterators[index])
                except StopIteration:
                    active.remove(index)
                    continue
                progressed = True
                record_type, store = stores[index]
                _AUDIT_RETRY_CURSORS[record_type] = filename
                visited += 1
                status: dict[str, bool] = {}
                try:
                    result = _audit_mutation(
                        store,
                        record_type,
                        {"id": record_id},
                        _status=status,
                    )
                except (
                    AuditOutboxError,
                    PermissionError,
                    PrivacyStateError,
                    ValueError,
                ) as exc:
                    # A corrupt record cannot consume an unbounded scan or
                    # prevent rotation to other stores. Keep it untouched for
                    # operator repair and continue within the visit budget.
                    log.error(
                        "invalid privacy audit outbox for %s/%s (%s)",
                        record_type,
                        record_id,
                        type(exc).__name__,
                    )
                    _AUDIT_RETRY_STORE_OFFSET = (index + 1) % len(stores)
                    continue
                if status.get("had_pending") and not isinstance(
                    result.get("_audit_pending"),
                    (dict, list),
                ):
                    cleared += 1
                # Resume at the next store even when this pass exhausts its
                # budget in the middle of a round-robin cycle.
                _AUDIT_RETRY_STORE_OFFSET = (index + 1) % len(stores)
            if not progressed:
                break
    return cleared

# --------------------------------------------------------------------------- #
# DPA review -- Art. 28(3) clause checklist
# --------------------------------------------------------------------------- #

# (key, requirement, citation, severity-if-missing, detection patterns)
DPA_CHECKLIST: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("instructions", "Processing only on documented controller instructions",
     "Art. 28(3)(a)", "high",
     (r"documented instruction", r"written instruction",
      r"only on (?:the )?instruction", r"instructions (?:of|from) the controller")),
    ("confidentiality", "Personnel bound to confidentiality",
     "Art. 28(3)(b)", "medium",
     (r"confidentialit", r"duty of confidence", r"non-disclosure")),
    ("security", "Art. 32 technical and organisational security measures",
     "Art. 28(3)(c) / Art. 32", "high",
     (r"technical and organi[sz]ational", r"article 32", r"art\.? ?32",
      r"encrypt", r"security measures")),
    ("subprocessors", "Sub-processor authorization and flow-down",
     "Art. 28(2), 28(4)", "high",
     (r"sub-?processor", r"subcontract")),
    ("assistance", "Assistance with data-subject rights requests",
     "Art. 28(3)(e)", "medium",
     (r"data subject request", r"assist the controller",
      r"rights of (?:the )?data subject", r"dsar")),
    ("breach", "Personal-data breach notification to the controller",
     "Art. 28(3)(f) / Art. 33", "high",
     (r"breach", r"security incident", r"notif")),
    ("deletion", "Deletion or return of data at end of services",
     "Art. 28(3)(g)", "medium",
     (r"delet(?:e|ion) or return", r"return or delet", r"end of the provision",
      r"termination.{0,80}(?:delete|return|destro)")),
    ("audit", "Audit and inspection rights for the controller",
     "Art. 28(3)(h)", "medium",
     (r"audit", r"inspection")),
    ("transfers", "Safeguards for international transfers",
     "Chapter V (Art. 44-49)", "high",
     (r"standard contractual clauses", r"\bscc", r"adequacy decision",
      r"chapter v", r"transfer mechanism", r"data privacy framework")),
    ("retention", "Defined retention / storage limitation",
     "Art. 5(1)(e)", "medium",
     (r"retention", r"retain(?:ed|s)? for", r"storage period")),
)

# Exposure signals: what the document itself says the processing involves.
_DPA_EXPOSURE = (
    ("international transfers", "high",
     (r"transfer", r"third countr", r"outside the (?:eu|eea|european)")),
    ("special-category data", "high",
     (r"special categor", r"health data", r"biometric", r"sensitive data")),
    ("large-scale / monitoring", "medium",
     (r"large.?scale", r"monitor", r"profil")),
    ("personal data processing", "medium",
     (r"personal data",)),
)


def _find(text_lower: str, patterns: tuple[str, ...],
          with_prefix: bool = False):
    """First match's surrounding excerpt (plus, optionally, the text right
    before the match — for adjacent-negation checks). None when no pattern
    hits."""
    for pat in patterns:
        m = re.search(pat, text_lower)
        if m:
            start = max(0, m.start() - 60)
            excerpt = text_lower[start:m.end() + 90].strip()
            if with_prefix:
                prefix = text_lower[max(0, m.start() - 30):m.start()]
                return excerpt, prefix
            return excerpt
    return (None, "") if with_prefix else None


def review_dpa(vendor: str, text: str, *, document_name: str = "",
               reviewed_by: str = "",
               _document_evidence: dict | None = None) -> dict:
    """Review DPA ``text`` against the Art. 28 checklist. Deterministic and
    explainable: every verdict carries the matched excerpt (or its absence).
    Returns the saved record."""
    body = (text or "")[:2_000_000]
    low = body.lower()
    clauses = []
    residual_sev: list[str] = []
    for key, requirement, citation, severity, patterns in DPA_CHECKLIST:
        excerpt, prefix = _find(low, patterns, with_prefix=True)
        if excerpt is None:
            status = "missing"
            residual_sev.append(severity)
        elif re.search(r"\b(?:not|no|without|never|excluded?)\b[^.;]{0,20}$",
                       prefix):
            # Negating language IMMEDIATELY before the clause ("shall not
            # delete...", "no audit rights") -- flag for a human call rather
            # than crediting or failing it. Negation elsewhere in the window
            # (e.g. "notify without undue delay") is normal DPA phrasing.
            status = "unclear"
            residual_sev.append("low")
        else:
            status = "present"
        clauses.append({
            "key": key, "requirement": requirement, "citation": citation,
            "severity": severity, "status": status,
            "excerpt": (excerpt or "")[:240],
        })
    inherent_sev = []
    exposures = []
    for label, severity, patterns in _DPA_EXPOSURE:
        if _find(low, patterns):
            inherent_sev.append(severity)
            exposures.append(label)
    from .assessment import _rollup
    present = sum(1 for c in clauses if c["status"] == "present")
    record = {
        "id": _DPA.new_id(),
        "vendor": (vendor or "").strip()[:200],
        "document_name": (document_name or "").strip()[:300],
        "created_at": time.time(),
        "reviewed_by": _actor_label(reviewed_by),
        "status": "pending_review",
        "clauses": clauses,
        "exposures": exposures,
        "clauses_present": present,
        "clauses_total": len(clauses),
        "inherent_risk": _rollup(inherent_sev),
        "residual_risk": _rollup(residual_sev),
        "chars_reviewed": len(body),
    }
    if _document_evidence:
        evidence = dict(_document_evidence)
        confidence = str(evidence.get("confidence") or "unknown")
        # A best-effort extractor can support a human review, never silently
        # turn heuristic findings into an authoritative approval.  In
        # particular, the intentionally incomplete PDF parser is always
        # untrusted and review-gated even if a caller supplies bad metadata.
        review_required = bool(evidence.get("review_required", True))
        if confidence == "untrusted":
            review_required = True
        evidence["review_required"] = review_required
        evidence_without_binding = {
            key: value for key, value in evidence.items()
            if key != "binding_sha256"
        }
        evidence["binding_sha256"] = _canonical_sha256(
            evidence_without_binding)
        record["document_evidence"] = evidence
        record["extraction_confidence"] = confidence
        record["review_required"] = review_required
        _validate_document_evidence_record(record)
    _queue_audit(record, "create", reviewed_by)
    saved = _DPA.save(record, expected_revision=0)
    return _audit_mutation(_DPA, "dpa_review", saved)


_PDF_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
                b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
_PDF_OBJECT_HEADER = re.compile(
    rb"(?m)^[ \t]*(\d{1,10})[ \t]+(\d{1,10})[ \t]+obj[ \t]*"
    rb"(?:\r\n|\n|\r)"
)
_PDF_END_OBJECT = re.compile(rb"(?m)^[ \t]*endobj\b")
_PDF_STREAM_START = re.compile(
    rb"(?m)^[ \t]*stream[ \t]*(?:\r\n|\n|\r)"
)
_PDF_STREAM_TAIL = re.compile(
    rb"(?:\r\n|\n|\r)[ \t]*endstream[ \t]*(?:\r\n|\n|\r)"
    rb"[ \t]*endobj\b"
)
_PDF_DIRECT_LENGTH = re.compile(rb"/Length\s+(\d{1,10})\b")
_PDF_TRAILER_TOKEN = re.compile(
    rb"(?m)^[ \t]*trailer[ \t]*(?:\r\n|\n|\r)"
)
_PDF_TRAILER_DICTIONARY = re.compile(
    rb"[ \t\r\n]*(<<(?:(?!<<|>>).)*>>)",
    re.DOTALL,
)
_PDF_TRAILER_TAIL = re.compile(
    rb"[ \t\r\n]*startxref[ \t]*(?:\r\n|\n|\r)[ \t]*\d+"
    rb"[ \t]*(?:\r\n|\n|\r)[ \t]*%%EOF[ \t\r\n]*\Z"
)
_PDF_TRAILER_ROOT = re.compile(rb"/Root\s+(\d+\s+\d+\s+R)\b")
_PDF_DIRECT_FILTER = re.compile(rb"/Filter\s*/(FlateDecode|Fl)\b")
_PDF_NUMBER = re.compile(rb"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z")
_PDF_CATALOG = re.compile(rb"/Type\s*/Catalog\b")
_PDF_PAGES_NODE = re.compile(rb"/Type\s*/Pages\b")
_PDF_PAGE = re.compile(rb"/Type\s*/Page(?!s)\b")
_PDF_ROOT_PAGES = re.compile(rb"/Pages\s+(\d+\s+\d+\s+R)\b")
_PDF_KIDS = re.compile(rb"/Kids\s*\[((?:[^\]]|\\.)*?)\]", re.DOTALL)
_PDF_CONTENTS = re.compile(
    rb"/Contents\s*(?:\[((?:[^\]]|\\.)*?)\]|(\d+\s+\d+\s+R))",
    re.DOTALL,
)
_PDF_REFERENCE = re.compile(rb"(\d{1,10})\s+(\d{1,10})\s+R\b")


class _PdfExtractionRejected(ValueError):
    """Malformed or resource-exhausting best-effort PDF extraction."""


def _pdf_literal_bytes(lit: bytes, *, max_output: int | None = None) -> bytes:
    """Decode one PDF literal string ``(...)`` including escapes/octals."""
    out = bytearray()
    i, end = 1, len(lit) - 1
    while i < end:
        if max_output is not None and len(out) >= max_output:
            raise _PdfExtractionRejected("PDF literal exceeds decoded text limit")
        c = lit[i:i + 1]
        if c == b"\\" and i + 1 < end:
            nxt = lit[i + 1:i + 2]
            if nxt.isdigit():  # octal \ddd (1-3 digits)
                j = i + 1
                while j < min(i + 4, end) and lit[j:j + 1].isdigit():
                    j += 1
                try:
                    out.append(int(lit[i + 1:j], 8) & 0xFF)
                except ValueError:  # pragma: no cover
                    pass
                i = j
                continue
            out += _PDF_ESCAPES.get(nxt, nxt)
            i += 2
            continue
        out += c
        i += 1
    return bytes(out)


def _pdf_hex_bytes(value: bytes, *, max_output: int) -> bytes:
    digits = re.sub(rb"\s", b"", value)
    if len(digits) % 2:
        digits += b"0"
    if len(digits) // 2 > max_output:
        raise _PdfExtractionRejected("PDF decoded text exceeds limit")
    try:
        return bytes.fromhex(digits.decode())
    except ValueError:  # pragma: no cover - regex admits only hex
        return b""


def _pdf_literal_token(data: bytes, start: int) -> tuple[bytes, int]:
    """Read one non-nested PDF literal token, preserving its delimiters."""
    index = start + 1
    while index < len(data):
        current = data[index]
        if current == 0x5C:  # backslash escape
            if index + 1 >= len(data):
                raise _PdfExtractionRejected("unterminated PDF literal escape")
            index += 2
            continue
        if current == 0x28:
            # Nested literals are valid PDF, but accepting them requires a
            # complete lexer to avoid recognizing operator-looking inner data.
            raise _PdfExtractionRejected("nested PDF literals are unsupported")
        if current == 0x29:
            return data[start:index + 1], index + 1
        index += 1
    raise _PdfExtractionRejected("unterminated PDF literal")


def _pdf_hex_token(data: bytes, start: int) -> tuple[bytes, int]:
    if start + 1 < len(data) and data[start + 1] == 0x3C:
        raise _PdfExtractionRejected("PDF content dictionaries are unsupported")
    end = data.find(b">", start + 1)
    if end < 0:
        raise _PdfExtractionRejected("unterminated PDF hex string")
    value = data[start + 1:end]
    if re.fullmatch(rb"[0-9A-Fa-f\s]*", value) is None:
        raise _PdfExtractionRejected("invalid PDF hex string")
    return value, end + 1


def _pdf_content_tokens(data: bytes) -> list[tuple[str, bytes]]:
    """Lex a bounded, conservative subset of a PDF content stream."""
    tokens: list[tuple[str, bytes]] = []
    whitespace = b"\x00\x09\x0a\x0c\x0d\x20"
    delimiters = b"()<>[]{}/%"
    index = 0

    def append(kind: str, value: bytes = b"") -> None:
        if len(tokens) >= _MAX_PDF_CONTENT_TOKENS:
            raise _PdfExtractionRejected("PDF content token count exceeds limit")
        tokens.append((kind, value))

    while index < len(data):
        current = data[index]
        if current in whitespace:
            index += 1
            continue
        if current == 0x25:  # comments are deliberately unsupported
            raise _PdfExtractionRejected("PDF content comments are unsupported")
        if current == 0x28:
            value, index = _pdf_literal_token(data, index)
            append("literal", value)
            continue
        if current == 0x3C:
            value, index = _pdf_hex_token(data, index)
            append("hex", value)
            continue
        if current == 0x5B:
            append("array_start")
            index += 1
            continue
        if current == 0x5D:
            append("array_end")
            index += 1
            continue
        if current in b")>{}":
            raise _PdfExtractionRejected("unbalanced PDF content delimiter")

        start = index
        if current == 0x2F:  # name token
            index += 1
        while (
            index < len(data)
            and data[index] not in whitespace
            and data[index] not in delimiters
        ):
            index += 1
        if index == start:
            raise _PdfExtractionRejected("unsupported PDF content delimiter")
        append("word", data[start:index])
    return tokens


def _decode_pdf_text_token(kind: str, value: bytes, max_output: int) -> bytes:
    if kind == "literal":
        return _pdf_literal_bytes(value, max_output=max_output)
    if kind == "hex":
        return _pdf_hex_bytes(value, max_output=max_output)
    raise _PdfExtractionRejected("invalid PDF text operand")


def _pdf_text_object_transition(
    kind: str,
    value: bytes,
    in_text_object: bool,
) -> tuple[bool, bool]:
    """Validate structural operators; return ``(new_state, consumed)``."""
    if kind != "word":
        return in_text_object, False
    if value in (b"BI", b"ID", b"EI"):
        raise _PdfExtractionRejected("PDF inline images are unsupported")
    if value == b"BT":
        if in_text_object:
            raise _PdfExtractionRejected("nested PDF text object")
        return True, True
    if value == b"ET":
        if not in_text_object:
            raise _PdfExtractionRejected("unbalanced PDF text object")
        return False, True
    if value in (b"Tj", b"TJ", b"'"):
        message = (
            "orphan PDF text operator"
            if in_text_object
            else "text operator outside PDF text object"
        )
        raise _PdfExtractionRejected(message)
    return in_text_object, False


def _pdf_tj_array_piece(
    tokens: list[tuple[str, bytes]],
    start: int,
    *,
    max_output: int,
) -> tuple[bytes | None, int]:
    """Decode one top-level TJ array or skip a non-TJ array operand."""
    end = start + 1
    while end < len(tokens) and tokens[end][0] != "array_end":
        if tokens[end][0] == "array_start":
            raise _PdfExtractionRejected("nested PDF arrays are unsupported")
        end += 1
    if end >= len(tokens):
        raise _PdfExtractionRejected("unterminated PDF content array")
    after = tokens[end + 1] if end + 1 < len(tokens) else None
    if after != ("word", b"TJ"):
        return None, end + 1

    combined = bytearray()
    for item_kind, item_value in tokens[start + 1:end]:
        if item_kind in ("literal", "hex"):
            combined.extend(_decode_pdf_text_token(
                item_kind,
                item_value,
                max(0, max_output - len(combined)),
            ))
        elif item_kind != "word" or _PDF_NUMBER.fullmatch(item_value) is None:
            raise _PdfExtractionRejected("invalid PDF TJ array")
    return bytes(combined), end + 2


def _pdf_stream_text_pieces(
    data: bytes,
    *,
    max_output: int,
    max_operators: int,
    has_prior_output: bool,
) -> tuple[list[bytes], int]:
    """Recognize text-showing operators only at top-level token boundaries."""
    tokens = _pdf_content_tokens(data)
    pieces: list[bytes] = []
    used = 0
    operators = 0
    in_text_object = False

    def output_budget() -> int:
        separator = 1 if has_prior_output or pieces else 0
        return max(0, max_output - used - separator)

    def append_piece(piece: bytes) -> None:
        nonlocal used
        if not piece:
            return
        separator = 1 if has_prior_output or pieces else 0
        if used + separator + len(piece) > max_output:
            raise _PdfExtractionRejected("PDF decoded text exceeds limit")
        pieces.append(piece)
        used += separator + len(piece)

    def count_operator() -> None:
        nonlocal operators
        operators += 1
        if operators > max_operators:
            raise _PdfExtractionRejected("PDF text operator count exceeds limit")

    index = 0
    while index < len(tokens):
        kind, value = tokens[index]
        in_text_object, consumed = _pdf_text_object_transition(
            kind,
            value,
            in_text_object,
        )
        if consumed:
            index += 1
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if (
            kind in ("literal", "hex")
            and following is not None
            and following[0] == "word"
            and following[1] in (b"Tj", b"'")
        ):
            if not in_text_object:
                raise _PdfExtractionRejected("text outside PDF text object")
            count_operator()
            append_piece(_decode_pdf_text_token(kind, value, output_budget()))
            index += 2
            continue
        if kind == "array_start":
            piece, next_index = _pdf_tj_array_piece(
                tokens,
                index,
                max_output=output_budget(),
            )
            if piece is not None:
                if not in_text_object:
                    raise _PdfExtractionRejected("text outside PDF text object")
                count_operator()
                append_piece(piece)
            index = next_index
            continue
        if kind == "array_end":
            raise _PdfExtractionRejected("unbalanced PDF content array")
        index += 1
    if in_text_object:
        raise _PdfExtractionRejected("unterminated PDF text object")
    return pieces, operators


def _bounded_pdf_stream(dictionary: bytes, raw: bytes) -> bytes:
    """Expand one content stream without allowing zlib output overflow."""
    filter_markers = len(re.findall(rb"/Filter\b", dictionary))
    if re.search(rb"/DecodeParms?\b", dictionary):
        raise _PdfExtractionRejected("unsupported PDF stream DecodeParms")
    if filter_markers == 0:
        if len(raw) > _MAX_PDF_EXPANDED_STREAM_BYTES:
            raise _PdfExtractionRejected("PDF content stream exceeds size limit")
        return raw
    filters = _PDF_DIRECT_FILTER.findall(dictionary)
    if filter_markers != 1 or len(filters) != 1:
        raise _PdfExtractionRejected("unsupported or ambiguous PDF stream filter")
    if len(raw) > _MAX_PDF_COMPRESSED_STREAM_BYTES:
        raise _PdfExtractionRejected("compressed PDF stream exceeds size limit")

    import zlib

    try:
        inflater = zlib.decompressobj()
        expanded = inflater.decompress(raw, _MAX_PDF_EXPANDED_STREAM_BYTES + 1)
    except zlib.error as exc:
        raise _PdfExtractionRejected("invalid compressed PDF stream") from exc
    if (
        len(expanded) > _MAX_PDF_EXPANDED_STREAM_BYTES
        or inflater.unconsumed_tail
        or not inflater.eof
        or inflater.unused_data.strip()
    ):
        # ``not eof`` also rejects truncated streams instead of silently
        # scoring whatever prefix happened to decompress.
        raise _PdfExtractionRejected("compressed PDF stream overflow or truncation")
    return expanded


def _strict_pdf_dictionary(value: bytes) -> bytes:
    """Accept only dictionary shells that cannot hide structural tokens."""
    dictionary = value.strip()
    if (
        not dictionary.startswith(b"<<")
        or not dictionary.endswith(b">>")
        or b"(" in dictionary
        or b")" in dictionary
        or b"%" in dictionary
    ):
        # Literal strings and comments can legally contain line-oriented
        # ``endobj`` text. Supporting them without a complete PDF lexer would
        # let data bytes masquerade as structure, so this extractor under-reads
        # those objects deliberately.
        raise _PdfExtractionRejected("unsupported or ambiguous PDF dictionary")
    return dictionary


def _pdf_object_map(
    data: bytes,
) -> tuple[dict[tuple[int, int], tuple[bytes, bytes | None]], int]:
    """Parse bounded objects using direct stream lengths."""
    objects: dict[tuple[int, int], tuple[bytes, bytes | None]] = {}
    stream_count = 0
    cursor = 0
    while header := _PDF_OBJECT_HEADER.search(data, cursor):
        if _PDF_TRAILER_TOKEN.search(data, cursor, header.start()):
            raise _PdfExtractionRejected("PDF has an incremental trailer")
        if len(objects) >= _MAX_PDF_OBJECTS:
            raise _PdfExtractionRejected("PDF object count exceeds limit")
        key = (int(header.group(1)), int(header.group(2)))
        if key in objects:
            raise _PdfExtractionRejected("duplicate PDF object identity")

        body_start = header.end()
        stream = _PDF_STREAM_START.search(data, body_start)
        end_object = _PDF_END_OBJECT.search(data, body_start)
        next_header = _PDF_OBJECT_HEADER.search(data, body_start)
        if end_object is None:
            raise _PdfExtractionRejected("PDF object has no endobj marker")
        structural_end = min(
            item.start() for item in (stream, end_object) if item is not None
        )
        if next_header is not None and next_header.start() < structural_end:
            raise _PdfExtractionRejected("nested PDF object header")

        raw: bytes | None = None
        if stream is not None and stream.start() < end_object.start():
            dictionary = _strict_pdf_dictionary(data[body_start:stream.start()])
            lengths = _PDF_DIRECT_LENGTH.findall(dictionary)
            if len(lengths) != 1:
                raise _PdfExtractionRejected(
                    "PDF streams require one direct Length value"
                )
            raw_start = stream.end()
            raw_end = raw_start + int(lengths[0])
            if raw_end > len(data):
                raise _PdfExtractionRejected("PDF stream length exceeds input")
            tail = _PDF_STREAM_TAIL.match(data, raw_end)
            if tail is None:
                raise _PdfExtractionRejected("PDF stream length is inconsistent")
            raw = data[raw_start:raw_end]
            stream_count += 1
            if stream_count > _MAX_PDF_STREAMS:
                raise _PdfExtractionRejected("PDF stream count exceeds limit")
            cursor = tail.end()
        else:
            dictionary = _strict_pdf_dictionary(data[body_start:end_object.start()])
            cursor = end_object.end()
        objects[key] = (dictionary, raw)
    return objects, cursor


def _pdf_trailer_root(data: bytes, object_end: int) -> tuple[int, int]:
    """Bind extraction to the one final trailer's direct Root reference."""
    suffix = data[object_end:]
    trailers = list(_PDF_TRAILER_TOKEN.finditer(suffix))
    if len(trailers) != 1:
        raise _PdfExtractionRejected("PDF requires one final trailer")
    trailer = trailers[0]
    dictionary_match = _PDF_TRAILER_DICTIONARY.match(suffix, trailer.end())
    if dictionary_match is None:
        raise _PdfExtractionRejected("PDF trailer dictionary is unsupported")
    dictionary = _strict_pdf_dictionary(dictionary_match.group(1))
    if _PDF_TRAILER_TAIL.fullmatch(suffix[dictionary_match.end():]) is None:
        raise _PdfExtractionRejected("PDF trailer is not final or is ambiguous")
    roots = _PDF_TRAILER_ROOT.findall(dictionary)
    if len(roots) != 1 or len(re.findall(rb"/Root\b", dictionary)) != 1:
        raise _PdfExtractionRejected("PDF trailer has ambiguous Root")
    root_match = _PDF_REFERENCE.fullmatch(roots[0])
    if root_match is None:  # pragma: no cover - constrained by root regex
        raise _PdfExtractionRejected("invalid PDF trailer Root")
    return int(root_match.group(1)), int(root_match.group(2))


def _pdf_rooted_pages(
    objects: dict[tuple[int, int], tuple[bytes, bytes | None]],
    catalog_key: tuple[int, int],
) -> list[bytes]:
    """Walk only the Page tree rooted by the trailer-selected Catalog."""
    catalog_object = objects.get(catalog_key)
    if catalog_object is None or catalog_object[1] is not None:
        raise _PdfExtractionRejected("PDF Root references invalid Catalog")
    catalog = catalog_object[0]
    if not _PDF_CATALOG.search(catalog):
        raise _PdfExtractionRejected("PDF Root is not an inspectable Catalog")
    roots = _PDF_ROOT_PAGES.findall(catalog)
    if len(roots) != 1 or len(re.findall(rb"/Pages\b", catalog)) != 1:
        raise _PdfExtractionRejected("PDF Catalog has ambiguous Pages root")
    root_match = _PDF_REFERENCE.fullmatch(roots[0])
    if root_match is None:  # pragma: no cover - constrained by root regex
        raise _PdfExtractionRejected("invalid PDF Pages root")

    queue = [(int(root_match.group(1)), int(root_match.group(2)))]
    visited: set[tuple[int, int]] = set()
    pages: list[bytes] = []
    position = 0
    while position < len(queue):
        key = queue[position]
        position += 1
        if key in visited:
            raise _PdfExtractionRejected("cyclic or duplicate PDF Pages node")
        visited.add(key)
        node = objects.get(key)
        if node is None or node[1] is not None:
            raise _PdfExtractionRejected("PDF Pages tree references invalid object")
        dictionary = node[0]
        if _PDF_PAGE.search(dictionary):
            pages.append(dictionary)
            continue
        if not _PDF_PAGES_NODE.search(dictionary):
            raise _PdfExtractionRejected("PDF Pages tree has invalid node type")
        kids = _PDF_KIDS.findall(dictionary)
        if len(kids) != 1 or len(re.findall(rb"/Kids\b", dictionary)) != 1:
            raise _PdfExtractionRejected("PDF Pages node has ambiguous Kids")
        child_refs = [
            (int(match.group(1)), int(match.group(2)))
            for match in _PDF_REFERENCE.finditer(kids[0])
        ]
        if not child_refs or len(queue) + len(child_refs) > _MAX_PDF_OBJECTS:
            raise _PdfExtractionRejected("PDF Pages Kids exceeds limit")
        queue.extend(child_refs)
    if not pages:
        raise _PdfExtractionRejected("PDF has no rooted inspectable Page objects")
    return pages


def _pdf_page_content_references(pages: list[bytes]) -> list[tuple[int, int]]:
    """Return ordered, unique direct stream refs from rooted Pages only."""
    # Never scan every stream as a fallback: hidden/unreferenced objects could
    # contain clauses that are not part of any rendered page and overstate the
    # review. This intentionally under-reads unsupported/object-stream PDFs.
    references: list[tuple[int, int]] = []
    reference_set: set[tuple[int, int]] = set()
    for page in pages:
        contents = _PDF_CONTENTS.findall(page)
        content_markers = len(re.findall(rb"/Contents\b", page))
        if content_markers == 0:
            continue
        if content_markers != 1 or len(contents) != 1:
            raise _PdfExtractionRejected("PDF Page has ambiguous Contents")
        raw_refs = contents[0][0] or contents[0][1] or b""
        for reference in _PDF_REFERENCE.finditer(raw_refs):
            key = (int(reference.group(1)), int(reference.group(2)))
            if key not in reference_set:
                reference_set.add(key)
                references.append(key)
    if not references:
        raise _PdfExtractionRejected("PDF pages have no referenced content streams")
    return references


def _pdf_objects(data: bytes) -> tuple[
    dict[tuple[int, int], tuple[bytes, bytes | None]],
    list[tuple[int, int]],
]:
    """Parse a strict PDF subset and return rooted Page content references.

    Direct ``/Length`` values let the parser skip over stream bytes without
    treating attacker-controlled ``obj``/``endobj`` tokens inside a stream as
    PDF structure. Only Pages reachable from the Catalog's Pages tree count.
    Unsupported or ambiguous constructs fail closed.
    """
    objects, object_end = _pdf_object_map(data)
    catalog_key = _pdf_trailer_root(data, object_end)
    pages = _pdf_rooted_pages(objects, catalog_key)
    return objects, _pdf_page_content_references(pages)


def _pdf_extraction(data: bytes) -> tuple[str, dict]:
    """Best-effort text from a digital PDF with the stdlib only.

    Only streams explicitly referenced by a Page ``/Contents`` entry are
    considered; unsupported PDFs fail closed rather than crediting hidden
    objects. Extraction remains untrusted because this is not a full PDF
    renderer (no encryption, object streams, font maps, XObjects, or OCR).
    """
    meta = {
        "method": "pdf_page_content_streams",
        "scope": "page_referenced",
        "confidence": "untrusted",
        "review_required": True,
    }
    if (
        not isinstance(data, bytes)
        or len(data) > _MAX_PDF_INPUT_BYTES
        or not data.startswith(b"%PDF-")
    ):
        return "", meta
    try:
        objects, references = _pdf_objects(data)
        chunks: list[str] = []
        aggregate_stream_bytes = 0
        decoded_text_bytes = 0
        operator_count = 0

        def append_piece(piece: bytes) -> None:
            nonlocal decoded_text_bytes
            separator = 1 if chunks else 0
            if (
                decoded_text_bytes + separator + len(piece)
                > _MAX_PDF_DECODED_TEXT_BYTES
            ):
                raise _PdfExtractionRejected("PDF decoded text exceeds limit")
            chunks.append(piece.decode("latin-1"))
            decoded_text_bytes += separator + len(piece)

        for reference in references:
            target = objects.get(reference)
            if target is None or target[1] is None:
                raise _PdfExtractionRejected("PDF page references a missing stream")
            dictionary, encoded = target
            raw = _bounded_pdf_stream(dictionary, encoded)
            aggregate_stream_bytes += len(raw)
            if aggregate_stream_bytes > _MAX_PDF_AGGREGATE_STREAM_BYTES:
                raise _PdfExtractionRejected("PDF aggregate stream bytes exceed limit")
            if b"Tj" not in raw and b"TJ" not in raw and b"'" not in raw:
                continue
            pieces, operators = _pdf_stream_text_pieces(
                raw,
                max_output=_MAX_PDF_DECODED_TEXT_BYTES - decoded_text_bytes,
                max_operators=_MAX_PDF_TEXT_OPERATORS - operator_count,
                has_prior_output=bool(chunks),
            )
            operator_count += operators
            for piece in pieces:
                append_piece(piece)
        text = "\n".join(chunks)
        printable = re.sub(r"[^\x20-\x7e\n\u00a0-\uffff]", "", text)
        if len(printable.strip()) < 40:
            return "", meta
        meta["referenced_streams"] = len(references)
        return printable, meta
    except _PdfExtractionRejected:
        return "", meta


def _pdf_text(data: bytes) -> str:
    """Compatibility wrapper returning only bounded untrusted PDF text."""
    return _pdf_extraction(data)[0]


def _document_text(
    data: bytes,
    mime: str,
    *,
    _metadata: dict | None = None,
) -> str:
    """Honest text extraction for the formats we can read with the stdlib:
    text/json/xml decode as UTF-8; docx is a ZIP whose word/document.xml we
    strip of tags. Anything else returns "" and the caller says so — no
    over-claiming that an unsupported or scanned document was read. PDF
    extraction is explicitly untrusted and limited to page content streams."""
    m = (mime or "").split(";")[0].strip().lower()
    metadata = {
        "method": "unsupported",
        "scope": "none",
        "confidence": "none",
        "review_required": True,
    }
    if _metadata is not None:
        _metadata.clear()
        _metadata.update(metadata)
    if not isinstance(data, bytes) or len(data) > _MAX_DOCUMENT_INPUT_BYTES:
        return ""
    if m.startswith("text/") or m in ("application/json", "application/xml"):
        metadata.update({
            "method": "utf8_decode",
            "scope": "document_bytes",
            "confidence": "source_bytes",
        })
        if _metadata is not None:
            _metadata.update(metadata)
        return data.decode("utf-8", errors="ignore")
    if m == "application/pdf":
        text, metadata = _pdf_extraction(data)
        if _metadata is not None:
            _metadata.update(metadata)
        return text
    if m == ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document"):
        metadata.update({
            "method": "docx_document_xml",
            "scope": "word/document.xml",
            "confidence": "best_effort",
        })
        if _metadata is not None:
            _metadata.update(metadata)
        import io
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                entries = z.infolist()
                if (
                    len(entries) > _MAX_DOCX_ENTRIES
                    or any(
                        info.file_size < 0 or info.compress_size < 0
                        for info in entries
                    )
                    or sum(max(0, info.file_size) for info in entries)
                    > _MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES
                ):
                    return ""
                documents = [
                    info for info in entries
                    if info.filename == "word/document.xml"
                ]
                if len(documents) != 1:
                    return ""
                document = documents[0]
                if (
                    document.is_dir()
                    or document.flag_bits & 0x1
                    or document.file_size < 0
                    or document.file_size > _MAX_DOCX_DOCUMENT_XML_BYTES
                    or document.compress_size > len(data)
                    or (
                        document.file_size > 0
                        and (
                            document.compress_size <= 0
                            or document.file_size
                            > document.compress_size * _MAX_DOCX_COMPRESSION_RATIO
                        )
                    )
                ):
                    return ""
                # Do not use ZipFile.read(): a forged/bomb entry can otherwise
                # allocate its entire decompressed body before we inspect it.
                with z.open(document, "r") as stream:
                    raw = stream.read(min(
                        _MAX_DOCX_DOCUMENT_XML_BYTES + 1,
                        document.file_size + 1,
                    ))
                if (
                    len(raw) > _MAX_DOCX_DOCUMENT_XML_BYTES
                    or len(raw) != document.file_size
                ):
                    return ""
                xml = raw.decode("utf-8", errors="ignore")
        except (
            EOFError,
            KeyError,
            NotImplementedError,
            OSError,
            OverflowError,
            RuntimeError,
            ValueError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ):
            return ""
        return re.sub(r"<[^>]+>", "", xml.replace("</w:p>", "\n"))
    return ""


def _canonical_sha256(value: object) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _build_document_evidence(
    *,
    data: bytes,
    text: str,
    mime: str,
    source: str,
    doc_id: str,
    ref: dict | None,
    extraction: dict,
) -> dict:
    """Digest-bind an extraction to bytes and a non-disclosed locator."""
    source_name = str(source or "")
    locator = {
        "source": source_name,
        "document_id": str(doc_id or ""),
        "ref": ref or {},
    }
    evidence = {
        "schema": "maverick.document-extraction-evidence.v1",
        # Connector names are operational metadata; the potentially sensitive
        # source locator and ref remain digest-only in the durable record.
        "source": source_name[:64],
        "source_binding_sha256": _canonical_sha256(locator),
        "document_sha256": hashlib.sha256(data).hexdigest(),
        "document_size_bytes": len(data),
        "extracted_text_sha256": hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest(),
        "resolved_mime": str(mime or "")[:255],
        "method": str(extraction.get("method") or "unsupported")[:64],
        "scope": str(extraction.get("scope") or "none")[:64],
        "confidence": str(extraction.get("confidence") or "none")[:32],
        "review_required": bool(extraction.get("review_required", True)),
    }
    if isinstance(extraction.get("referenced_streams"), int):
        evidence["referenced_streams"] = extraction["referenced_streams"]
    evidence["binding_sha256"] = _canonical_sha256(evidence)
    return evidence


def list_dpa_reviews() -> list[dict]:
    retry_pending_audits()
    return [{k: r.get(k) for k in
             ("id", "vendor", "document_name", "created_at", "status",
              "inherent_risk", "residual_risk", "clauses_present",
              "clauses_total", "extraction_confidence",
              "review_required")} for r in _DPA.list()]


def get_dpa_review(review_id: str) -> dict | None:
    retry_pending_audits()
    return _DPA.load(review_id)


# --------------------------------------------------------------------------- #
# Vendor-paper reviews -- their paper against our playbook, with the redline
# --------------------------------------------------------------------------- #

def _redline_dir():
    from .paths import data_dir
    return data_dir("paper_redlines")


def save_paper_review(review, *, redline: bytes = b"", memo: str = "",
                      redline_result=None, reviewed_by: str = "",
                      assessment_id: str = "",
                      redline_filename: str = "") -> dict:
    """Persist one vendor-paper review and its tracked-changes document.

    The redline is written beside the record rather than inside it: it is a
    binary Word package, and a reviewer needs to download exactly the bytes
    that were reviewed, not a re-render that might differ.

    ``version`` counts prior reviews of the same vendor+instrument, so a
    renegotiated paper reads as v1, v2, v3 rather than a pile of same-named
    files."""
    d = review.to_dict()
    vendor = (d.get("vendor") or "").strip()[:200]
    instrument = d.get("instrument") or "dpa"
    rounds = [r for r in _PAPER.list()
              if (r.get("vendor") or "").strip().lower() == vendor.lower()
              and r.get("instrument") == instrument]
    version = len(rounds) + 1
    # The negotiation round-trip: when this is a renegotiated draft (v2+),
    # diff its gaps against the PREVIOUS round's, so the memo reports which
    # of our demanded changes the counter-party accepted and which they
    # still refuse. Requirement labels come from the checklist because a
    # closed gap has no concern object in the current review.
    closed: list[str] | None = None
    prev_version = 0
    if rounds:
        from .paper_review import requirement_labels
        prev = max(rounds, key=lambda r: r.get("version", 0))
        prev_version = int(prev.get("version", 0))
        prev_gaps = {c.get("clause_key") for c in (prev.get("concerns") or [])
                     if c.get("status") != "present"}
        now_gaps = {c.get("clause_key") for c in (d.get("concerns") or [])
                    if c.get("status") != "present"}
        labels = requirement_labels(instrument)
        closed = sorted(labels.get(k, str(k))
                        for k in prev_gaps - now_gaps if k)
    if not memo:
        # Built here, not by the caller, so the version stamped on the memo is
        # the version the record actually got -- one code path, no drift.
        from .paper_review import analysis_memo
        memo = analysis_memo(
            review, version=version, reviewed_by=_actor_label(reviewed_by),
            redline_filename=redline_filename, redline_result=redline_result,
            closed_from_previous=closed, previous_version=prev_version)
    record = {
        "id": _PAPER.new_id(),
        "vendor": vendor,
        "instrument": instrument,
        "instrument_label": d.get("instrument_label", ""),
        "document_name": (d.get("document_name") or "")[:300],
        "created_at": time.time(),
        "reviewed_by": _actor_label(reviewed_by),
        "assessment_id": str(assessment_id or "")[:128],
        "version": version,
        "status": "pending_review",
        "classification": d.get("classification") or {},
        "clauses_present": d.get("clauses_present", 0),
        "clauses_total": d.get("clauses_total", 0),
        "gaps": d.get("gaps", 0),
        "high_severity_gaps": d.get("high_severity_gaps", 0),
        "recommendation": d.get("recommendation", ""),
        "drafted_with_model": bool(d.get("drafted_with_model")),
        "concerns": d.get("concerns") or [],
        "closed_from_previous": closed or [],
        "previous_version": prev_version,
        "memo": memo,
        "redline_filename": redline_filename or "",
        "redline_bytes": len(redline or b""),
    }
    _queue_audit(record, "create", reviewed_by)
    saved = _PAPER.save(record, expected_revision=0)
    if redline:
        directory = _redline_dir()
        from .file_lock import ensure_private_directory
        ensure_private_directory(directory)
        path = directory / f"{saved['id']}.docx"
        path.write_bytes(redline)
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover -- best effort on odd filesystems
            pass
    return _audit_mutation(_PAPER, "paper_review", saved)


def list_paper_reviews() -> list[dict]:
    retry_pending_audits()
    return [{k: r.get(k) for k in
             ("id", "vendor", "instrument", "instrument_label",
              "document_name", "created_at", "status", "version",
              "clauses_present", "clauses_total", "gaps",
              "high_severity_gaps", "recommendation", "drafted_with_model",
              "reviewed_by", "redline_bytes", "redline_filename")}
            for r in _PAPER.list()]


def get_paper_review(review_id: str) -> dict | None:
    retry_pending_audits()
    return _PAPER.load(review_id)


def graph_source_records() -> dict[str, list[dict]]:
    """Full privacy records for the entity graph's derivation pass.

    The graph is a derived index: it reads records, never writes them, and
    every edge it emits cites the record id it came from. Exposed as one
    accessor so the store objects stay private to this module."""
    if not enabled():
        return {}
    return {
        "dpa_reviews": _DPA.list(),
        "paper_reviews": _PAPER.list(),
        "ai_systems": _AI.list(),
        "dsars": _DSAR.list(),
        "incidents": _INCIDENT.list(),
    }


def paper_redline_bytes(review_id: str) -> bytes | None:
    """The exact tracked-changes package that was filed, or None."""
    record = _PAPER.load(review_id)
    if not record:
        return None
    path = _redline_dir() / f"{record['id']}.docx"
    try:
        return path.read_bytes()
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# AI systems registry -- EU AI Act screening
# --------------------------------------------------------------------------- #

AI_ACT_NOTE = ("Automated screening against the EU AI Act's category tests. "
               "A screening aid for triage, not legal advice.")

_PROHIBITED = (
    ("social scoring by public authorities", (r"social scor",)),
    ("subliminal / manipulative techniques", (r"subliminal", r"manipulat")),
    ("exploiting vulnerabilities", (r"exploit.{0,40}vulnerab",)),
    ("real-time remote biometric ID in public", (r"real.?time.{0,40}biometric",
                                                 r"biometric.{0,40}public")),
)
_HIGH_RISK = (
    ("employment / worker management (Annex III 4)",
     (r"hiring", r"recruit", r"employment", r"worker", r"cv screen",
      r"resume", r"candidate", r"promotion", r"termination")),
    ("credit / essential services (Annex III 5)",
     (r"credit", r"loan", r"scoring", r"insurance", r"benefit")),
    ("education (Annex III 3)", (r"exam", r"student", r"education", r"grading")),
    ("biometrics (Annex III 1)", (r"biometric", r"face recogni", r"emotion")),
    ("critical infrastructure (Annex III 2)",
     (r"critical infrastructure", r"utility", r"power grid", r"water suppl")),
    ("law enforcement / justice (Annex III 6-8)",
     (r"law enforcement", r"police", r"judicial", r"asylum", r"border")),
)
_LIMITED = (
    ("interacts with people (Art. 50 transparency)",
     (r"chatbot", r"assistant", r"conversation", r"customer service")),
    ("generates content (Art. 50)", (r"generat", r"synthetic", r"deepfake")),
)

_TIER_OBLIGATIONS = {
    "prohibited": ["Do not deploy in the EU (Art. 5)."],
    "high": ["Risk management system (Art. 9)", "Data governance (Art. 10)",
             "Technical documentation + logging (Art. 11-12)",
             "Human oversight (Art. 14)", "Accuracy/robustness (Art. 15)",
             "Conformity assessment + CE marking before market (Art. 43)",
             "FRIA where applicable (Art. 27)"],
    "limited": ["Transparency: disclose AI interaction / AI-generated "
                "content (Art. 50)"],
    "minimal": ["Voluntary codes of conduct (Art. 95)"],
}


def classify_ai_system(name: str, purpose: str) -> dict:
    """EU AI Act tier for a described system, with the matched signals."""
    text = f"{name} {purpose}".lower()
    for label, patterns in _PROHIBITED:
        if _find(text, patterns):
            return {"tier": "prohibited", "signals": [label],
                    "obligations": _TIER_OBLIGATIONS["prohibited"]}
    signals = [label for label, patterns in _HIGH_RISK if _find(text, patterns)]
    if signals:
        return {"tier": "high", "signals": signals,
                "obligations": _TIER_OBLIGATIONS["high"]}
    signals = [label for label, patterns in _LIMITED if _find(text, patterns)]
    if signals:
        return {"tier": "limited", "signals": signals,
                "obligations": _TIER_OBLIGATIONS["limited"]}
    return {"tier": "minimal", "signals": [],
            "obligations": _TIER_OBLIGATIONS["minimal"]}


def register_ai_system(name: str, purpose: str, *, provider: str = "",
                       owner: str = "", assessment_id: str = "",
                       registered_by: str = "", _audit: bool = True) -> dict:
    """Add an AI system to the registry, classified on the way in."""
    cls = classify_ai_system(name, purpose)
    record = {
        "id": _AI.new_id(),
        "name": (name or "").strip()[:200],
        "purpose": (purpose or "").strip()[:2000],
        "provider": (provider or "").strip()[:200],
        "owner": (owner or "").strip()[:200],
        "assessment_id": (assessment_id or "").strip()[:64],
        "registered_by": _actor_label(registered_by),
        "created_at": time.time(),
        "status": "registered",
        "tier": cls["tier"],
        "signals": cls["signals"],
        "obligations": cls["obligations"],
        "note": AI_ACT_NOTE,
    }
    if _audit:
        _queue_audit(record, "register", registered_by)
    saved = _AI.save(record, expected_revision=0)
    if _audit:
        saved = _audit_mutation(_AI, "ai_system", saved)
    return saved


_TIER_RANK = {"minimal": 0, "limited": 1, "high": 2, "prohibited": 3}


def register_ai_system_from_assessment(
    assessment_id: str, *, registered_by: str = "",
) -> dict | None:
    """Register the system a completed AIRA assessment describes — the
    assessment feeds the registry the same way a PIA feeds the RoPA.

    Keyword screening runs as usual, then the assessment's own attestations
    ratchet the tier UPWARD only (an attested Art. 5 practice or Annex III
    use outranks quiet wording; nothing ever downgrades)."""
    from .assessment import load_saved
    rec = load_saved(assessment_id)
    if rec is None:
        return None
    answers = rec.get("answers") or {}

    def ans(qid: str) -> str:
        return str((answers.get(qid) or {}).get("answer", ""))

    def note(qid: str) -> str:
        return str((answers.get(qid) or {}).get("note", ""))

    record = register_ai_system(
        rec.get("subject") or "unnamed system",
        note("aira_purpose") or f"See assessment {assessment_id}",
        assessment_id=assessment_id,
        registered_by=registered_by,
    )
    tier, signals = record["tier"], list(record["signals"])
    if ans("aira_prohibited") == "yes" and _TIER_RANK[tier] < 3:
        tier = "prohibited"
        signals.append("assessment attests an Art. 5 prohibited practice")
    elif ans("aira_high_risk") == "yes" and _TIER_RANK[tier] < 2:
        tier = "high"
        signals.append("assessment attests an Annex III high-risk use")
    if tier != record["tier"]:
        def _ratchet(r: dict) -> None:
            r["tier"] = tier
            r["signals"] = signals
            r["obligations"] = _TIER_OBLIGATIONS[tier]
            _queue_audit(r, "assessment_tier_ratchet", registered_by)
        record = _AI.update(record["id"], _ratchet)
        if record is not None:
            record = _audit_mutation(_AI, "ai_system", record)
    return record


def list_ai_systems() -> list[dict]:
    retry_pending_audits()
    return _AI.list()


def get_ai_system(system_id: str) -> dict | None:
    retry_pending_audits()
    return _AI.load(system_id)


# --------------------------------------------------------------------------- #
# RoPA / data inventory -- Art. 30(1)
# --------------------------------------------------------------------------- #

ROPA_FIELDS = ("activity", "purpose", "controller", "data_categories",
               "data_subjects", "recipients", "transfers", "retention",
               "security_measures")


def upsert_ropa(
    record: dict,
    *,
    ropa_id: str = "",
    updated_by: str = "",
    expected_revision: int | None = None,
    _audit: bool = True,
) -> dict | None:
    """Create or atomically update one Art. 30 processing-activity record."""
    rid = (ropa_id or "").strip()
    if rid and expected_revision is None:
        raise ValueError("revision is required when updating a RoPA activity")
    action = "update" if rid else "create"

    def _apply(out: dict) -> None:
        for field in ROPA_FIELDS:
            if record.get(field) is not None:
                out[field] = str(record.get(field, ""))[:2000]
        for field in ROPA_FIELDS:
            out.setdefault(field, "")
        if record.get("assessment_id"):
            out["assessment_id"] = str(record["assessment_id"])[:64]
        out["updated_at"] = time.time()
        out["updated_by"] = _actor_label(updated_by)
        if _audit:
            _queue_audit(out, action, updated_by)

    if rid:
        saved = _ROPA.update(
            rid,
            _apply,
            expected_revision=expected_revision,
        )
    else:
        saved = {
            "id": _ROPA.new_id(),
            "created_at": time.time(),
            "created_by": _actor_label(updated_by),
            "source": record.get("source", "manual"),
            "assessment_id": "",
        }
        _apply(saved)
        saved = _ROPA.save(saved, expected_revision=0)
    if saved is not None and _audit:
        saved = _audit_mutation(_ROPA, "ropa", saved)
    return saved


# PIA answers -> Art. 30 field hints. The draft is explicit about provenance
# ("from PIA ...") and everything stays editable -- a draft, not a verdict.
def draft_ropa_from_assessment(
    assessment_id: str, *, created_by: str = "",
) -> dict | None:
    """Auto-draft a RoPA entry from a completed assessment (the moment the
    assessment flow starts FEEDING the inventory)."""
    from .assessment import load_saved
    rec = load_saved(assessment_id)
    if rec is None:
        return None
    answers = rec.get("answers") or {}

    def ans(qid: str) -> str:
        return str((answers.get(qid) or {}).get("answer", ""))

    def note(qid: str) -> str:
        return str((answers.get(qid) or {}).get("note", ""))

    transfers = "None declared"
    if ans("pia_transfers") == "yes":
        transfers = ("Transfers outside EU/EEA WITHOUT Chapter V safeguard "
                     "declared" + (f" — {note('pia_transfers')}"
                                   if note("pia_transfers") else ""))
    retention = ("Defined retention schedule attested"
                 if ans("pia_retention") == "yes"
                 else "No defined retention period"
                 + (f" — {note('pia_retention')}" if note("pia_retention") else ""))
    security = ("Encryption in transit and at rest attested"
                if ans("pia_security") == "yes" else "Security posture unclear")
    return upsert_ropa({
        "activity": f"Processing via {rec.get('subject', 'unknown system')}",
        "purpose": note("pia_necessity") or "See assessment record",
        "controller": "",
        "data_categories": "Personal data (see assessment"
                           + (", special categories declared)" if
                              ans("pia_special_category") == "yes" else ")"),
        "data_subjects": "See assessment record",
        "recipients": ("Processors under Art. 28 DPA"
                       if ans("pia_processors") == "yes"
                       else "Processor without countersigned DPA declared"),
        "transfers": transfers,
        "retention": retention,
        "security_measures": security,
        "assessment_id": assessment_id,
        "source": f"assessment:{assessment_id}",
    }, updated_by=created_by)


# OneTrust RoPA/Data-Mapping CSV export -> Art. 30 fields. Their column
# names drift across versions and templates, so each field carries the
# aliases seen in real exports; matching is case-insensitive.
_ONETRUST_HEADERS = {
    "activity": ("processing activity name", "processing activity",
                 "activity name", "activity", "name"),
    "purpose": ("purpose of processing", "purposes of processing",
                "purpose", "purposes"),
    "controller": ("controller", "legal entity", "organization"),
    "data_categories": ("personal data categories", "data categories",
                        "categories of personal data", "data elements"),
    "data_subjects": ("categories of data subjects", "data subject types",
                      "data subjects"),
    "recipients": ("categories of recipients", "recipients",
                   "third party recipients"),
    "transfers": ("cross-border transfers", "international transfers",
                  "transfer mechanism", "transfers"),
    "retention": ("retention period", "retention schedule", "retention"),
    "security_measures": ("technical and organizational measures",
                          "security measures", "security"),
}


def import_onetrust_ropa(csv_text: str, *, imported_by: str = "") -> list[dict]:
    """Import OneTrust's RoPA / Data Mapping CSV export into the Art. 30
    register — the "connect to OneTrust" on-ramp: their record comes in with
    ``source="onetrust"`` provenance and stays editable here.

    Tolerant of their column-name drift; rows without a recognizable
    activity name are skipped rather than guessed at."""
    import csv as _csv
    import io
    reader = _csv.DictReader(io.StringIO(csv_text or ""))
    imported = []
    for row in list(reader)[:5000]:
        low = {(k or "").strip().lower(): (v or "").strip()
               for k, v in row.items()}
        entry: dict = {}
        for field, aliases in _ONETRUST_HEADERS.items():
            for alias in aliases:
                if low.get(alias):
                    entry[field] = low[alias]
                    break
        if not entry.get("activity"):
            continue
        entry["source"] = "onetrust"
        saved = upsert_ropa(
            entry,
            updated_by=imported_by,
        )
        if saved is not None:
            imported.append(saved)
    return imported


def list_ropa() -> list[dict]:
    retry_pending_audits()
    return _ROPA.list()


def export_ropa_art30() -> list[dict]:
    """The Art. 30 register, exportable shape (stable field order)."""
    retry_pending_audits()
    return [{f: r.get(f, "") for f in
             ("id", *ROPA_FIELDS, "assessment_id", "source")}
            for r in _ROPA.list()]


# --------------------------------------------------------------------------- #
# DSAR request tracker -- over the real fulfillment machinery
# --------------------------------------------------------------------------- #

DSAR_KINDS = ("access", "portability", "erasure")
DSAR_DUE_DAYS = 30  # GDPR Art. 12(3): one month
_DSAR_EXPORT_SCHEMA = 1
_DSAR_EXPORT_META = "_maverick_export"
_ERASURE_OPERATOR_INSTRUCTION = (
    "Erasure is ready for an authorized operator. Use the authenticated "
    "Maverick erasure workflow; this API intentionally does not render a "
    "shell command."
)


def _erasure_argv(record: dict) -> list[str]:
    """Structured erasure arguments; consumers must execute with no shell."""
    argv = ["maverick", "erase", "--user", str(record.get("subject_id") or "")]
    if record.get("channel"):
        argv.extend(["--channel", str(record["channel"])])
    return argv


def _secure_erasure_handoff(record: dict) -> bool:
    """Remove legacy shell text and enforce the structured handoff shape."""
    if record.get("kind") != "erasure" or not isinstance(
        record.get("fulfillment"), dict,
    ):
        return False
    fulfillment = dict(record["fulfillment"])
    fulfillment.pop("erase_command", None)
    fulfillment["erase_argv"] = _erasure_argv(record)
    fulfillment["operator_instruction"] = _ERASURE_OPERATOR_INSTRUCTION
    if fulfillment == record["fulfillment"]:
        return False
    record["fulfillment"] = fulfillment
    return True


def _dsar_export_intent(record: dict) -> dict:
    from .paths import current_tenant_id

    return {
        "schema": _DSAR_EXPORT_SCHEMA,
        "tenant": current_tenant_id() or "",
        "request_id": str(record.get("id") or ""),
        "subject_id": str(record.get("subject_id") or ""),
        "channel": str(record.get("channel") or ""),
    }


def _dsar_export_digest(intent: dict, bundle: dict) -> str:
    canonical = json.dumps(
        {"intent": intent, "bundle": bundle},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _valid_dsar_bundle(bundle: object, intent: dict) -> bool:
    if not isinstance(bundle, dict) or not isinstance(bundle.get("counts"), dict):
        return False
    subject = bundle.get("subject")
    if not isinstance(subject, dict) or subject.get("user_id") != intent["subject_id"]:
        return False
    expected_channel = intent["channel"] or None
    if expected_channel is not None and subject.get("channel") != expected_channel:
        return False
    return bundle.get("tenant") == (intent["tenant"] or None)


def _load_dsar_export_artifact(
    path: Path,
    *,
    intent: dict,
) -> tuple[dict, str] | None:
    try:
        artifact = _read_private_json(path, root=path.parent)
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return None
    meta = artifact.get(_DSAR_EXPORT_META)
    if not isinstance(meta, dict) or meta.get("intent") != intent:
        return None
    bundle = {key: value for key, value in artifact.items()
              if key != _DSAR_EXPORT_META}
    if not _valid_dsar_bundle(bundle, intent):
        return None
    expected = _dsar_export_digest(intent, bundle)
    digest = str(meta.get("sha256") or "")
    if not digest or not hmac.compare_digest(digest, expected):
        return None
    return bundle, digest


def _write_dsar_export_artifact(path: Path, intent: dict, bundle: dict) -> str:
    from .file_lock import atomic_write_text

    if not _valid_dsar_bundle(bundle, intent):
        raise ValueError("DSAR exporter returned data for a different subject")
    digest = _dsar_export_digest(intent, bundle)
    artifact = {
        **bundle,
        _DSAR_EXPORT_META: {
            "schema": _DSAR_EXPORT_SCHEMA,
            "intent": intent,
            "sha256": digest,
        },
    }
    atomic_write_text(
        path,
        json.dumps(artifact, indent=2, default=str),
        mode=0o600,
    )
    return digest


def open_dsar(
    subject_id: str,
    kind: str,
    *,
    channel: str = "",
    opened_by: str = "",
    _intake: dict | None = None,
) -> dict:
    kind = (kind or "").strip().lower()
    if kind not in DSAR_KINDS:
        raise ValueError(f"kind must be one of {DSAR_KINDS}")
    now = time.time()
    subject = (subject_id or "").strip()[:200]
    if not subject:
        raise ValueError("subject_id is required")
    record = {
        "id": _DSAR.new_id(),
        "subject_id": subject,
        "channel": (channel or "").strip()[:64],
        "kind": kind,
        "opened_by": _actor_label(opened_by),
        "created_at": now,
        "due_at": now + DSAR_DUE_DAYS * 86400,
        "status": "open",
        "fulfillment": None,
    }
    if _intake is not None:
        signals = _intake.get("signals") if isinstance(_intake, dict) else None
        excerpt = _intake.get("excerpt") if isinstance(_intake, dict) else None
        if (
            not isinstance(signals, list)
            or not signals
            or not all(isinstance(signal, str) for signal in signals)
            or not isinstance(excerpt, str)
        ):
            raise ValueError("DSAR intake provenance is invalid")
        record["intake"] = {
            "signals": [signal[:200] for signal in signals[:16]],
            "excerpt": excerpt[:300],
        }
    _queue_audit(record, "open", opened_by)
    saved = _DSAR.save(record, expected_revision=0)
    return _audit_mutation(_DSAR, "dsar", saved)


# What an inbound data-subject request sounds like. Deterministic and
# explainable, same as every engine here: each kind carries its trigger
# patterns; a hit records WHICH phrase matched so the reviewer can verify.
_DSAR_INTENTS = (
    ("erasure", (
        r"delete (?:my|all my) (?:data|account|information|records)",
        r"right to be forgotten", r"right to erasure",
        r"erase (?:my|all my) (?:data|information)",
        r"remove (?:my|all my) (?:personal )?(?:data|information)",
        r"art(?:icle)?\.? ?17",
    )),
    ("portability", (
        r"data portability", r"port my data",
        r"transfer my data to", r"machine.?readable",
        r"art(?:icle)?\.? ?20",
    )),
    ("access", (
        r"copy of (?:my|all) (?:personal )?data",
        r"what (?:data|information) (?:do )?you (?:hold|have|store)",
        r"(?:subject )?access request", r"\bsar\b",
        r"access to my (?:personal )?data",
        r"art(?:icle)?\.? ?15",
    )),
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def detect_dsar(text: str, *, sender: str = "") -> dict | None:
    """Classify an inbound message as a data-subject request (or not).

    Returns ``{"kind", "subject_id", "signals"}`` when the message reads as
    a DSAR, else ``None``. ``subject_id`` is the sender when given,
    otherwise the first email address found in the body. Deterministic --
    a triage aid; opening the request stays an explicit step."""
    low = (text or "")[:20000].lower()
    if not low.strip():
        return None
    for kind, patterns in _DSAR_INTENTS:
        signals = [m.group(0) for pat in patterns
                   for m in [re.search(pat, low)] if m]
        if signals:
            subject = (sender or "").strip()[:200]
            if not subject:
                found = _EMAIL_RE.search(text or "")
                subject = found.group(0)[:200] if found else ""
            return {"kind": kind, "subject_id": subject, "signals": signals}
    return None


def open_dsar_from_message(text: str, *, channel: str = "",
                           sender: str = "", opened_by: str = "") -> dict | None:
    """Open a tracked request from an inbound message (email/chat/web form).

    Detection is deterministic; the created record carries the matched
    signals and an excerpt of the message for provenance. Returns ``None``
    when the message doesn't read as a request or names no subject."""
    hit = detect_dsar(text, sender=sender)
    if hit is None or not hit["subject_id"]:
        return None
    return open_dsar(
        hit["subject_id"],
        hit["kind"],
        channel=channel,
        opened_by=opened_by,
        _intake={
            "signals": hit["signals"],
            "excerpt": (text or "").strip()[:300],
        },
    )


def list_dsars() -> list[dict]:
    retry_pending_audits()
    now = time.time()
    out = []
    for stored in _DSAR.list():
        r = stored
        probe = dict(stored)
        if _secure_erasure_handoff(probe):
            migrated = False

            def _migrate(current: dict) -> None:
                nonlocal migrated
                if _secure_erasure_handoff(current):
                    _queue_audit(current, "secure_erasure_handoff", "system")
                    migrated = True

            updated = _DSAR.update(str(stored.get("id") or ""), _migrate)
            if updated is not None and migrated:
                updated = _audit_mutation(_DSAR, "dsar", updated)
            r = updated or stored
        if "status" not in r:  # not a request record (defensive)
            continue
        r = dict(r)
        r["days_left"] = int((float(r.get("due_at", now)) - now) // 86400)
        r["overdue"] = r["status"] == "open" and r["days_left"] < 0
        out.append(r)
    return out


def fulfill_dsar(request_id: str, *, fulfilled_by: str = "") -> dict | None:
    """Fulfill an open request through the REAL machinery.

    ``access`` / ``portability``: runs :func:`maverick.dsar.export_subject_data`
    and stores the bundle (counts recorded on the request). ``erasure``: never
    destructive from here -- returns structured arguments for an authenticated
    operator workflow (the CLI carries the confirmation flow, knowledge-plane
    scrub, and audit trail). No shell command is constructed or executed."""
    changed = False
    invalid = False

    def _fulfill(record: dict) -> None:
        nonlocal changed, invalid
        if _secure_erasure_handoff(record):
            _queue_audit(record, "secure_erasure_handoff", fulfilled_by)
            changed = True
            return
        # Durable idempotency receipt: once a fulfillment exists, every retry
        # returns it without repeating the export or changing the revision.
        if record.get("fulfillment"):
            return
        if record.get("status") != "open":
            invalid = True
            return
        if record.get("kind") in ("access", "portability"):
            from .file_lock import ensure_private_directory
            from .paths import data_dir

            out_dir = ensure_private_directory(
                data_dir("dsar_requests") / "exports"
            )
            out_path = out_dir / f"{record['id']}-export.json"
            _validated_private_directory(out_dir, must_exist=True)
            intent = _dsar_export_intent(record)
            recovered = _load_dsar_export_artifact(out_path, intent=intent)
            if recovered is None:
                from .dsar import export_subject_data

                bundle = export_subject_data(
                    record["subject_id"],
                    channel=record.get("channel") or None,
                    tenant=intent["tenant"] or None,
                )
                artifact_sha256 = _write_dsar_export_artifact(
                    out_path,
                    intent,
                    bundle,
                )
            else:
                bundle, artifact_sha256 = recovered
            record["status"] = "fulfilled"
            record["fulfilled_at"] = time.time()
            record["fulfilled_by"] = _actor_label(fulfilled_by)
            record["fulfillment"] = {
                "export_path": str(out_path),
                "counts": bundle.get("counts", {}),
                "export_schema": _DSAR_EXPORT_SCHEMA,
                "artifact_sha256": artifact_sha256,
            }
            _queue_audit(record, "fulfill", fulfilled_by)
            changed = True
            return

        # Erasure remains a deliberate operator step. Arguments stay structured
        # so Windows cmd.exe metacharacters are data, never executable syntax.
        # Any consumer must pass this argv to a process API with shell=False.
        record["status"] = "awaiting_erasure"
        record["fulfilled_at"] = time.time()
        record["fulfilled_by"] = _actor_label(fulfilled_by)
        record["fulfillment"] = {
            "erase_argv": _erasure_argv(record),
            "operator_instruction": _ERASURE_OPERATOR_INSTRUCTION,
        }
        _queue_audit(record, "fulfill", fulfilled_by)
        changed = True

    saved = _DSAR.update(request_id, _fulfill)
    if saved is None or invalid:
        return None
    if changed or isinstance(saved.get("_audit_pending"), (dict, list)):
        saved = _audit_mutation(_DSAR, "dsar", saved)
    return saved


def close_dsar(request_id: str, *, closed_by: str = "") -> dict | None:
    changed = False

    def _close(r: dict) -> None:
        nonlocal changed
        if r.get("status") == "closed":
            return
        _secure_erasure_handoff(r)
        r["status"] = "closed"
        r["closed_at"] = time.time()
        r["closed_by"] = _actor_label(closed_by)
        _queue_audit(r, "close", closed_by)
        changed = True

    saved = _DSAR.update(request_id, _close)
    if saved is not None and (
        changed or isinstance(saved.get("_audit_pending"), (dict, list))
    ):
        saved = _audit_mutation(_DSAR, "dsar", saved)
    return saved


# --------------------------------------------------------------------------- #
# Privacy incident / breach register -- Art. 33/34
# --------------------------------------------------------------------------- #

INCIDENT_SEVERITIES = ("low", "medium", "high")
BREACH_NOTIFY_HOURS = 72  # Art. 33(1): without undue delay, within 72h


def open_incident(title: str, *, severity: str = "medium",
                  description: str = "", categories: str = "",
                  affected_estimate: str = "", reported_by: str = "") -> dict:
    """Open a privacy-incident record; the Art. 33 clock starts at discovery
    (= now). Whether the incident is a notifiable breach stays a HUMAN
    decision recorded via :func:`decide_incident_notification` -- the
    register tracks the clock, it never notifies anyone itself."""
    severity = (severity or "medium").strip().lower()
    if severity not in INCIDENT_SEVERITIES:
        raise ValueError(f"severity must be one of {INCIDENT_SEVERITIES}")
    now = time.time()
    record = {
        "id": _INCIDENT.new_id(),
        "title": (title or "").strip()[:200],
        "description": (description or "").strip()[:4000],
        "categories": (categories or "").strip()[:500],
        "affected_estimate": (affected_estimate or "").strip()[:100],
        "severity": severity,
        "reported_by": _actor_label(reported_by),
        "created_at": now,
        "notify_deadline_at": now + BREACH_NOTIFY_HOURS * 3600,
        "status": "open",
        "notification": None,
    }
    if not record["title"]:
        raise ValueError("title is required")
    _queue_audit(record, "open", reported_by)
    saved = _INCIDENT.save(record, expected_revision=0)
    return _audit_mutation(_INCIDENT, "incident", saved)


def decide_incident_notification(incident_id: str, notifiable: bool, *,
                                 rationale: str = "",
                                 decided_by: str = "") -> dict | None:
    """Record the human call on Art. 33/34 notification. ``notifiable=True``
    marks the record ``notify`` (the operator runs the actual notification
    through their authority's channel); ``False`` documents WHY not --
    Art. 33(5) requires documenting non-notified breaches too."""
    if not isinstance(notifiable, bool):
        raise ValueError("notifiable must be a boolean")
    decision_rationale = (rationale or "").strip()[:2000]
    if not decision_rationale:
        raise ValueError("notification rationale is required")
    changed = False

    def _mut(r: dict) -> None:
        nonlocal changed
        if r.get("status") == "closed":
            raise RecordConflict("closed privacy incident cannot be changed")
        r["notification"] = {
            "notifiable": notifiable,
            "rationale": decision_rationale,
            "decided_by": _actor_label(decided_by),
            "decided_at": time.time(),
        }
        r["status"] = "notify" if notifiable else "documented"
        _queue_audit(r, "notification_decision", decided_by)
        changed = True

    saved = _INCIDENT.update(incident_id, _mut)
    if saved is not None and (
        changed or isinstance(saved.get("_audit_pending"), (dict, list))
    ):
        saved = _audit_mutation(_INCIDENT, "incident", saved)
    return saved


def close_incident(incident_id: str, *, closed_by: str = "") -> dict | None:
    changed = False

    def _mut(r: dict) -> None:
        nonlocal changed
        if r.get("status") == "closed":
            return
        r["status"] = "closed"
        r["closed_at"] = time.time()
        r["closed_by"] = _actor_label(closed_by)
        _queue_audit(r, "close", closed_by)
        changed = True

    saved = _INCIDENT.update(incident_id, _mut)
    if saved is not None and (
        changed or isinstance(saved.get("_audit_pending"), (dict, list))
    ):
        saved = _audit_mutation(_INCIDENT, "incident", saved)
    return saved


def list_incidents() -> list[dict]:
    retry_pending_audits()
    now = time.time()
    out = []
    for r in _INCIDENT.list():
        if "status" not in r:  # not an incident record (defensive)
            continue
        r = dict(r)
        remaining = float(r.get("notify_deadline_at", now)) - now
        r["hours_left"] = int(remaining // 3600)
        # The 72h clock matters while the notification call is unmade.
        r["clock_breached"] = (r.get("notification") is None
                               and r.get("status") == "open"
                               and remaining < 0)
        out.append(r)
    return out


# --------------------------------------------------------------------------- #
# Program report -- the board-pack aggregate over every register
# --------------------------------------------------------------------------- #

def program_report(assessment_types: set | None = None) -> dict:
    """One deterministic aggregate over the whole privacy program: the
    assessment worklist posture, DSAR SLA performance, the incident log,
    DPA coverage, AI Act tiers, and the Art. 30 register. Everything is
    computed from the records themselves -- nothing estimated."""
    from collections import Counter

    from .assessment import list_saved

    types = assessment_types or {"pia", "aira", "vendor_risk", "tia",
                                 "hipaa", "soc2", "pci_dss"}
    rows = [s for s in list_saved() if s.get("type") in types]
    status = Counter(r["status"] for r in rows)
    residual = Counter(r["residual_risk"] for r in rows)
    now = time.time()
    open_rows = [r for r in rows
                 if r["status"] in ("pending_review", "needs_more")]
    assessments = {
        "total": len(rows),
        "by_status": dict(status),
        "by_residual": dict(residual),
        "review_due": sum(1 for r in rows if r.get("review_due")),
        "open": len(open_rows),
        "oldest_open_days": (max(int((now - float(r.get("created_at") or now))
                                     // 86400) for r in open_rows)
                             if open_rows else 0),
    }

    dsars = list_dsars()
    closed_like = [d for d in dsars if d["status"] in ("fulfilled", "closed",
                                                       "awaiting_erasure")]
    on_time = sum(1 for d in closed_like
                  if float(d.get("fulfilled_at")
                           or d.get("closed_at")
                           or d.get("created_at") or 0)
                  <= float(d.get("due_at") or 0))
    dsar = {
        "total": len(dsars),
        "open": sum(1 for d in dsars if d["status"] == "open"),
        "overdue": sum(1 for d in dsars if d.get("overdue")),
        "by_kind": dict(Counter(d["kind"] for d in dsars)),
        "handled": len(closed_like),
        "handled_on_time": on_time,
    }

    incidents = list_incidents()
    incident = {
        "total": len(incidents),
        "open": sum(1 for i in incidents
                    if i["status"] in ("open", "notify")),
        "past_72h_undecided": sum(1 for i in incidents
                                  if i.get("clock_breached")),
        "notified": sum(1 for i in incidents
                        if (i.get("notification") or {}).get("notifiable")),
        "documented_no_notify": sum(
            1 for i in incidents
            if i.get("notification") is not None
            and not i["notification"].get("notifiable")),
    }

    dpas = list_dpa_reviews()
    clause_gaps: Counter = Counter()
    for summary in dpas:
        full = get_dpa_review(summary["id"]) or {}
        for c in full.get("clauses", []):
            if c.get("status") != "present":
                clause_gaps[c.get("requirement", c.get("key", "?"))] += 1
    dpa = {
        "total": len(dpas),
        "avg_clauses_present": (round(sum(d.get("clauses_present", 0)
                                          for d in dpas) / len(dpas), 1)
                                if dpas else 0.0),
        "high_residual": sum(1 for d in dpas
                             if d.get("residual_risk") == "high"),
        "top_gaps": [{"requirement": k, "reviews_missing_it": v}
                     for k, v in clause_gaps.most_common(5)],
    }

    ai = {"total": 0, "by_tier": {}}
    systems = list_ai_systems()
    ai["total"] = len(systems)
    ai["by_tier"] = dict(Counter(s.get("tier", "?") for s in systems))

    ropa_rows = list_ropa()
    ropa = {
        "total": len(ropa_rows),
        "with_transfers": sum(
            1 for r in ropa_rows
            if (r.get("transfers") or "").strip().lower()
            not in ("", "none", "none declared", "n/a")),
        "from_assessments": sum(1 for r in ropa_rows
                                if str(r.get("source", "")).startswith(
                                    "assessment:")),
        "from_onetrust": sum(1 for r in ropa_rows
                             if r.get("source") == "onetrust"),
    }

    return {"generated_at": now, "assessments": assessments, "dsar": dsar,
            "incidents": incident, "dpa": dpa, "ai": ai, "ropa": ropa}


__all__ = [
    "AI_ACT_NOTE", "BREACH_NOTIFY_HOURS", "DPA_CHECKLIST", "DSAR_DUE_DAYS",
    "DSAR_KINDS", "close_incident", "decide_incident_notification",
    "list_incidents", "open_incident", "program_report",
    "ROPA_FIELDS", "RecordConflict", "classify_ai_system", "close_dsar",
    "draft_ropa_from_assessment", "enabled", "export_ropa_art30",
    "detect_dsar", "fulfill_dsar", "get_ai_system", "get_dpa_review",
    "import_onetrust_ropa", "list_ai_systems", "open_dsar_from_message",
    "list_dpa_reviews", "list_dsars", "list_ropa", "open_dsar",
    "register_ai_system", "register_ai_system_from_assessment",
    "retry_pending_audits", "review_dpa",
    "upsert_ropa",
]
