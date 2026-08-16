"""Non-executing contracts for governed external training backends.

This module plans work; it never installs dependencies, starts a process,
contacts a service, uploads data, or probes hardware.  An operator-controlled
runner may consume a :class:`TrainingRunSpec` only after independently
revalidating its data boundary and immutable artifact digests.

The Prime RL compatibility target is deliberately exact:

* release ``v0.7.0``;
* annotated tag object ``8cab1e5f...``;
* release commit ``d334ea52...``; and
* the Verifiers submodule commit carried by that release, ``6c64ce6a...``.

Those pins were resolved from the upstream Git repositories.  Mutable branches
such as ``main`` are never accepted.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..paths import InvalidTenantError, canonical_tenant_id
from .environments import (
    BoundaryDecision,
    EnvironmentError,
    EnvironmentPack,
    check_boundary,
    require_training_readiness,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

TRAINING_RUN_SPEC_SCHEMA = "maverick.training-run-spec.v1"
TRAINING_RUN_RESULT_SCHEMA = "maverick.training-run-result.v1"
TRAINING_ADMISSION_SCHEMA = "maverick.training-admission.v1"
TRAINING_CHECKPOINT_MANIFEST_SCHEMA = "maverick.training-checkpoint-manifest.v1"
TRAINING_CHECKPOINT_MANIFEST_BASENAME = "maverick-checkpoint-manifest.json"

PRIME_RL_BACKEND = "prime-rl"
PRIME_RL_VERSION = "0.7.0"
PRIME_RL_TAG = "v0.7.0"
PRIME_RL_TAG_OBJECT = "8cab1e5f6e9ad916d292eb5b49a8800f765ba089"
PRIME_RL_COMMIT = "d334ea52940b47f426293a7d146239e3fbf91caa"
PRIME_RL_VERIFIERS_VERSION = "0.2.0"
PRIME_RL_VERIFIERS_TAG = "v0.2.0"
PRIME_RL_VERIFIERS_SUBMODULE_COMMIT = "6c64ce6a3a01e8edde7c3c0e8e5315fb236e9faa"
PRIME_RL_SOURCE = (
    "https://github.com/PrimeIntellect-ai/prime-rl/tree/"
    f"{PRIME_RL_COMMIT}"
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_REVISION_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,127}\Z")
_PACKAGE_ID_RE = re.compile(r"[a-z][a-z0-9-]{2,127}\Z")
_PYTHON_312_RE = re.compile(r"3\.12(?:\.\d+)?\Z")
_MAX_CHECKPOINT_FILES = 100_000
_MAX_CHECKPOINT_BYTES = 512 * 1024 * 1024 * 1024
_MAX_CHECKPOINT_FILE_BYTES = 256 * 1024 * 1024 * 1024
_MAX_CHECKPOINT_MANIFEST_BYTES = 16 * 1024 * 1024
_BUNDLE_PYTHONPATH_NAME = "PYTHONPATH"

# Only operational, non-secret values may cross into a training process.
_PASSTHROUGH_ENV_NAMES = frozenset({
    "CUDA_VISIBLE_DEVICES",
    "HF_HOME",
    "NCCL_DEBUG",
    "NCCL_SOCKET_IFNAME",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TORCH_HOME",
    "TRANSFORMERS_CACHE",
    "XDG_CACHE_HOME",
})
_OFFLINE_ENV = {
    "DO_NOT_TRACK": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_OFFLINE": "1",
    # Prime launches the orchestrator/trainer through console scripts and
    # torchrun after this guarded entrypoint.  Propagate safe-path semantics so
    # those child interpreters cannot reintroduce the operator workspace ahead
    # of the digest-locked taskset package.
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "UV_OFFLINE": "1",
    "WANDB_DISABLED": "true",
    "WANDB_MODE": "disabled",
}


class TrainingBackendError(RuntimeError):
    """A backend plan or lifecycle transition was refused."""


class TrainingRunState(str, Enum):
    """Portable lifecycle states for an externally executed run."""

    PREPARED = "prepared"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TrainingTarget(str, Enum):
    """The only reviewed destinations for one-tenant training data."""

    TENANT_LOCAL = "tenant_local"
    HOSTED = "hosted"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def canonical_training_tenant_id(value: object) -> str:
    """Validate and normalize one tenant identity without allocating storage."""

    tenant = _bounded_text(value, "tenant_id", 256)
    try:
        return canonical_tenant_id(tenant)
    except InvalidTenantError as exc:
        raise TrainingBackendError(f"tenant_id is invalid: {exc}") from exc


def training_tenant_namespace(tenant_id: object) -> str:
    """Return a collision-resistant, non-reversible filesystem namespace."""

    tenant = canonical_training_tenant_id(tenant_id)
    # An 80-bit namespace keeps deep generated package paths below legacy
    # Windows MAX_PATH while retaining negligible accidental-collision risk.
    digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:20]
    return "tenant-" + digest


def normalize_training_target(value: object) -> TrainingTarget:
    """Parse the closed training-target enum and reject invented destinations."""

    destination = _bounded_text(value, "target", 32)
    try:
        return TrainingTarget(destination)
    except ValueError as exc:
        if destination == "cross_tenant":
            raise TrainingBackendError(
                "cross-tenant training is unsupported; weights are not a privacy boundary",
            ) from exc
        raise TrainingBackendError(
            f"unsupported training target {destination!r}",
        ) from exc


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise TrainingBackendError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_revision(value: object, field: str) -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise TrainingBackendError(f"{field} must be an immutable 40/64-hex revision")
    return value


def _bounded_text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrainingBackendError(f"{field} is required")
    text = value.strip()
    if len(text.encode("utf-8")) > maximum or "\x00" in text:
        raise TrainingBackendError(f"{field} is not a bounded text value")
    return text


def _absolute_path(value: str | Path, field: str) -> Path:
    if "\x00" in str(value):
        raise TrainingBackendError(f"{field} contains a NUL byte")
    path = Path(value)
    if not path.is_absolute():
        raise TrainingBackendError(f"{field} must be an absolute path")
    return path.resolve(strict=False)


def _require_within(path: Path, parent: Path, field: str) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:
        raise TrainingBackendError(f"{field} must stay within the pinned workspace") from exc


def _strict_json_object(blob: bytes, field: str) -> dict[str, object]:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-standard JSON constant {value!r}")

    def reject_duplicates(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError(f"duplicate JSON field {key!r}")
            parsed[key] = value
        return parsed

    try:
        value = json.loads(
            blob.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise TrainingBackendError(f"{field} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise TrainingBackendError(f"{field} must be an object")
    return value


def _is_linklike(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _checkpoint_file_sha256(path: Path) -> tuple[str, int]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise TrainingBackendError("checkpoint file could not be inspected") from exc
    if (
        _is_linklike(path)
        or not path.is_file()
        or before.st_nlink != 1
        or before.st_size < 0
        or before.st_size > _MAX_CHECKPOINT_FILE_BYTES
    ):
        raise TrainingBackendError("checkpoint contains an unsafe file")
    digest = hashlib.sha256()
    observed = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > _MAX_CHECKPOINT_FILE_BYTES:
                    raise TrainingBackendError(
                        "checkpoint file exceeds its storage boundary",
                    )
                digest.update(chunk)
        after = path.lstat()
    except OSError as exc:
        raise TrainingBackendError("checkpoint file could not be read") from exc
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or observed != after.st_size
        or _is_linklike(path)
    ):
        raise TrainingBackendError("checkpoint file changed during verification")
    return digest.hexdigest(), observed


def _checkpoint_commitments(
    checkpoint_dir: Path,
) -> tuple[dict[str, dict[str, object]], int]:
    try:
        root_info = checkpoint_dir.lstat()
    except OSError as exc:
        raise TrainingBackendError("resume checkpoint directory does not exist") from exc
    if _is_linklike(checkpoint_dir) or not checkpoint_dir.is_dir():
        raise TrainingBackendError("resume checkpoint path is not a safe directory")
    commitments: dict[str, dict[str, object]] = {}
    total_bytes = 0
    try:
        entries = sorted(
            checkpoint_dir.rglob("*"),
            key=lambda item: item.relative_to(checkpoint_dir).as_posix(),
        )
    except OSError as exc:
        raise TrainingBackendError("resume checkpoint could not be enumerated") from exc
    for path in entries:
        if _is_linklike(path):
            raise TrainingBackendError("checkpoint contains a link or junction")
        relative = path.relative_to(checkpoint_dir).as_posix()
        if (
            not relative
            or len(relative.encode("utf-8")) > 4_096
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise TrainingBackendError("checkpoint contains an unsafe relative path")
        if path.is_dir():
            continue
        if not path.is_file():
            raise TrainingBackendError("checkpoint contains an unsupported entry")
        if relative == TRAINING_CHECKPOINT_MANIFEST_BASENAME:
            continue
        if len(commitments) >= _MAX_CHECKPOINT_FILES:
            raise TrainingBackendError("checkpoint exceeds its file-count boundary")
        sha256, size = _checkpoint_file_sha256(path)
        total_bytes += size
        if total_bytes > _MAX_CHECKPOINT_BYTES:
            raise TrainingBackendError("checkpoint exceeds its storage boundary")
        commitments[relative] = {"sha256": sha256, "bytes": size}
    if not commitments:
        raise TrainingBackendError("resume checkpoint has no artifact files")
    try:
        after = checkpoint_dir.lstat()
    except OSError as exc:
        raise TrainingBackendError("resume checkpoint changed during verification") from exc
    if (
        (root_info.st_dev, root_info.st_ino) != (after.st_dev, after.st_ino)
        or _is_linklike(checkpoint_dir)
    ):
        raise TrainingBackendError("resume checkpoint changed during verification")
    return dict(sorted(commitments.items())), total_bytes


def build_training_checkpoint_manifest(spec: TrainingRunSpec) -> dict[str, object]:
    """Build the exact checkpoint observation an external runner must persist."""
    if not isinstance(spec, TrainingRunSpec) or not spec.verify():
        raise TrainingBackendError("checkpoint provenance requires a valid run spec")
    output_dir = _absolute_path(spec.output_dir, "checkpoint output directory")
    commitments, total_bytes = _checkpoint_commitments(output_dir)
    payload: dict[str, object] = {
        "schema": TRAINING_CHECKPOINT_MANIFEST_SCHEMA,
        "run_id": spec.run_id,
        "attempt": spec.attempt,
        "spec_digest": spec.digest,
        "files": commitments,
        "file_count": len(commitments),
        "total_bytes": total_bytes,
    }
    return {
        **payload,
        "checkpoint_sha256": _sha256(payload),
    }


def _verified_training_checkpoint(
    checkpoint_dir: Path,
    *,
    run_id: str,
    attempt: int,
    spec_digest: str,
    expected_checkpoint_sha256: str | None = None,
) -> str:
    manifest_path = checkpoint_dir / TRAINING_CHECKPOINT_MANIFEST_BASENAME
    try:
        manifest_before = manifest_path.lstat()
    except OSError as exc:
        raise TrainingBackendError(
            "resume checkpoint has no observed manifest",
        ) from exc
    if (
        _is_linklike(manifest_path)
        or not manifest_path.is_file()
        or manifest_before.st_nlink != 1
        or manifest_before.st_size <= 0
        or manifest_before.st_size > _MAX_CHECKPOINT_MANIFEST_BYTES
    ):
        raise TrainingBackendError(
            "resume checkpoint manifest exceeds its storage boundary",
        )
    try:
        blob = manifest_path.read_bytes()
        manifest_after = manifest_path.lstat()
    except OSError as exc:
        raise TrainingBackendError("resume checkpoint manifest is unreadable") from exc
    if (
        len(blob) != manifest_after.st_size
        or (
            manifest_before.st_dev,
            manifest_before.st_ino,
            manifest_before.st_size,
            manifest_before.st_mtime_ns,
        )
        != (
            manifest_after.st_dev,
            manifest_after.st_ino,
            manifest_after.st_size,
            manifest_after.st_mtime_ns,
        )
        or manifest_after.st_nlink != 1
        or _is_linklike(manifest_path)
    ):
        raise TrainingBackendError(
            "resume checkpoint manifest changed during verification",
        )
    manifest = _strict_json_object(blob, "resume checkpoint manifest")
    expected_fields = {
        "schema",
        "run_id",
        "attempt",
        "spec_digest",
        "files",
        "file_count",
        "total_bytes",
        "checkpoint_sha256",
    }
    if set(manifest) != expected_fields:
        raise TrainingBackendError(
            "resume checkpoint manifest has missing or unexpected fields",
        )
    expected_spec = _require_sha256(spec_digest, "checkpoint parent run spec")
    checkpoint_sha256 = _require_sha256(
        manifest.get("checkpoint_sha256"),
        "checkpoint manifest digest",
    )
    if (
        manifest.get("schema") != TRAINING_CHECKPOINT_MANIFEST_SCHEMA
        or manifest.get("run_id") != run_id
        or not isinstance(manifest.get("attempt"), int)
        or isinstance(manifest.get("attempt"), bool)
        or manifest.get("attempt") != attempt
        or manifest.get("spec_digest") != expected_spec
        or not isinstance(manifest.get("file_count"), int)
        or isinstance(manifest.get("file_count"), bool)
        or not isinstance(manifest.get("total_bytes"), int)
        or isinstance(manifest.get("total_bytes"), bool)
    ):
        raise TrainingBackendError(
            "resume checkpoint manifest belongs to another run attempt",
        )
    commitments, total_bytes = _checkpoint_commitments(checkpoint_dir)
    if (
        manifest.get("files") != commitments
        or manifest.get("file_count") != len(commitments)
        or manifest.get("total_bytes") != total_bytes
    ):
        raise TrainingBackendError("resume checkpoint bytes differ from its manifest")
    payload = {
        key: manifest[key]
        for key in (
            "schema",
            "run_id",
            "attempt",
            "spec_digest",
            "files",
            "file_count",
            "total_bytes",
        )
    }
    if _sha256(payload) != checkpoint_sha256:
        raise TrainingBackendError("resume checkpoint manifest digest is invalid")
    if (
        expected_checkpoint_sha256 is not None
        and checkpoint_sha256
        != _require_sha256(
            expected_checkpoint_sha256,
            "expected resume checkpoint",
        )
    ):
        raise TrainingBackendError(
            "resume checkpoint differs from the observed prior result",
        )
    return checkpoint_sha256


def _verify_requested_resume_checkpoint(
    checkpoint_dir: Path | None,
    *,
    root: Path,
    tenant_id: str,
    run_id: str,
    attempt: int,
    parent_spec_digest: str,
    checkpoint_sha256: str,
) -> None:
    if checkpoint_dir is None:
        return
    _require_within(checkpoint_dir, root, "resume checkpoint path")
    expected = (
        root
        / "maverick-runs"
        / training_tenant_namespace(tenant_id)
        / run_id
        / f"attempt-{attempt - 1}"
        / "outputs"
    ).resolve(strict=False)
    if checkpoint_dir != expected:
        raise TrainingBackendError(
            "resume checkpoint path is not the previous attempt output",
        )
    _verified_training_checkpoint(
        checkpoint_dir,
        run_id=run_id,
        attempt=attempt - 1,
        spec_digest=parent_spec_digest,
        expected_checkpoint_sha256=checkpoint_sha256,
    )


def _bounded_positive_int(value: object, field: str, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise TrainingBackendError(f"{field} must be an integer in 1..{maximum}")
    return value


def _bounded_positive_float(value: object, field: str, maximum: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0.0 < float(value) <= maximum
    ):
        raise TrainingBackendError(f"{field} must be finite and in (0, {maximum}]")
    return float(value)


def scrub_training_environment(
    inherited: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return a deterministic allowlist with forced offline/telemetry settings.

    Unknown names, including credentials and provider API keys, are dropped.
    The caller must pass an environment explicitly; this function never reads
    ``os.environ``.
    """

    admitted: dict[str, str] = {}
    for name, value in (inherited or {}).items():
        if name not in _PASSTHROUGH_ENV_NAMES:
            continue
        if not isinstance(value, str):
            raise TrainingBackendError(f"environment value {name} must be a string")
        if (
            len(value.encode("utf-8")) > 4_096
            or "\x00" in value
            or "\r" in value
            or "\n" in value
        ):
            raise TrainingBackendError(f"environment value {name} is not bounded")
        admitted[name] = value
    admitted.update(_OFFLINE_ENV)
    return tuple(sorted(admitted.items()))


def _bundle_training_environment(
    inherited: Mapping[str, str] | None,
    bundle_path: Path,
) -> tuple[tuple[str, str], ...]:
    """Bind only the verified bundle source into the trainer import path."""

    admitted = dict(scrub_training_environment(inherited))
    bundle_source = (bundle_path / "src").resolve(strict=False)
    _require_within(bundle_source, bundle_path, "environment bundle source")
    admitted[_BUNDLE_PYTHONPATH_NAME] = str(bundle_source)
    return tuple(sorted(admitted.items()))


def _configured_model_improvement() -> dict[str, object]:
    """Read the high-authority training controls without fail-soft fallback."""
    from ..config import (
        ModelImprovementConfigError,
        get_model_improvement_mutation_policy,
    )

    try:
        return dict(get_model_improvement_mutation_policy())
    except ModelImprovementConfigError as exc:
        raise TrainingBackendError(
            "model-improvement policy source is invalid",
        ) from exc


def admit_training_boundary(
    pack: EnvironmentPack,
    *,
    tenant_id: str,
    target: str,
    bundle_digest: str,
    hosted_boundary_permission: bool = False,
) -> BoundaryDecision:
    """Apply Maverick's boundary contract plus explicit anti-federation rules."""

    tenant = canonical_training_tenant_id(tenant_id)
    destination = normalize_training_target(target).value
    _require_sha256(bundle_digest, "environment bundle digest")
    tenant_ids = {case.tenant_id for case in pack.cases if case.tenant_id}
    if len(tenant_ids) > 1:
        raise TrainingBackendError("training pack contains more than one tenant")
    if tenant_ids and tenant_ids != {tenant}:
        raise TrainingBackendError("training pack tenant does not match the run tenant")
    if destination == "hosted" and hosted_boundary_permission is not True:
        raise TrainingBackendError("hosted training needs explicit boundary permission")
    if destination == "hosted":
        policy = _configured_model_improvement()
        if policy.get("enable") is not True or policy.get("allow_hosted") is not True:
            raise TrainingBackendError(
                "hosted training is disabled by [model_improvement] policy",
            )
    if destination != "hosted" and hosted_boundary_permission:
        raise TrainingBackendError(
            "hosted boundary permission cannot be reused for a non-hosted target",
        )
    try:
        decision = check_boundary(
            pack,
            destination,
        )
    except EnvironmentError as exc:
        raise TrainingBackendError(
            f"training boundary refused: {exc}",
        ) from exc
    if (
        decision.target != destination
        or decision.pack_digest != pack.digest
    ):
        raise TrainingBackendError("boundary decision is not bound to the requested pack")
    if not decision.allowed:
        raise TrainingBackendError(
            "training boundary refused: " + "; ".join(decision.reasons),
        )
    return decision


def training_admission_digest(
    decision: BoundaryDecision,
    *,
    tenant_id: str,
    bundle_digest: str,
) -> str:
    """Commit an authoritative boundary decision to tenant, bundle, and target."""

    tenant = canonical_training_tenant_id(tenant_id)
    bundle = _require_sha256(bundle_digest, "environment bundle digest")
    target = normalize_training_target(decision.target).value
    if decision.allowed is not True or decision.reasons:
        raise TrainingBackendError("only an allowed boundary decision may be committed")
    _require_sha256(decision.pack_digest, "boundary decision pack")
    if decision.registry_digest:
        _require_sha256(decision.registry_digest, "trusted evidence registry")
    if (
        not isinstance(decision.evaluated_at, (int, float))
        or isinstance(decision.evaluated_at, bool)
        or not math.isfinite(float(decision.evaluated_at))
        or decision.evaluated_at < 0
    ):
        raise TrainingBackendError("boundary decision evaluated_at is invalid")
    admitted_ids: set[str] = set()
    for case_id, digest in decision.admitted_content_digests:
        identifier = _bounded_text(case_id, "admitted case_id", 128)
        if identifier in admitted_ids:
            raise TrainingBackendError("boundary decision repeats an admitted case_id")
        admitted_ids.add(identifier)
        _require_sha256(digest, "admitted case content")
    if tuple(sorted(decision.admitted_content_digests)) != (
        decision.admitted_content_digests
    ):
        raise TrainingBackendError("boundary decision case commitments are not canonical")
    return _sha256({
        "schema": TRAINING_ADMISSION_SCHEMA,
        "tenant_id": tenant,
        "bundle_digest": bundle,
        "target": target,
        "boundary_decision_digest": _require_sha256(
            decision.digest,
            "boundary decision",
        ),
    })


@dataclass(frozen=True, slots=True)
class PrimeRLWorkspace:
    """Operator attestation for an already provisioned Prime RL workspace."""

    root: Path
    python_executable: Path
    revision: str
    verifiers_revision: str
    python_version: str
    operator_provisioned: bool = False
    nvidia_available: bool = False

    def validated(self) -> tuple[Path, Path]:
        """Validate declarations without probing, installing, or executing."""

        if self.operator_provisioned is not True:
            raise TrainingBackendError(
                "Prime RL needs an operator-provisioned workspace; auto-install is forbidden",
            )
        if self.nvidia_available is not True:
            raise TrainingBackendError(
                "Prime RL v0.7.0 requires an operator-confirmed NVIDIA workspace",
            )
        if not _PYTHON_312_RE.fullmatch(self.python_version):
            raise TrainingBackendError(
                "Prime RL v0.7.0 requires operator-provisioned Python 3.12",
            )
        if self.revision != PRIME_RL_COMMIT:
            raise TrainingBackendError(
                f"Prime RL workspace must be pinned to {PRIME_RL_COMMIT}",
            )
        if self.verifiers_revision != PRIME_RL_VERIFIERS_SUBMODULE_COMMIT:
            raise TrainingBackendError(
                "Prime RL Verifiers submodule does not match the v0.7.0 release",
            )
        root = _absolute_path(self.root, "workspace root")
        python = _absolute_path(self.python_executable, "workspace Python executable")
        _require_within(python, root, "workspace Python executable")
        return root, python


@runtime_checkable
class TrainingEnvironmentBundle(Protocol):
    """Structural contract supplied by a backend-specific environment adapter."""

    package_id: str
    tenant_id: str
    target: str
    hosted_boundary_permission: bool
    environment_id: str
    environment_version: str
    environment_digest: str
    purpose: str
    split: str
    boundary_decision_digest: str
    admission_digest: str
    verifiers_version: str
    verifiers_revision: str
    digest: str

    def verify(self) -> bool:
        """Verify the bundle's content commitment without executing it."""

    def verify_materialized(self, root: str | Path) -> bool:
        """Verify the exact on-disk handoff, including absence of extra files."""

    def verify_against(self, pack: EnvironmentPack) -> bool:
        """Verify that task rows are the exact selected cases from the pack."""

    def boundary_decision(self) -> BoundaryDecision:
        """Return the exact authoritative decision committed by the bundle."""


@dataclass(frozen=True, slots=True)
class TrainingRunSpec:
    """Content-free, deterministic plan for one external training attempt."""

    run_id: str
    attempt: int
    tenant_id: str
    backend: str
    backend_version: str
    backend_revision: str
    backend_tag: str
    backend_tag_object: str
    backend_dependency_revisions: tuple[tuple[str, str], ...]
    environment_id: str
    environment_version: str
    environment_digest: str
    environment_split: str
    environment_bundle_digest: str
    environment_package_id: str
    boundary_target: str
    boundary_decision_digest: str
    boundary_admission_digest: str
    hosted_boundary_permission: bool
    base_model_path: str
    base_model_revision: str
    base_model_sha256: str
    workspace_root: str
    environment_bundle_path: str
    output_dir: str
    config_path: str
    config_text: str
    argv: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...]
    parent_spec_digest: str = ""
    resume_from_checkpoint_sha256: str = ""
    resume_from_checkpoint_path: str = ""
    resume_step: int | None = None
    schema: str = TRAINING_RUN_SPEC_SCHEMA

    @property
    def shell(self) -> bool:
        """External runners must pass argv directly and keep shell disabled."""

        return False

    @property
    def config_sha256(self) -> str:
        return hashlib.sha256(self.config_text.encode("utf-8")).hexdigest()

    @property
    def immutable_semantics_digest(self) -> str:
        """Commit every run semantic that a resume is forbidden to change."""

        config = tomllib.loads(self.config_text)
        normalized_config = dict(config)
        normalized_config.pop("output_dir", None)
        normalized_config.pop("ckpt", None)
        normalized_argv = tuple(
            "<config-path>" if item == self.config_path else item
            for item in self.argv
        )
        return _sha256({
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "backend": {
                "name": self.backend,
                "version": self.backend_version,
                "revision": self.backend_revision,
                "tag": self.backend_tag,
                "tag_object": self.backend_tag_object,
                "dependency_revisions": dict(self.backend_dependency_revisions),
            },
            "environment": {
                "id": self.environment_id,
                "version": self.environment_version,
                "digest": self.environment_digest,
                "split": self.environment_split,
                "bundle_digest": self.environment_bundle_digest,
                "package_id": self.environment_package_id,
                "bundle_path": self.environment_bundle_path,
            },
            "boundary": {
                "target": self.boundary_target,
                "decision_digest": self.boundary_decision_digest,
                "admission_digest": self.boundary_admission_digest,
                "hosted_permission": self.hosted_boundary_permission,
            },
            "base_model": {
                "path": self.base_model_path,
                "revision": self.base_model_revision,
                "sha256": self.base_model_sha256,
            },
            "workspace_root": self.workspace_root,
            "cwd": self.cwd,
            "argv": normalized_argv,
            "environment_variables": dict(self.environment),
            "config": normalized_config,
        })

    def public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "tenant_id": self.tenant_id,
            "backend": {
                "name": self.backend,
                "version": self.backend_version,
                "revision": self.backend_revision,
                "tag": self.backend_tag,
                "tag_object": self.backend_tag_object,
                "dependency_revisions": dict(self.backend_dependency_revisions),
            },
            "environment": {
                "id": self.environment_id,
                "version": self.environment_version,
                "digest": self.environment_digest,
                "split": self.environment_split,
                "bundle_digest": self.environment_bundle_digest,
                "package_id": self.environment_package_id,
            },
            "boundary": {
                "target": self.boundary_target,
                "decision_digest": self.boundary_decision_digest,
                "admission_digest": self.boundary_admission_digest,
                "hosted_permission": self.hosted_boundary_permission,
                "cross_tenant": False,
                "upload_enabled": False,
                "telemetry_enabled": False,
                "network_policy": "operator-enforced-deny",
            },
            "base_model": {
                "local_path": self.base_model_path,
                "revision": self.base_model_revision,
                "artifact_sha256": self.base_model_sha256,
            },
            "workspace": {
                "root": self.workspace_root,
                "environment_bundle_path": self.environment_bundle_path,
                "python": self.argv[0],
                "python_version": "3.12",
                "nvidia_required": True,
                "operator_provisioned": True,
            },
            "output_dir": self.output_dir,
            "config_path": self.config_path,
            "config_sha256": self.config_sha256,
            "argv": list(self.argv),
            "shell": False,
            "cwd": self.cwd,
            "environment_variables": dict(self.environment),
            "resume": {
                "parent_spec_digest": self.parent_spec_digest,
                "checkpoint_sha256": self.resume_from_checkpoint_sha256,
                "checkpoint_path": self.resume_from_checkpoint_path,
                "step": self.resume_step,
            },
            "immutable_semantics_digest": self.immutable_semantics_digest,
        }

    def _verify_identity(self) -> bool:
        if (
            self.schema != TRAINING_RUN_SPEC_SCHEMA
            or not _RUN_ID_RE.fullmatch(self.run_id)
            or self.attempt < 1
            or self.environment_split != "train"
            or canonical_training_tenant_id(self.tenant_id) != self.tenant_id
            or not _PACKAGE_ID_RE.fullmatch(self.environment_package_id)
        ):
            return False
        _require_revision(self.backend_revision, "backend revision")
        _require_sha256(self.environment_digest, "environment digest")
        _require_sha256(self.environment_bundle_digest, "environment bundle digest")
        _require_sha256(self.boundary_decision_digest, "boundary decision")
        _require_sha256(self.boundary_admission_digest, "boundary admission")
        _require_revision(self.base_model_revision, "base model revision")
        _require_sha256(self.base_model_sha256, "base model artifact")
        environment = dict(self.environment)
        bundle_pythonpath = environment.pop(_BUNDLE_PYTHONPATH_NAME, None)
        return (
            len(dict(self.environment)) == len(self.environment)
            and tuple(sorted(self.environment)) == self.environment
            and isinstance(bundle_pythonpath, str)
            and bool(bundle_pythonpath)
            and scrub_training_environment(environment)
            == tuple(sorted(environment.items()))
        )

    def _verified_paths(self) -> tuple[Path, Path, Path]:
        root = _absolute_path(self.workspace_root, "workspace root")
        python = _absolute_path(self.argv[0], "workspace Python executable")
        bundle_path = _absolute_path(
            self.environment_bundle_path,
            "environment bundle path",
        )
        output_dir = _absolute_path(self.output_dir, "output_dir")
        config_path = _absolute_path(self.config_path, "config_path")
        _absolute_path(self.base_model_path, "base model path")
        for path, field in (
            (python, "workspace Python executable"),
            (bundle_path, "environment bundle path"),
            (output_dir, "output_dir"),
            (config_path, "config_path"),
        ):
            _require_within(path, root, field)
        namespace = training_tenant_namespace(self.tenant_id)
        expected_bundle = (
            root
            / "maverick-bundles"
            / namespace
            / self.environment_package_id
        ).resolve(strict=False)
        expected_run = (
            root
            / "maverick-runs"
            / namespace
            / self.run_id
            / f"attempt-{self.attempt}"
        ).resolve(strict=False)
        if (
            bundle_path != expected_bundle
            or output_dir != expected_run / "outputs"
            or config_path != expected_run / "rl.toml"
            or dict(self.environment).get(_BUNDLE_PYTHONPATH_NAME)
            != str((expected_bundle / "src").resolve(strict=False))
        ):
            raise TrainingBackendError(
                "training paths do not match the tenant-scoped layout",
            )
        return root, python, config_path

    def _verify_boundary_and_resume(self, root: Path) -> bool:
        if normalize_training_target(self.boundary_target) is TrainingTarget.HOSTED:
            if self.hosted_boundary_permission is not True:
                return False
        elif self.hosted_boundary_permission:
            return False
        if self.attempt == 1:
            return not (
                self.parent_spec_digest
                or
                self.resume_from_checkpoint_sha256
                or self.resume_from_checkpoint_path
                or self.resume_step is not None
            )
        _require_sha256(self.parent_spec_digest, "parent run spec")
        _require_sha256(self.resume_from_checkpoint_sha256, "resume checkpoint")
        resume_path = _absolute_path(
            self.resume_from_checkpoint_path,
            "resume checkpoint path",
        )
        _require_within(resume_path, root, "resume checkpoint path")
        expected_resume_path = (
            root
            / "maverick-runs"
            / training_tenant_namespace(self.tenant_id)
            / self.run_id
            / f"attempt-{self.attempt - 1}"
            / "outputs"
        ).resolve(strict=False)
        return (
            resume_path == expected_resume_path
            and
            isinstance(self.resume_step, int)
            and not isinstance(self.resume_step, bool)
            and self.resume_step >= -1
        )

    def _verify_prime_config(self, python: Path, config_path: Path) -> bool:
        if (
            self.backend_version != PRIME_RL_VERSION
            or self.backend_revision != PRIME_RL_COMMIT
            or self.backend_tag != PRIME_RL_TAG
            or self.backend_tag_object != PRIME_RL_TAG_OBJECT
            or dict(self.backend_dependency_revisions)
            != {"verifiers": PRIME_RL_VERIFIERS_SUBMODULE_COMMIT}
            or self.argv
            != (
                str(python),
                "-P",
                "-m",
                "prime_rl.entrypoints.rl",
                "@",
                str(config_path),
            )
        ):
            return False
        config = tomllib.loads(self.config_text)
        expected_top_level = {
            "output_dir",
            "max_steps",
            "seq_len",
            "clean_output_dir",
            "dry_run",
            "env_vars",
            "deployment",
            "weight_broadcast",
            "model",
            "trainer",
            "orchestrator",
            "inference",
        }
        if self.attempt > 1:
            expected_top_level.add("ckpt")
        model = config.get("model")
        if (
            set(config) != expected_top_level
            or config.get("output_dir") != self.output_dir
            or config.get("clean_output_dir") is not False
            or config.get("dry_run") is not False
            or config.get("env_vars") != dict(self.environment)
            or not isinstance(model, dict)
            or set(model) != {"name"}
            or model.get("name") != self.base_model_path
            or config.get("inference") != {}
            or config.get("weight_broadcast") != {"type": "filesystem"}
        ):
            return False
        _bounded_positive_int(config.get("max_steps"), "max_steps", 10_000_000)
        sequence_length = _bounded_positive_int(
            config.get("seq_len"),
            "sequence_length",
            1_048_576,
        )
        deployment = config.get("deployment")
        if not isinstance(deployment, dict) or set(deployment) != {
            "type",
            "gpus_per_node",
            "num_train_gpus",
            "num_infer_gpus",
        }:
            return False
        train_gpus = _bounded_positive_int(
            deployment.get("num_train_gpus"),
            "num_train_gpus",
            16_384,
        )
        inference_gpus = _bounded_positive_int(
            deployment.get("num_infer_gpus"),
            "num_infer_gpus",
            16_384,
        )
        if (
            deployment.get("type") != "single_node"
            or deployment.get("gpus_per_node") != train_gpus + inference_gpus
        ):
            return False
        trainer = config.get("trainer")
        if (
            not isinstance(trainer, dict)
            or set(trainer) != {"optim"}
            or not isinstance(trainer.get("optim"), dict)
            or set(trainer["optim"]) != {"lr"}
        ):
            return False
        _bounded_positive_float(trainer["optim"].get("lr"), "learning_rate", 1.0)
        orchestrator = config.get("orchestrator")
        if (
            not isinstance(orchestrator, dict)
            or set(orchestrator)
            != {
                "batch_size",
                "group_size",
                "collect_inference_metrics",
                "algo",
                "train",
                "renderer",
            }
            or orchestrator.get("collect_inference_metrics") is not False
            or orchestrator.get("algo") != {"type": "grpo"}
            or orchestrator.get("renderer") != {"name": "default"}
        ):
            return False
        batch_size = _bounded_positive_int(
            orchestrator.get("batch_size"),
            "batch_size",
            1_000_000,
        )
        group_size = _bounded_positive_int(
            orchestrator.get("group_size"),
            "group_size",
            1_000_000,
        )
        if batch_size % group_size:
            return False
        train = orchestrator.get("train")
        if (
            not isinstance(train, dict)
            or set(train) != {"sampling", "env"}
            or not isinstance(train.get("sampling"), dict)
            or set(train["sampling"]) != {"max_completion_tokens"}
            or train.get("env")
            != [{"taskset": {"id": self.environment_package_id}}]
        ):
            return False
        _bounded_positive_int(
            train["sampling"].get("max_completion_tokens"),
            "completion_tokens",
            sequence_length,
        )
        checkpoint = config.get("ckpt")
        if self.attempt == 1:
            return checkpoint is None
        return (
            isinstance(checkpoint, dict)
            and set(checkpoint) == {"output_dir", "resume_step"}
            and checkpoint.get("output_dir") == self.resume_from_checkpoint_path
            and checkpoint.get("resume_step") == self.resume_step
        )

    def verify(self) -> bool:
        """Validate the inert plan and its Prime-specific offline invariants."""

        try:
            if not self._verify_identity():
                return False
            root, python, config_path = self._verified_paths()
            if self.cwd != str(root) or not self._verify_boundary_and_resume(root):
                return False
            if self.backend == PRIME_RL_BACKEND:
                return self._verify_prime_config(python, config_path)
            return True
        except (
            AttributeError,
            IndexError,
            KeyError,
            OSError,
            TrainingBackendError,
            TypeError,
            ValueError,
        ):
            return False

    @property
    def digest(self) -> str:
        return _sha256(self.public_dict())


@dataclass(frozen=True, slots=True)
class TrainingRunResult:
    """Observed lifecycle state, bound to one exact run-spec digest."""

    run_id: str
    attempt: int
    state: TrainingRunState
    spec_digest: str
    checkpoint_sha256: str = ""
    output_artifact_sha256: str = ""
    detail: str = ""
    schema: str = TRAINING_RUN_RESULT_SCHEMA

    def public_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "state": self.state.value,
            "spec_digest": self.spec_digest,
            "checkpoint_sha256": self.checkpoint_sha256,
            "output_artifact_sha256": self.output_artifact_sha256,
            "detail": self.detail,
        }

    @property
    def digest(self) -> str:
        return _sha256(self.public_dict())


@runtime_checkable
class TrainingBackend(Protocol):
    """Lifecycle interface implemented independently of any trainer library."""

    def prepare(
        self,
        spec: TrainingRunSpec,
        *,
        pack: EnvironmentPack,
        bundle: TrainingEnvironmentBundle,
    ) -> TrainingRunResult:
        """Admit a non-executed run spec."""

    def status(self, run_id: str) -> TrainingRunResult:
        """Return the latest observed state."""

    def cancel(self, run_id: str) -> TrainingRunResult:
        """Cancel a prepared/running attempt or return an idempotent result."""

    def resume(
        self,
        spec: TrainingRunSpec,
        *,
        pack: EnvironmentPack,
        bundle: TrainingEnvironmentBundle,
    ) -> TrainingRunResult:
        """Prepare the next attempt from a bound checkpoint."""


@dataclass(slots=True)
class _RunRecord:
    spec: TrainingRunSpec
    result: TrainingRunResult


class NonExecutingTrainingBackend:
    """Process-local lifecycle semantics for externally executed run specs.

    This class intentionally cannot mark work successful: an external runner
    and the governed receipt subsystem remain authoritative for execution and
    results.  It provides deterministic prepare/status/cancel/resume behavior
    without pretending that generating an argv launched anything.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        name: str,
        version: str,
        revision: str,
    ) -> None:
        self.tenant_id = canonical_training_tenant_id(tenant_id)
        self.name = _bounded_text(name, "backend name", 128)
        self.version = _bounded_text(version, "backend version", 128)
        self.revision = _require_revision(revision, "backend revision")
        self._records: dict[str, _RunRecord] = {}
        self._lock = threading.RLock()

    def _validate_backend(self, spec: TrainingRunSpec) -> None:
        if canonical_training_tenant_id(spec.tenant_id) != self.tenant_id:
            raise TrainingBackendError("run spec belongs to another tenant")
        if (
            spec.backend != self.name
            or spec.backend_version != self.version
            or spec.backend_revision != self.revision
        ):
            raise TrainingBackendError("run spec does not match this backend pin")

    def _revalidate_admission(
        self,
        spec: TrainingRunSpec,
        *,
        pack: EnvironmentPack,
        bundle: TrainingEnvironmentBundle,
    ) -> None:
        require_training_readiness(pack)
        if (
            bundle.tenant_id != self.tenant_id
            or bundle.environment_id != spec.environment_id
            or bundle.environment_version != spec.environment_version
            or bundle.environment_digest != spec.environment_digest
            or bundle.split != "train"
            or bundle.purpose != "training"
            or bundle.digest != spec.environment_bundle_digest
            or bundle.package_id != spec.environment_package_id
            or bundle.target != spec.boundary_target
            or bundle.hosted_boundary_permission != spec.hosted_boundary_permission
        ):
            raise TrainingBackendError(
                "environment bundle does not match the tenant-scoped run spec",
            )
        if not bundle.verify() or not bundle.verify_against(pack):
            raise TrainingBackendError("environment bundle integrity verification failed")
        if not bundle.verify_materialized(spec.environment_bundle_path):
            raise TrainingBackendError("materialized environment bundle verification failed")
        decision = admit_training_boundary(
            pack,
            tenant_id=self.tenant_id,
            target=spec.boundary_target,
            bundle_digest=bundle.digest,
            hosted_boundary_permission=spec.hosted_boundary_permission,
        )
        recorded = bundle.boundary_decision()
        recorded_admission_digest = training_admission_digest(
            recorded,
            tenant_id=self.tenant_id,
            bundle_digest=bundle.digest,
        )
        if (
            recorded.digest != bundle.boundary_decision_digest
            or recorded.digest != spec.boundary_decision_digest
            or recorded_admission_digest != bundle.admission_digest
            or recorded_admission_digest != spec.boundary_admission_digest
            or decision.target != recorded.target
            or decision.pack_digest != recorded.pack_digest
            or decision.registry_digest != recorded.registry_digest
            or (
                decision.admitted_content_digests
                != recorded.admitted_content_digests
            )
            or decision.evaluated_at < recorded.evaluated_at
        ):
            raise TrainingBackendError(
                "boundary admission decision is stale or belongs to another bundle",
            )

    def prepare(
        self,
        spec: TrainingRunSpec,
        *,
        pack: EnvironmentPack,
        bundle: TrainingEnvironmentBundle,
    ) -> TrainingRunResult:
        self._validate_backend(spec)
        if not spec.verify():
            raise TrainingBackendError("run spec integrity verification failed")
        self._revalidate_admission(
            spec,
            pack=pack,
            bundle=bundle,
        )
        if spec.attempt != 1 or spec.resume_from_checkpoint_sha256:
            raise TrainingBackendError("initial prepare must be attempt 1 without a checkpoint")
        with self._lock:
            if spec.run_id in self._records:
                raise TrainingBackendError(f"run {spec.run_id!r} is already prepared")
            result = TrainingRunResult(
                run_id=spec.run_id,
                attempt=spec.attempt,
                state=TrainingRunState.PREPARED,
                spec_digest=spec.digest,
                detail="spec prepared; no process was started",
            )
            self._records[spec.run_id] = _RunRecord(spec=spec, result=result)
            return result

    def status(self, run_id: str) -> TrainingRunResult:
        identifier = _bounded_text(run_id, "run_id", 128)
        with self._lock:
            try:
                return self._records[identifier].result
            except KeyError as exc:
                raise TrainingBackendError(f"unknown training run {identifier!r}") from exc

    def cancel(self, run_id: str) -> TrainingRunResult:
        identifier = _bounded_text(run_id, "run_id", 128)
        with self._lock:
            try:
                record = self._records[identifier]
            except KeyError as exc:
                raise TrainingBackendError(f"unknown training run {identifier!r}") from exc
            if record.result.state == TrainingRunState.CANCELLED:
                return record.result
            if record.result.state in {
                TrainingRunState.SUCCEEDED,
                TrainingRunState.FAILED,
            }:
                raise TrainingBackendError(
                    f"cannot cancel terminal run in state {record.result.state.value}",
                )
            checkpoint_sha256 = record.result.checkpoint_sha256
            detail = "external execution must remain stopped"
            manifest_path = (
                Path(record.spec.output_dir)
                / TRAINING_CHECKPOINT_MANIFEST_BASENAME
            )
            if not checkpoint_sha256 and manifest_path.exists():
                try:
                    checkpoint_sha256 = _verified_training_checkpoint(
                        Path(record.spec.output_dir),
                        run_id=record.spec.run_id,
                        attempt=record.spec.attempt,
                        spec_digest=record.spec.digest,
                    )
                except TrainingBackendError:
                    detail += "; checkpoint observation failed, resume disabled"
            result = TrainingRunResult(
                run_id=identifier,
                attempt=record.result.attempt,
                state=TrainingRunState.CANCELLED,
                spec_digest=record.spec.digest,
                checkpoint_sha256=checkpoint_sha256,
                detail=detail,
            )
            record.result = result
            return result

    def resume(
        self,
        spec: TrainingRunSpec,
        *,
        pack: EnvironmentPack,
        bundle: TrainingEnvironmentBundle,
    ) -> TrainingRunResult:
        self._validate_backend(spec)
        checkpoint = _require_sha256(
            spec.resume_from_checkpoint_sha256,
            "resume checkpoint",
        )
        if not spec.verify():
            raise TrainingBackendError("run spec integrity verification failed")
        self._revalidate_admission(
            spec,
            pack=pack,
            bundle=bundle,
        )
        with self._lock:
            try:
                previous = self._records[spec.run_id]
            except KeyError as exc:
                raise TrainingBackendError(
                    f"unknown training run {spec.run_id!r}",
                ) from exc
            if previous.result.state not in {
                TrainingRunState.CANCELLED,
                TrainingRunState.FAILED,
            }:
                raise TrainingBackendError(
                    "resume is allowed only after cancellation or failure",
                )
            if spec.attempt != previous.result.attempt + 1:
                raise TrainingBackendError("resume attempt must increment exactly once")
            if spec.parent_spec_digest != previous.spec.digest:
                raise TrainingBackendError(
                    "resume parent_spec_digest does not match the previous attempt",
                )
            if spec.resume_from_checkpoint_path != previous.spec.output_dir:
                raise TrainingBackendError(
                    "resume checkpoint path is not the previous attempt output",
                )
            if not previous.result.checkpoint_sha256:
                raise TrainingBackendError(
                    "previous attempt has no observed checkpoint",
                )
            if checkpoint != previous.result.checkpoint_sha256:
                raise TrainingBackendError(
                    "resume checkpoint differs from the observed prior result",
                )
            if (
                previous.spec.immutable_semantics_digest
                != spec.immutable_semantics_digest
            ):
                raise TrainingBackendError("resume changed immutable training identity")
            _verified_training_checkpoint(
                Path(previous.spec.output_dir),
                run_id=previous.spec.run_id,
                attempt=previous.spec.attempt,
                spec_digest=previous.spec.digest,
                expected_checkpoint_sha256=checkpoint,
            )
            result = TrainingRunResult(
                run_id=spec.run_id,
                attempt=spec.attempt,
                state=TrainingRunState.PREPARED,
                spec_digest=spec.digest,
                checkpoint_sha256=checkpoint,
                detail="resume spec prepared; no process was started",
            )
            self._records[spec.run_id] = _RunRecord(spec=spec, result=result)
            return result


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _prime_rl_config(
    *,
    output_dir: Path,
    model_path: Path,
    package_id: str,
    environment: tuple[tuple[str, str], ...],
    max_steps: int,
    sequence_length: int,
    learning_rate: float,
    batch_size: int,
    group_size: int,
    completion_tokens: int,
    train_gpus: int,
    inference_gpus: int,
    resume_step: int | None,
    resume_checkpoint_path: Path | None,
) -> str:
    lines = [
        f"output_dir = {_toml_string(str(output_dir))}",
        f"max_steps = {max_steps}",
        f"seq_len = {sequence_length}",
        "clean_output_dir = false",
        "dry_run = false",
        "",
        "[env_vars]",
    ]
    lines.extend(f"{name} = {_toml_string(value)}" for name, value in environment)
    lines.extend([
        "",
        "[deployment]",
        'type = "single_node"',
        f"gpus_per_node = {train_gpus + inference_gpus}",
        f"num_train_gpus = {train_gpus}",
        f"num_infer_gpus = {inference_gpus}",
        "",
        "[weight_broadcast]",
        'type = "filesystem"',
        "",
        "[model]",
        f"name = {_toml_string(str(model_path))}",
    ])
    if resume_step is not None:
        assert resume_checkpoint_path is not None
        lines.extend([
            "",
            "[ckpt]",
            f"output_dir = {_toml_string(str(resume_checkpoint_path))}",
            f"resume_step = {resume_step}",
        ])
    lines.extend([
        "",
        "[trainer.optim]",
        f"lr = {learning_rate:.12g}",
        "",
        "[orchestrator]",
        f"batch_size = {batch_size}",
        f"group_size = {group_size}",
        "collect_inference_metrics = false",
        "",
        "[orchestrator.algo]",
        'type = "grpo"',
        "",
        "[orchestrator.train.sampling]",
        f"max_completion_tokens = {completion_tokens}",
        "",
        "[[orchestrator.train.env]]",
        f"taskset = {{ id = {_toml_string(package_id)} }}",
        "",
        "[inference]",
        "",
        "[orchestrator.renderer]",
        'name = "default"',
        "",
    ])
    return "\n".join(lines)


def generate_prime_rl_run_spec(
    pack: EnvironmentPack,
    bundle: TrainingEnvironmentBundle,
    workspace: PrimeRLWorkspace,
    *,
    run_id: str,
    tenant_id: str,
    target: str,
    hosted_boundary_permission: bool,
    base_model_path: str | Path,
    base_model_revision: str,
    base_model_sha256: str,
    environment_bundle_path: str | Path,
    attempt: int = 1,
    parent_spec_digest: str = "",
    resume_from_checkpoint_sha256: str = "",
    resume_from_checkpoint_path: str | Path | None = None,
    resume_step: int = -1,
    inherited_environment: Mapping[str, str] | None = None,
    max_steps: int = 100,
    sequence_length: int = 4_096,
    learning_rate: float = 0.000003,
    batch_size: int = 128,
    group_size: int = 16,
    completion_tokens: int = 512,
    train_gpus: int = 1,
    inference_gpus: int = 1,
) -> TrainingRunSpec:
    """Generate an exact, offline Prime RL v0.7.0 argv and config.

    The returned value is inert.  It contains no subprocess runner and performs
    no filesystem or network mutation.
    """

    identifier = _bounded_text(run_id, "run_id", 128)
    if not _RUN_ID_RE.fullmatch(identifier):
        raise TrainingBackendError("run_id is not a filesystem-safe identifier")
    tenant = canonical_training_tenant_id(tenant_id)
    destination = normalize_training_target(target).value
    if bundle.tenant_id != tenant:
        raise TrainingBackendError("environment bundle belongs to another tenant")
    if not bundle.verify() or not bundle.verify_against(pack):
        raise TrainingBackendError("environment bundle integrity verification failed")
    if (
        bundle.environment_id != pack.environment_id
        or bundle.environment_version != pack.version
        or bundle.environment_digest != pack.digest
    ):
        raise TrainingBackendError("environment bundle is not bound to this pack")
    if bundle.target != destination:
        raise TrainingBackendError("environment bundle target differs from the run target")
    if bundle.hosted_boundary_permission != hosted_boundary_permission:
        raise TrainingBackendError("environment bundle hosted permission differs from the run")
    if bundle.purpose != "training" or bundle.split != "train":
        raise TrainingBackendError(
            "Prime RL trainer specs require a training bundle with split='train'",
        )
    require_training_readiness(pack)
    if not pack.split("train"):
        raise TrainingBackendError("environment training split is empty")
    if (
        bundle.verifiers_version != PRIME_RL_VERIFIERS_VERSION
        or bundle.verifiers_revision != PRIME_RL_VERIFIERS_SUBMODULE_COMMIT
    ):
        raise TrainingBackendError(
            "Prime RL v0.7.0 requires its embedded Verifiers v0.2.0 submodule; "
            "standalone Verifiers v0.2.1 bundles are not compatible",
        )
    package_id = _bounded_text(bundle.package_id, "environment package_id", 128)
    if not _PACKAGE_ID_RE.fullmatch(package_id):
        raise TrainingBackendError("environment package_id is not a safe package name")
    bundle_digest = _require_sha256(bundle.digest, "environment bundle digest")
    boundary = admit_training_boundary(
        pack,
        tenant_id=tenant,
        target=destination,
        bundle_digest=bundle_digest,
        hosted_boundary_permission=hosted_boundary_permission,
    )
    recorded_boundary = bundle.boundary_decision()
    admission_digest = training_admission_digest(
        recorded_boundary,
        tenant_id=tenant,
        bundle_digest=bundle_digest,
    )
    if (
        bundle.boundary_decision_digest != recorded_boundary.digest
        or bundle.admission_digest != admission_digest
        or boundary.target != recorded_boundary.target
        or boundary.pack_digest != recorded_boundary.pack_digest
        or boundary.registry_digest != recorded_boundary.registry_digest
        or (
            boundary.admitted_content_digests
            != recorded_boundary.admitted_content_digests
        )
        or boundary.evaluated_at < recorded_boundary.evaluated_at
    ):
        raise TrainingBackendError(
            "environment bundle admission decision is stale or mismatched",
        )

    attempt_number = _bounded_positive_int(attempt, "attempt", 10_000)
    if attempt_number == 1:
        if (
            parent_spec_digest
            or resume_from_checkpoint_sha256
            or resume_from_checkpoint_path is not None
        ):
            raise TrainingBackendError(
                "attempt 1 cannot have a parent or resume from a checkpoint",
            )
        resolved_parent_digest = ""
        resolved_resume_sha256 = ""
        resolved_resume_path: Path | None = None
        resolved_resume_step = None
    else:
        resolved_parent_digest = _require_sha256(
            parent_spec_digest,
            "parent run spec",
        )
        resolved_resume_sha256 = _require_sha256(
            resume_from_checkpoint_sha256,
            "resume checkpoint",
        )
        if (
            not isinstance(resume_step, int)
            or isinstance(resume_step, bool)
            or resume_step < -1
        ):
            raise TrainingBackendError("resume_step must be -1 or a non-negative integer")
        if resume_from_checkpoint_path is None:
            raise TrainingBackendError("resume checkpoint path is required")
        resolved_resume_path = _absolute_path(
            resume_from_checkpoint_path,
            "resume checkpoint path",
        )
        resolved_resume_step = resume_step

    root, python = workspace.validated()
    bundle_path = _absolute_path(environment_bundle_path, "environment bundle path")
    _require_within(bundle_path, root, "environment bundle path")
    namespace = training_tenant_namespace(tenant)
    expected_bundle_path = (
        root / "maverick-bundles" / namespace / package_id
    ).resolve(strict=False)
    if bundle_path != expected_bundle_path:
        raise TrainingBackendError(
            "environment bundle path does not match its tenant-scoped package path",
        )
    if not bundle.verify_materialized(bundle_path):
        raise TrainingBackendError("materialized environment bundle verification failed")
    model_path = _absolute_path(base_model_path, "base model path")
    model_revision = _require_revision(base_model_revision, "base model revision")
    model_sha256 = _require_sha256(base_model_sha256, "base model artifact")

    steps = _bounded_positive_int(max_steps, "max_steps", 10_000_000)
    seq_len = _bounded_positive_int(sequence_length, "sequence_length", 1_048_576)
    lr = _bounded_positive_float(learning_rate, "learning_rate", 1.0)
    batch = _bounded_positive_int(batch_size, "batch_size", 1_000_000)
    group = _bounded_positive_int(group_size, "group_size", 1_000_000)
    if batch % group:
        raise TrainingBackendError("batch_size must be divisible by group_size")
    completion = _bounded_positive_int(
        completion_tokens,
        "completion_tokens",
        seq_len,
    )
    training_gpus = _bounded_positive_int(train_gpus, "train_gpus", 16_384)
    serving_gpus = _bounded_positive_int(inference_gpus, "inference_gpus", 16_384)

    run_root = (
        root
        / "maverick-runs"
        / namespace
        / identifier
        / f"attempt-{attempt_number}"
    )
    _verify_requested_resume_checkpoint(
        resolved_resume_path,
        root=root,
        tenant_id=tenant,
        run_id=identifier,
        attempt=attempt_number,
        parent_spec_digest=resolved_parent_digest,
        checkpoint_sha256=resolved_resume_sha256,
    )
    config_path = run_root / "rl.toml"
    output_dir = run_root / "outputs"
    environment = _bundle_training_environment(
        inherited_environment,
        bundle_path,
    )
    config_text = _prime_rl_config(
        output_dir=output_dir,
        model_path=model_path,
        package_id=package_id,
        environment=environment,
        max_steps=steps,
        sequence_length=seq_len,
        learning_rate=lr,
        batch_size=batch,
        group_size=group,
        completion_tokens=completion,
        train_gpus=training_gpus,
        inference_gpus=serving_gpus,
        resume_step=resolved_resume_step,
        resume_checkpoint_path=resolved_resume_path,
    )
    argv = (
        str(python),
        "-P",
        "-m",
        "prime_rl.entrypoints.rl",
        "@",
        str(config_path),
    )
    return TrainingRunSpec(
        run_id=identifier,
        attempt=attempt_number,
        tenant_id=tenant,
        backend=PRIME_RL_BACKEND,
        backend_version=PRIME_RL_VERSION,
        backend_revision=PRIME_RL_COMMIT,
        backend_tag=PRIME_RL_TAG,
        backend_tag_object=PRIME_RL_TAG_OBJECT,
        backend_dependency_revisions=(
            ("verifiers", PRIME_RL_VERIFIERS_SUBMODULE_COMMIT),
        ),
        environment_id=pack.environment_id,
        environment_version=pack.version,
        environment_digest=pack.digest,
        environment_split=bundle.split,
        environment_bundle_digest=bundle_digest,
        environment_package_id=package_id,
        boundary_target=recorded_boundary.target,
        boundary_decision_digest=recorded_boundary.digest,
        boundary_admission_digest=admission_digest,
        hosted_boundary_permission=hosted_boundary_permission,
        base_model_path=str(model_path),
        base_model_revision=model_revision,
        base_model_sha256=model_sha256,
        workspace_root=str(root),
        environment_bundle_path=str(bundle_path),
        output_dir=str(output_dir),
        config_path=str(config_path),
        config_text=config_text,
        argv=argv,
        cwd=str(root),
        environment=environment,
        parent_spec_digest=resolved_parent_digest,
        resume_from_checkpoint_sha256=resolved_resume_sha256,
        resume_from_checkpoint_path=(
            str(resolved_resume_path) if resolved_resume_path is not None else ""
        ),
        resume_step=resolved_resume_step,
    )


__all__ = [
    "NonExecutingTrainingBackend",
    "PRIME_RL_BACKEND",
    "PRIME_RL_COMMIT",
    "PRIME_RL_SOURCE",
    "PRIME_RL_TAG",
    "PRIME_RL_TAG_OBJECT",
    "PRIME_RL_VERIFIERS_SUBMODULE_COMMIT",
    "PRIME_RL_VERIFIERS_TAG",
    "PRIME_RL_VERIFIERS_VERSION",
    "PRIME_RL_VERSION",
    "PrimeRLWorkspace",
    "TRAINING_ADMISSION_SCHEMA",
    "TRAINING_CHECKPOINT_MANIFEST_BASENAME",
    "TRAINING_CHECKPOINT_MANIFEST_SCHEMA",
    "TRAINING_RUN_RESULT_SCHEMA",
    "TRAINING_RUN_SPEC_SCHEMA",
    "TrainingBackend",
    "TrainingBackendError",
    "TrainingEnvironmentBundle",
    "TrainingRunResult",
    "TrainingRunSpec",
    "TrainingRunState",
    "TrainingTarget",
    "admit_training_boundary",
    "build_training_checkpoint_manifest",
    "canonical_training_tenant_id",
    "generate_prime_rl_run_spec",
    "normalize_training_target",
    "scrub_training_environment",
    "training_admission_digest",
    "training_tenant_namespace",
]
