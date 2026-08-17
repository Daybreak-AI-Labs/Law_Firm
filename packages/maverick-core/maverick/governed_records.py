"""Reusable CAS record store with a durable at-least-once audit outbox.

Security, privacy, agent-governance, product-security, and evidence records all
need the same persistence contract: tenant scoping, monotonic revisions,
compare-and-swap mutations, and a signed audit event that cannot be lost when
the audit writer is temporarily unavailable.  This module keeps that contract
in one place rather than letting each product invent a weaker variation.

The local backend remains :class:`maverick.privacy_ops._RecordStore` for the
single-replica compatibility posture.  A Postgres backend provides the same
contract across hosts.  Enterprise and multi-replica deployments fail closed
unless that shared backend and an explicit tenant are available.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .privacy_ops import (
    PrivacyStateError,
    _queue_audit,
    _RecordStore,
    _validate_document_evidence_record,
    _validated_audit_queue,
)

log = logging.getLogger(__name__)

_EVENT_KIND = "governed_record_changed"
_MAX_DELIVERIES_PER_FLUSH = 16
_MAX_RECORD_JSON_BYTES = 16 * 1024 * 1024
_MAX_STORED_RECORD_BYTES = 24 * 1024 * 1024
_POSTGRES_TABLE = "governed_records"
_POSTGRES_SCHEMA_LOCK = 0x6D766B677276  # ``mvkgrv``
_POSTGRES_COLUMNS = (
    ("tenant_id", "text", "NO"),
    ("namespace", "text", "NO"),
    ("record_id", "text", "NO"),
    ("revision", "bigint", "NO"),
    ("record_json", "text", "NO"),
    ("created_at", "double precision", "NO"),
    ("updated_at", "double precision", "NO"),
)
_POSTGRES_PRIMARY_KEY = ("tenant_id", "namespace", "record_id")
# Valid tenant ids have an encoded path segment of at most 200 characters.
# This 201-character value therefore cannot collide with a real tenant, while
# preserving the legacy unbound root for explicitly non-strict deployments.
_UNBOUND_TENANT_KEY = "_" * 201
# Deployment-global control-plane authorities must not inherit an ambient
# request tenant, but they also must not share the legacy unbound row.  A real
# tenant can never produce a 201-character canonical id: its portable segment
# is bounded to 200 characters by ``paths.MAX_TENANT_SEGMENT_LENGTH``.  Keeping
# this key private and selecting it only through the explicit authority scope
# below makes the row unreachable through ordinary tenant binding.
_DEPLOYMENT_GLOBAL_TENANT_KEY = "G" * 201
_AUTHORITY_SCOPES = frozenset({"tenant", "deployment_global"})
_RECORD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_BACKENDS = frozenset({"auto", "local"})
_REQUIRED_BACKENDS = frozenset({"local"})
_RESERVED_FIELDS = frozenset({
    "_audit_pending",
    "created_at",
    "updated_at",
    "revision",
})


@dataclass(frozen=True)
class GovernedMutationFence:
    """One authority-document predicate checked inside a target mutation.

    Shared cross-record invariants cannot be protected by a lease pre-check:
    the holder may expire after the check and before its target row commits.
    A Postgres backend evaluates this predicate after locking the authority
    document and before touching the target row, in the *same transaction* as
    the target mutation.  A takeover therefore either waits for that commit or
    makes the stale holder fail before it can write.

    The predicate must be deterministic, side-effect free, and raise on a
    missing, expired, or otherwise invalid permit.  Local callers should keep
    using their strict filesystem critical section instead of this shared-only
    primitive.
    """

    namespace: str
    prefix: str
    record_id: str
    authority_scope: str
    validate: Callable[[dict[str, Any], float], None]

    def __post_init__(self) -> None:
        _required(self.namespace, "fence namespace", 128)
        prefix = _required(self.prefix, "fence prefix", 16)
        _valid_record_id(self.record_id, prefix)
        if self.authority_scope not in _AUTHORITY_SCOPES:
            raise ValueError("fence authority_scope must be tenant or deployment_global")
        if not callable(self.validate):
            raise TypeError("fence validator must be callable")


def _required(value: object, label: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    if len(text) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    return text


def _list_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("list limit must be an integer")
    # One sentinel row beyond a 10,000-record operational ceiling lets callers
    # detect overflow without issuing an unbounded read.
    if value < 1 or value > 10_001:
        raise ValueError("list limit must be between 1 and 10001")
    return value


def _capacity_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("record capacity must be an integer")
    if value < 1:
        raise ValueError("record capacity must be positive")
    return value


class GovernedRecordBackendError(RuntimeError):
    """A governed-record backend cannot preserve its persistence contract."""


class GovernedRecordCapacityError(GovernedRecordBackendError):
    """A bounded governed namespace has no admission slot for a new identity."""


class _GovernedRecordValidationError(ValueError):
    """Controlled validation diagnostic safe to cross the provider boundary."""


class GovernedRecordBackend(Protocol):
    """Narrow persistence seam used by :class:`GovernedRecordStore`."""

    kind: str
    shared: bool

    def new_id(self) -> str: ...

    def authoritative_time(self) -> float: ...

    def save(
        self,
        record: dict[str, Any],
        *,
        expected_revision: int | None = None,
        fence: GovernedMutationFence | None = None,
        max_records: int | None = None,
    ) -> dict[str, Any]: ...

    def load(self, record_id: str) -> dict[str, Any] | None: ...

    def list(self, *, limit: int | None = None) -> list[dict[str, Any]]: ...

    def iter_record_ids(
        self,
        *,
        start_after: str = "",
        limit: int = 100,
    ) -> Iterator[tuple[str, str]]: ...

    def update(
        self,
        record_id: str,
        mutate: Callable[[dict[str, Any]], None],
        *,
        expected_revision: int | None = None,
        metadata_only: bool = False,
        fence: GovernedMutationFence | None = None,
    ) -> dict[str, Any] | None: ...


class LocalGovernedRecordBackend:
    """Existing private-file backend for one control-plane replica."""

    kind = "local"
    shared = False

    def __init__(
        self,
        namespace: str,
        prefix: str,
        *,
        authority_scope: str = "tenant",
    ) -> None:
        scope = str(authority_scope or "").strip().lower()
        if scope not in _AUTHORITY_SCOPES:
            raise ValueError(
                "authority_scope must be tenant or deployment_global"
            )
        if scope == "deployment_global":
            self._store = _DeploymentGlobalRecordStore(namespace, prefix)
        else:
            self._store = _RecordStore(namespace, prefix)
        self.authority_scope = scope

    def new_id(self) -> str:
        return self._store.new_id()

    @staticmethod
    def authoritative_time() -> float:
        return time.time()

    def save(
        self,
        record: dict[str, Any],
        *,
        expected_revision: int | None = None,
        fence: GovernedMutationFence | None = None,
        max_records: int | None = None,
    ) -> dict[str, Any]:
        if fence is not None:
            raise GovernedRecordBackendError(
                "transactional mutation fences require a shared backend"
            )
        if max_records is None:
            return self._store.save(
                record,
                expected_revision=expected_revision,
            )
        maximum = _capacity_limit(max_records)
        from .file_lock import cross_process_lock, ensure_private_directory

        record_dir = self._store._dir()
        ensure_private_directory(record_dir)
        # All bounded creates for this local namespace take the same admission
        # lock. The capacity check and per-record atomic create therefore form
        # one cross-process critical section without changing legacy updates.
        admission_target = record_dir / ".namespace-admission"
        with cross_process_lock(admission_target, strict=True):
            record_id = _valid_record_id(record.get("id"), self._store.prefix)
            existing = self._store.load(record_id)
            if (
                existing is None
                and len(self._store.list_bounded(limit=maximum)) >= maximum
            ):
                raise GovernedRecordCapacityError(
                    "governed record namespace capacity has been reached"
                )
            return self._store.save(
                record,
                expected_revision=expected_revision,
            )

    def load(self, record_id: str) -> dict[str, Any] | None:
        return self._store.load(record_id)

    def list(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is None:
            return self._store.list()
        bounded = _list_limit(limit)
        return self._store.list_bounded(limit=bounded)

    def iter_record_ids(
        self,
        *,
        start_after: str = "",
        limit: int = 100,
    ) -> Iterator[tuple[str, str]]:
        return self._store.iter_record_ids(
            start_after=start_after,
            limit=limit,
        )

    def update(
        self,
        record_id: str,
        mutate: Callable[[dict[str, Any]], None],
        *,
        expected_revision: int | None = None,
        metadata_only: bool = False,
        fence: GovernedMutationFence | None = None,
    ) -> dict[str, Any] | None:
        if fence is not None:
            raise GovernedRecordBackendError(
                "transactional mutation fences require a shared backend"
            )
        return self._store.update(
            record_id,
            mutate,
            expected_revision=expected_revision,
            metadata_only=metadata_only,
        )


class _DeploymentGlobalRecordStore(_RecordStore):
    """Local compatibility projection that cannot inherit request tenancy."""

    def _dir(self):
        from .paths import data_dir

        return data_dir(self.name, tenant=None)


def _valid_record_id(record_id: object, prefix: str) -> str:
    value = str(record_id or "")
    if not _RECORD_ID_RE.fullmatch(value) or not value.startswith(f"{prefix}-"):
        raise ValueError("invalid governed record id")
    return value


def _expected_revision(value: int | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("expected governed record revision is invalid")
    return value


def _canonical_json(record: Mapping[str, Any]) -> str:
    try:
        value = json.dumps(
            dict(record),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _GovernedRecordValidationError(
            "governed record is not valid bounded JSON"
        ) from exc
    if len(value.encode("utf-8")) > _MAX_RECORD_JSON_BYTES:
        raise _GovernedRecordValidationError(
            "governed record exceeds the 16 MiB JSON limit"
        )
    return value


def _logical_digest(record: Mapping[str, Any]) -> str:
    logical = {key: value for key, value in record.items() if key != "_audit_pending"}
    return hashlib.sha256(_canonical_json(logical).encode("utf-8")).hexdigest()


def _prepared_record(
    record: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    bump_revision: bool,
) -> dict[str, Any]:
    saved = dict(record)
    prior_revision = _RecordStore._revision(dict(previous) if previous else None)
    saved["revision"] = prior_revision + (1 if bump_revision else 0)
    pending_items = _validated_audit_queue(saved)
    if pending_items:
        digest = _logical_digest(saved)
        stamped: list[dict[str, Any]] = []
        for item in pending_items:
            receipt = dict(item)
            receipt.setdefault("revision", saved["revision"])
            receipt.setdefault("status", str(saved.get("status") or ""))
            receipt.setdefault("record_sha256", digest)
            stamped.append(receipt)
        saved["_audit_pending"] = stamped
        _validated_audit_queue(saved)
    else:
        saved.pop("_audit_pending", None)
    # Validate and size the exact logical document before opening a transaction.
    _canonical_json(saved)
    return saved


def _encode_record(
    record: Mapping[str, Any],
    *,
    require_encryption: bool,
) -> str:
    raw = _canonical_json(record)
    from .crypto_at_rest import (
        at_rest_enabled,
        deployment_key_scope,
        external_fleet_key_scope,
        seal_to_str,
    )

    encryption_enabled = at_rest_enabled()
    if require_encryption and not encryption_enabled:
        raise GovernedRecordBackendError(
            "shared governed records require application at-rest encryption"
        )
    if require_encryption:
        # Shared rows use the externally pinned deployment key even when the
        # rest of the deployment opts into node-local tenant DEKs. This keeps
        # every Postgres reader decryptable without claiming an unfinished
        # cross-replica tenant-KMS authority.
        with deployment_key_scope(), external_fleet_key_scope():
            stored = seal_to_str(raw)
    else:
        stored = seal_to_str(raw) if encryption_enabled else raw
    if len(stored.encode("utf-8")) > _MAX_STORED_RECORD_BYTES:
        raise _GovernedRecordValidationError(
            "sealed governed record exceeds the 24 MiB storage limit"
        )
    return stored


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("governed record JSON contains a duplicate key")
        value[key] = item
    return value


def _decode_record(
    stored: object,
    *,
    record_id: str,
    prefix: str,
    revision: object,
    created_at: object,
    updated_at: object,
    require_encryption: bool,
) -> dict[str, Any]:
    if not isinstance(stored, str):
        raise PrivacyStateError("governed record payload is not text")
    if len(stored.encode("utf-8")) > _MAX_STORED_RECORD_BYTES:
        raise PrivacyStateError("governed record payload exceeds its storage bound")
    from .crypto_at_rest import (
        at_rest_enabled,
        deployment_key_scope,
        external_fleet_key_scope,
        is_sealed_str,
        strict_at_rest,
        unseal_from_str,
    )

    sealed = is_sealed_str(stored)
    if require_encryption and not sealed:
        raise PrivacyStateError(
            "unsealed shared governed record withheld by encryption floor"
        )
    if at_rest_enabled() and strict_at_rest() and not sealed:
        raise PrivacyStateError(
            "unsealed governed record withheld in strict mode"
        )
    if sealed and require_encryption:
        with deployment_key_scope(), external_fleet_key_scope():
            raw = unseal_from_str(stored)
    else:
        raw = unseal_from_str(stored) if sealed else stored
    if len(raw.encode("utf-8")) > _MAX_RECORD_JSON_BYTES:
        raise PrivacyStateError("governed record JSON exceeds its size bound")
    try:
        value = json.loads(raw, object_pairs_hook=_json_object)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PrivacyStateError("governed record JSON is invalid") from exc
    if not isinstance(value, dict):
        raise PrivacyStateError("governed record JSON is not an object")
    if value.get("id") != record_id:
        raise PrivacyStateError("governed record identity does not match its row")
    body_revision = _RecordStore._revision(value)
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or body_revision != revision
    ):
        raise PrivacyStateError("governed record revision does not match its row")
    for key, column in (("created_at", created_at), ("updated_at", updated_at)):
        body_value = value.get(key)
        if isinstance(body_value, bool) or isinstance(column, bool):
            raise PrivacyStateError("governed record timestamp is invalid")
        try:
            body_timestamp = float(body_value)
            column_timestamp = float(column)
        except (TypeError, ValueError) as exc:
            raise PrivacyStateError("governed record timestamp is invalid") from exc
        if not math.isfinite(body_timestamp) or body_timestamp != column_timestamp:
            raise PrivacyStateError("governed record timestamp does not match its row")
    _validated_audit_queue(value)
    _validate_document_evidence_record(value)
    _valid_record_id(record_id, prefix)
    return value


def _row_record(
    row: object,
    *,
    record_id: str,
    prefix: str,
    require_encryption: bool,
) -> dict[str, Any]:
    if not isinstance(row, (tuple, list)) or len(row) != 4:
        raise PrivacyStateError("governed record row has an invalid shape")
    return _decode_record(
        row[1],
        record_id=record_id,
        prefix=prefix,
        revision=row[0],
        created_at=row[2],
        updated_at=row[3],
        require_encryption=require_encryption,
    )


@dataclass(frozen=True)
class _BackendSelection:
    kind: str
    dsn: str
    shared_required: bool


@dataclass(frozen=True)
class _BackendAuthorityToken:
    """Non-secret identity of one deployment-selected persistence plane."""

    kind: str
    dsn_sha256: str
    shared_required: bool


def _backend_authority_token(selection: _BackendSelection) -> _BackendAuthorityToken:
    digest = (
        hashlib.sha256(selection.dsn.encode("utf-8")).hexdigest()
        if selection.dsn
        else ""
    )
    return _BackendAuthorityToken(
        kind=selection.kind,
        dsn_sha256=digest,
        shared_required=selection.shared_required,
    )


def _backend_authority_matches(
    expected: _BackendAuthorityToken,
    current: _BackendAuthorityToken,
) -> bool:
    return (
        expected.kind == current.kind
        and hmac.compare_digest(expected.dsn_sha256, current.dsn_sha256)
        and expected.shared_required is current.shared_required
    )


_PROCESS_AUTHORITY_LOCK = threading.Lock()
_PROCESS_AUTHORITY_TOKEN: _BackendAuthorityToken | None = None


def _admit_process_authority_selection(selection: _BackendSelection) -> None:
    """Atomically pin the first resolved deployment authority for this process."""
    global _PROCESS_AUTHORITY_TOKEN

    current = _backend_authority_token(selection)
    with _PROCESS_AUTHORITY_LOCK:
        expected = _PROCESS_AUTHORITY_TOKEN
        if expected is None:
            _PROCESS_AUTHORITY_TOKEN = current
            return
        if not _backend_authority_matches(expected, current):
            raise GovernedRecordBackendError(
                "governed-record deployment authority changed after process admission"
            )


def _reset_process_authority_pin_for_testing() -> None:
    """Clear process-lifetime authority admission state for isolated tests only."""
    global _PROCESS_AUTHORITY_TOKEN

    with _PROCESS_AUTHORITY_LOCK:
        _PROCESS_AUTHORITY_TOKEN = None


def _replica_count() -> int:
    raw: object = os.environ.get("MAVERICK_REPLICA_COUNT", "1")
    if isinstance(raw, bool):
        raise GovernedRecordBackendError("replica count must be a positive integer")
    try:
        parsed = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise GovernedRecordBackendError(
            "replica count must be a positive integer"
        ) from exc
    if parsed < 1 or str(raw).strip() != str(parsed):
        raise GovernedRecordBackendError("replica count must be a positive integer")
    return parsed


def _selection() -> _BackendSelection:
    try:
        from .config import (
            governed_records_config_source_errors,
            load_governed_records_config,
        )

        config = load_governed_records_config()
        source_errors = governed_records_config_source_errors()
    except Exception as exc:
        raise GovernedRecordBackendError(
            "governed-record deployment policy is unavailable"
        ) from exc
    if not isinstance(config, Mapping):
        raise GovernedRecordBackendError("governed-record deployment policy is invalid")
    section = config.get("governed_records") or {}
    world = config.get("world_model") or {}
    if not isinstance(section, Mapping) or not isinstance(world, Mapping):
        raise GovernedRecordBackendError("governed-record deployment policy is invalid")
    configured = (
        os.environ.get("MAVERICK_GOVERNED_RECORDS_BACKEND")
        or section.get("backend")
        or "auto"
    )
    kind = str(configured).strip().lower()
    if kind not in _BACKENDS:
        raise GovernedRecordBackendError(
            "governed-record backend must be auto or local"
        )
    from .enterprise import deployment_enterprise_enabled

    enterprise = deployment_enterprise_enabled(
        config=config,
        source_errors=source_errors,
    )
    replicas = _replica_count()
    shared_required = enterprise or replicas > 1
    if shared_required:
        raise GovernedRecordBackendError(
            "enterprise and multi-replica governed records require a shared "
            "backend; this deployment is SQLite-only"
        )
    return _BackendSelection("local", "", False)


def configured_governed_records_backend() -> str:
    """Return the configured governed-record authority backend.

    This selection seam does not open a database or resolve a tenant. Once it
    admits either local or Postgres, however, that complete non-secret authority
    identity is process-sticky: a backend, DSN, or shared-enforcement change
    requires a process restart rather than allowing a later adapter call to
    choose a different plane. :class:`GovernedRecordStore` still performs its
    stricter first-I/O tenant, encryption, schema, and cutover checks.
    """
    try:
        selection = _selection()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        with _PROCESS_AUTHORITY_LOCK:
            admitted = _PROCESS_AUTHORITY_TOKEN is not None
        if admitted:
            raise GovernedRecordBackendError(
                "governed-record deployment authority cannot be revalidated"
            ) from None
        raise
    _admit_process_authority_selection(selection)
    return selection.kind


class GovernedRecordStore:
    """A product-neutral governed record collection.

    ``record_type`` is emitted into the audit chain and should remain stable
    across releases.  Callers own their record schema and lifecycle rules; this
    class owns only durability, CAS, and audit delivery.
    """

    def __init__(
        self,
        namespace: str,
        prefix: str,
        record_type: str,
        *,
        event_kind: str = _EVENT_KIND,
        backend: GovernedRecordBackend | None = None,
        required_backend: str | None = None,
        authority_scope: str = "tenant",
    ) -> None:
        self.namespace = _required(namespace, "namespace", 128)
        self.prefix = _required(prefix, "prefix", 16)
        self.record_type = _required(record_type, "record_type", 64)
        self.event_kind = _required(event_kind, "event_kind", 64)
        scope = str(authority_scope or "").strip().lower()
        if scope not in _AUTHORITY_SCOPES:
            raise ValueError(
                "authority_scope must be tenant or deployment_global"
            )
        self.authority_scope = scope
        self._local_backend = LocalGovernedRecordBackend(
            self.namespace,
            self.prefix,
            authority_scope=self.authority_scope,
        )
        self._backend_override = backend
        required = (
            None
            if required_backend is None
            else str(required_backend).strip().lower()
        )
        if required is not None and required not in _REQUIRED_BACKENDS:
            raise ValueError("required_backend must be local or None")
        self._required_backend = required
        self._authority_token: _BackendAuthorityToken | None = None
        self._authority_lock = threading.Lock()

    def _assert_backend_authority(self, selection: _BackendSelection) -> None:
        """Pin selector-backed stores before any persistence-plane I/O.

        Explicit backend objects are already concrete pins, but mandatory
        authorities still require their declared ``kind`` and sharing posture
        to match the deployment selector exactly.
        """
        required = self._required_backend
        if required is None:
            return
        # ``required_backend`` is a deployment-policy constraint, not merely a
        # property check on an injected adapter.  An explicit shared test or
        # integration backend must still agree with the process-selected plane;
        # otherwise a caller could select local in configuration while writing
        # a supposedly mandatory authority through an unrelated shared object.
        if selection.kind != required:
            raise GovernedRecordBackendError(
                "governed-record deployment authority changed after store selection"
            )
        override = self._backend_override
        if override is not None:
            override_kind = str(getattr(override, "kind", "")).strip().lower()
            if override_kind != required:
                raise GovernedRecordBackendError(
                    "governed-record explicit backend does not match required authority"
                )
            if required == "local" and bool(override.shared):
                raise GovernedRecordBackendError(
                    "governed-record authority requires a local backend"
                )
            if (
                self.authority_scope == "deployment_global"
                and getattr(override, "authority_scope", None)
                != "deployment_global"
            ):
                raise GovernedRecordBackendError(
                    "deployment-global governed authority requires a "
                    "deployment-global backend"
                )
            return
        current = _backend_authority_token(selection)
        with self._authority_lock:
            expected = self._authority_token
            if expected is None:
                self._authority_token = current
                return
            if not _backend_authority_matches(expected, current):
                raise GovernedRecordBackendError(
                    "governed-record deployment authority changed after store selection"
                )

    @staticmethod
    def _require_bound_tenant(shared_required: bool) -> None:
        if not shared_required:
            return
        from .paths import current_tenant_id_strict

        if current_tenant_id_strict() is None:
            raise GovernedRecordBackendError(
                "enterprise and multi-replica governed records require an "
                "active tenant"
            )

    @staticmethod
    def _require_application_encryption(
        required: bool,
        *,
        require_shared_key_identity: bool,
    ) -> None:
        if not required:
            return
        from .crypto_at_rest import (
            EncryptionUnavailable,
            at_rest_enabled,
            require_shared_deployment_key_identity,
        )

        if not at_rest_enabled():
            raise GovernedRecordBackendError(
                "shared governed records require application at-rest encryption"
            )
        if require_shared_key_identity:
            try:
                require_shared_deployment_key_identity()
            except EncryptionUnavailable as exc:
                raise GovernedRecordBackendError(
                    "shared governed-record encryption key identity is unavailable"
                ) from exc

    def _local_records_exist(self) -> bool:
        return next(
            iter(self._local_backend.iter_record_ids(limit=1)),
            None,
        ) is not None

    def _backend(self) -> GovernedRecordBackend:
        try:
            selection = _selection()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            with _PROCESS_AUTHORITY_LOCK:
                process_admitted = _PROCESS_AUTHORITY_TOKEN is not None
            if self._required_backend is not None or process_admitted:
                raise GovernedRecordBackendError(
                    "governed-record deployment authority cannot be revalidated"
                ) from None
            raise
        # The first successfully resolved selector identity wins the process.
        # Admit it before tenant/encryption checks, backend construction, legacy
        # scans, or record I/O so concurrent local/Postgres first use cannot
        # touch two persistence planes.
        _admit_process_authority_selection(selection)
        self._assert_backend_authority(selection)
        if self.authority_scope != "deployment_global":
            self._require_bound_tenant(selection.shared_required)
        if self._backend_override is not None:
            selected = self._backend_override
            if selection.shared_required and not bool(selected.shared):
                raise GovernedRecordBackendError(
                    "enterprise and multi-replica governed records require a "
                    "shared backend"
                )
        else:
            selected = self._local_backend
        self._require_application_encryption(
            selection.shared_required,
            require_shared_key_identity=False,
        )
        if selected.shared and self._local_records_exist():
            raise GovernedRecordBackendError(
                "local governed records exist for this tenant and namespace; "
                "migrate/import them into the shared backend and archive the "
                "local copies before selecting it"
            )
        return selected

    @property
    def backend_kind(self) -> str:
        """Resolved backend name, enforcing the same admission checks as I/O."""
        return self._backend().kind

    def authoritative_time(self) -> float:
        """Return the selected authority's clock for persisted claim leases."""
        backend = self._backend()
        clock = getattr(backend, "authoritative_time", None)
        if not callable(clock):
            if backend.shared:
                raise GovernedRecordBackendError(
                    "shared governed-record backend has no authoritative clock"
                )
            return time.time()
        try:
            value = float(clock())
        except GovernedRecordBackendError:
            raise
        except Exception as exc:
            raise GovernedRecordBackendError(
                "governed-record authoritative time is unavailable"
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise GovernedRecordBackendError(
                "governed-record authoritative time is invalid"
            )
        return value

    def new_id(self) -> str:
        return self._backend().new_id()

    def _flush(
        self,
        record: dict[str, Any],
        *,
        backend: GovernedRecordBackend | None = None,
    ) -> dict[str, Any]:
        """Attempt bounded outbox delivery without changing logical revision."""
        record_id = str(record.get("id") or "")
        outcome: dict[str, Any] | None = None

        def _deliver(current: dict[str, Any]) -> None:
            nonlocal outcome
            queue = _validated_audit_queue(current)
            if not queue:
                outcome = current
                return
            remaining = list(queue)
            for pending in queue[:_MAX_DELIVERIES_PER_FLUSH]:
                try:
                    from . import audit

                    if self.authority_scope == "deployment_global":
                        audit_record = audit.record_global
                        audit_tenant = ""
                    else:
                        from .paths import current_tenant_id_strict

                        audit_record = audit.record
                        audit_tenant = current_tenant_id_strict() or ""
                    ok = audit_record(
                        self.event_kind,
                        agent=str(pending.get("actor") or "system"),
                        event_id=str(pending.get("event_id") or ""),
                        occurred_at=float(pending.get("prepared_at") or 0.0),
                        actor=str(pending.get("actor") or "system"),
                        tenant=audit_tenant,
                        record_type=self.record_type,
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
                        raise RuntimeError("audit writer refused governed event")
                except Exception as exc:  # noqa: BLE001 - receipt stays durable
                    log.error(
                        "governed audit pending for %s/%s (%s)",
                        self.record_type,
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

        resolved = self._backend() if backend is None else backend
        saved = resolved.update(
            record_id,
            _deliver,
            metadata_only=True,
        )
        if saved is None:
            raise RuntimeError("governed record disappeared during audit commit")
        return saved if outcome is None else outcome

    def create(
        self,
        record: dict[str, Any],
        *,
        action: str,
        actor: str,
        fence: GovernedMutationFence | None = None,
        max_records: int | None = None,
    ) -> dict[str, Any]:
        actor_name = _required(actor, "actor", 4096)
        action_name = _required(action, "action", 64)
        value = dict(record)
        reserved = sorted(_RESERVED_FIELDS.intersection(value))
        if reserved:
            raise ValueError(
                "governed record contains store-reserved fields: "
                + ", ".join(reserved)
            )
        now = time.time()
        value["created_at"] = now
        value["updated_at"] = now
        _queue_audit(value, action_name, actor_name)
        maximum = (
            None
            if max_records is None
            else _capacity_limit(max_records)
        )
        backend = self._backend()
        if fence is None and maximum is None:
            saved = backend.save(value, expected_revision=0)
        elif fence is not None and maximum is None:
            saved = backend.save(
                value,
                expected_revision=0,
                fence=fence,
            )
        elif fence is None:
            saved = backend.save(
                value,
                expected_revision=0,
                max_records=maximum,
            )
        else:
            saved = backend.save(
                value,
                expected_revision=0,
                fence=fence,
                max_records=maximum,
            )
        return self._flush(
            saved,
            backend=backend,
        )

    def update(
        self,
        record_id: str,
        mutate: Callable[[dict[str, Any]], None],
        *,
        expected_revision: int,
        action: str,
        actor: str,
        fence: GovernedMutationFence | None = None,
    ) -> dict[str, Any] | None:
        actor_name = _required(actor, "actor", 4096)
        action_name = _required(action, "action", 64)
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")

        def _mutate(current: dict[str, Any]) -> None:
            immutable_id = current.get("id")
            mutate(current)
            if current.get("id") != immutable_id:
                raise ValueError("governed record identity cannot be changed")
            current["updated_at"] = time.time()
            _queue_audit(current, action_name, actor_name)

        backend = self._backend()
        if fence is None:
            saved = backend.update(
                str(record_id or ""),
                _mutate,
                expected_revision=expected_revision,
            )
        else:
            saved = backend.update(
                str(record_id or ""),
                _mutate,
                expected_revision=expected_revision,
                fence=fence,
            )
        if saved is None:
            return None
        return self._flush(saved, backend=backend)

    def get(self, record_id: str) -> dict[str, Any] | None:
        return self._backend().load(record_id)

    def list(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        backend = self._backend()
        return backend.list() if limit is None else backend.list(limit=limit)

    def iter_record_ids(
        self,
        *,
        start_after: str = "",
        limit: int = 100,
    ) -> Iterator[tuple[str, str]]:
        """Yield a bounded rotating window of backend cursor and record ids.

        This exposes the narrow scan primitive already required of every
        governed-record backend without performing any record reads.  Callers
        must persist the first tuple value as the opaque backend cursor and use
        the second value with :meth:`get`.
        """

        return self._backend().iter_record_ids(
            start_after=str(start_after or ""),
            limit=limit,
        )

    def retry_pending(self, *, limit: int = 100) -> int:
        """Retry a bounded set of stable audit receipts."""
        cleared = 0
        backend = self._backend()
        for _, record_id in backend.iter_record_ids(limit=max(0, int(limit))):
            before = backend.load(record_id)
            if not before or not before.get("_audit_pending"):
                continue
            after = self._flush(before, backend=backend)
            if not after.get("_audit_pending"):
                cleared += 1
        return cleared


__all__ = [
    "configured_governed_records_backend",
    "GovernedRecordBackend",
    "GovernedRecordBackendError",
    "GovernedRecordCapacityError",
    "GovernedMutationFence",
    "GovernedRecordStore",
    "LocalGovernedRecordBackend",
]
