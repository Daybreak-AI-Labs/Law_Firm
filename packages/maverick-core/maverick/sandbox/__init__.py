"""The two retained execution backends: local subprocesses and Docker.

Callers go through :func:`build_sandbox` so ``[sandbox]`` configuration and
the require-container policy are enforced in one place. The firm runtime does
not discover third-party backends or silently downgrade an unsupported backend
to host execution.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from ..sandbox_names import BUILTIN_SANDBOX_BACKENDS
from .docker import DockerBackend
from .local import ExecResult, LocalBackend
from .sdk import SDK_VERSION, SandboxV2

__all__ = [
    "BUILTIN_SANDBOX_BACKENDS",
    "LocalBackend",
    "DockerBackend",
    "ExecResult",
    "build_sandbox",
    "fs_is_host_visible",
    "container_backend_required",
    "SandboxPolicyError",
    "SDK_VERSION",
    "SandboxV2",
]

log = logging.getLogger(__name__)

Sandbox = LocalBackend | DockerBackend


# Operator-provisioned Docker image defaults used by the offline evaluator.
_DEFAULT_IMAGE = "python:3.12-slim"
_IMAGE_BY_LANGUAGE = {
    "python": "python:3.12-slim",
    "py": "python:3.12-slim",
    "rust": "rust:1-slim",
    "go": "golang:1-bookworm",
    "golang": "golang:1-bookworm",
    "javascript": "node:22-bookworm-slim",
    "typescript": "node:22-bookworm-slim",
    "js": "node:22-bookworm-slim",
    "ts": "node:22-bookworm-slim",
    "node": "node:22-bookworm-slim",
    "ruby": "ruby:3-slim",
    "java": "eclipse-temurin:21-jdk",
    "kotlin": "eclipse-temurin:21-jdk",
}

_TRUE_CONFIG_VALUES = {"1", "true", "yes", "on"}
_FALSE_CONFIG_VALUES = {"0", "false", "no", "off", ""}


def _config_bool(value: object, default: bool = False) -> bool:
    """Parse hand-edited/interpolated booleans without string truthiness."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_CONFIG_VALUES:
            return True
        if normalized in _FALSE_CONFIG_VALUES:
            return False
        log.warning("invalid sandbox boolean %r; using default %s", value, default)
        return default
    if isinstance(value, int):
        if value in (0, 1):
            return value == 1
        log.warning("invalid sandbox boolean %r; using default %s", value, default)
        return default
    log.warning("invalid sandbox boolean %r; using default %s", value, default)
    return default


def _resolve_image(full_cfg: dict) -> str:
    """Resolve an explicit Docker image or the evaluator language default."""
    explicit = full_cfg.get("image")
    if explicit:
        return str(explicit)
    lang_value = full_cfg.get("language") or os.environ.get("MAVERICK_LANGUAGE", "")
    if not isinstance(lang_value, str):
        return _DEFAULT_IMAGE
    return _IMAGE_BY_LANGUAGE.get(lang_value.strip().lower(), _DEFAULT_IMAGE)


_IMMUTABLE_IMAGE_RE = re.compile(
    r"^(?:[^\s@]+@)?sha256:[0-9a-fA-F]{64}$"
)


def _secure_defaults_enabled() -> bool:
    try:
        from ..security_defaults import secure_by_default

        return bool(secure_by_default())
    except Exception:  # pragma: no cover - uncertainty must preserve firm posture
        return True


def _require_immutable_image(image: str, full_cfg: dict) -> None:
    """Reject mutable Docker tags for secure or require-container execution."""
    if not (_secure_defaults_enabled() or _container_backend_required(full_cfg)):
        return
    if not _IMMUTABLE_IMAGE_RE.fullmatch(image.strip()):
        raise SandboxPolicyError(
            "secure Docker execution requires an immutable image reference: set "
            "[sandbox] image to repository@sha256:<64-hex-digest> (or a local "
            "sha256:<64-hex-image-id>); mutable tags and defaults are refused"
        )


_LOCAL_WARNING_EMITTED = False


def fs_is_host_visible(sandbox: object | None) -> bool:
    """Whether files written by ``sandbox`` are visible to this process."""
    if sandbox is None:
        return True
    return bool(getattr(sandbox, "host_visible_fs", False))


class SandboxPolicyError(RuntimeError):
    """The selected backend violates the retained firm sandbox policy."""


def _config_truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_CONFIG_VALUES


def _container_backend_required(full_cfg: dict | None = None) -> bool:
    """Whether Docker isolation is mandatory and local execution is refused."""
    if _config_truthy_env("MAVERICK_REQUIRE_CONTAINER_BACKEND"):
        return True
    if full_cfg and _config_bool(full_cfg.get("require_container"), False):
        return True
    try:
        from ..enterprise import enterprise_enabled

        return bool(enterprise_enabled())
    except Exception:  # pragma: no cover - config lookup must not break startup
        return False


def container_backend_required() -> bool:
    """Config-aware public predicate for the Docker-isolation requirement."""
    try:
        from ..config import load_config

        full_cfg = (load_config() or {}).get("sandbox", {})
    except Exception:  # pragma: no cover - build_sandbox performs the final gate
        full_cfg = None
    return _container_backend_required(full_cfg)


def _default_container_backend() -> str | None:
    return "docker" if shutil.which("docker") else None


def _resolve_local_under_policy(chosen: str, full_cfg: dict | None) -> str:
    """Replace local with Docker under require-container, or fail closed."""
    if chosen != "local" or not _container_backend_required(full_cfg):
        return chosen
    if _default_container_backend() is None:
        raise SandboxPolicyError(
            "refusing the unsandboxed 'local' backend: require-container policy "
            "is active but Docker is unavailable. Install Docker and set "
            "[sandbox] backend = \"docker\"."
        )
    log.warning(
        "require-container policy is active and [sandbox] backend is 'local'; "
        "selecting the available Docker backend"
    )
    return "docker"


def _warn_local_unsandboxed() -> None:
    """Warn once when subprocess-capable work can execute on the host."""
    global _LOCAL_WARNING_EMITTED
    if _LOCAL_WARNING_EMITTED:
        return
    if os.environ.get("MAVERICK_SUPPRESS_SANDBOX_WARNING") == "1":
        _LOCAL_WARNING_EMITTED = True
        return
    _LOCAL_WARNING_EMITTED = True
    log.warning(
        "sandbox backend is 'local': subprocess-capable work runs directly on "
        "this host with no container isolation. Use [sandbox] backend = "
        "\"docker\" for untrusted evaluator workloads. Silence with "
        "MAVERICK_SUPPRESS_SANDBOX_WARNING=1."
    )


def _read_only_paths_for_backend(chosen: str, full_cfg: dict | None):
    """Return immutable mounts only for Docker, which enforces them."""
    paths = (full_cfg or {}).get("read_only_paths") or ()
    if paths and chosen != "docker":
        raise ValueError(
            "sandbox.read_only_paths requires backend='docker'; "
            f"backend={chosen!r} cannot enforce immutable mounts"
        )
    return paths


def build_sandbox(
    workdir: str | Path | None = None,
    backend: str | None = None,
    *,
    sandbox_config: dict | None = None,
) -> Sandbox:
    """Construct exactly one retained backend from a pinned/configured policy."""
    if sandbox_config is not None:
        full_cfg = dict(sandbox_config) if isinstance(sandbox_config, dict) else {}
        cfg = full_cfg
    else:
        try:
            from ..config import get_sandbox

            cfg = get_sandbox()
            try:
                from ..config import load_config

                full_cfg = (load_config() or {}).get("sandbox", {})
            except Exception:
                full_cfg = {}
        except Exception:
            cfg = {}
            full_cfg = {}

    chosen = str(backend or cfg.get("backend") or "local").strip().lower()
    if chosen not in BUILTIN_SANDBOX_BACKENDS:
        raise SandboxPolicyError(
            f"unsupported sandbox backend {chosen!r}; retained backends are: "
            f"{', '.join(BUILTIN_SANDBOX_BACKENDS)}"
        )
    chosen = _resolve_local_under_policy(chosen, full_cfg)
    read_only_paths = _read_only_paths_for_backend(chosen, full_cfg)
    wd = Path(workdir or cfg.get("workdir", str(Path.cwd()))).expanduser()

    try:
        timeout = float(cfg.get("timeout", 60))
        if timeout <= 0:
            raise ValueError("non-positive")
    except (TypeError, ValueError):
        log.warning("invalid [sandbox] timeout %r; using 60s", cfg.get("timeout"))
        timeout = 60.0

    if chosen == "docker":
        image = _resolve_image(full_cfg)
        _require_immutable_image(image, full_cfg)
        return DockerBackend(
            workdir=wd,
            image=image,
            timeout=timeout,
            allow_network=_config_bool(full_cfg.get("allow_network"), False),
            pids_limit=full_cfg.get("pids_limit", 512),
            memory=full_cfg.get("memory", "4g"),
            cpus=full_cfg.get("cpus"),
            allow_root=_config_bool(full_cfg.get("allow_root"), False),
            reuse_container=_config_bool(full_cfg.get("reuse_container"), False),
            read_only_paths=read_only_paths,
        )

    _warn_local_unsandboxed()
    return LocalBackend(workdir=wd, timeout=timeout)
