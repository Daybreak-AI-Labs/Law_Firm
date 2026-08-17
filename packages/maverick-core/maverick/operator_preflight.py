"""Deterministic, offline operator preflight for a fresh Maverick install.

``maverick doctor`` deliberately performs live checks (provider API, Docker
daemon, database).  That is useful diagnostics, but it is a poor automation
contract: the output is human-only and can change with the network.  This
module supplies the complementary read-only preflight used by installers,
deployment scripts, and the first-run dashboard:

* stable ordering and JSON shape;
* no network requests and no state creation;
* every blocker has one copyable remediation command;
* a ``run`` profile for the agent runtime and a stricter ``cockpit`` profile
  for the dashboard plus the evidence graph.

It reports configuration, not legal or regulatory compliance.
"""
from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

Status = Literal["ready", "blocked", "attention"]
Profile = Literal["run", "cockpit"]

SCHEMA = "maverick.operator-preflight.v1"


@dataclass(frozen=True)
class Check:
    """One stable, secret-free readiness check."""

    id: str
    label: str
    status: Status
    detail: str
    remediation: str = ""


@dataclass(frozen=True)
class Report:
    """Structured operator preflight result."""

    schema: str
    profile: Profile
    ready: bool
    blocker_count: int
    attention_count: int
    checks: tuple[Check, ...]
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "checks": [asdict(check) for check in self.checks],
        }


def _config_state() -> tuple[Check, dict[str, Any] | None]:
    from .config import config_path, config_source_errors, load_config

    path = config_path()
    if not path.is_file():
        return (
            Check(
                "config",
                "Configuration",
                "blocked",
                f"No configuration file at {path}.",
                "maverick init --fast",
            ),
            None,
        )
    try:
        cfg = load_config()
        errors = config_source_errors()
    except Exception as exc:
        return (
            Check(
                "config",
                "Configuration",
                "blocked",
                f"Configuration could not be loaded ({type(exc).__name__}).",
                "maverick config-lint",
            ),
            None,
        )
    if errors:
        names = ", ".join(sorted(Path(item).name for item in errors))
        return (
            Check(
                "config",
                "Configuration",
                "blocked",
                f"Active configuration is unreadable or invalid: {names}.",
                "maverick config-lint",
            ),
            None,
        )
    if not isinstance(cfg, dict):
        return (
            Check(
                "config",
                "Configuration",
                "blocked",
                "Configuration root must be a TOML table.",
                "maverick config-lint",
            ),
            None,
        )
    return (
        Check(
            "config",
            "Configuration",
            "ready",
            f"Loaded {path}.",
        ),
        cfg,
    )


def _provider_state(cfg: dict[str, Any] | None) -> tuple[Check, bool]:
    if cfg is None:
        return (
            Check(
                "provider",
                "Model provider",
                "blocked",
                "Provider readiness cannot be established until config is valid.",
                "maverick init --fast",
            ),
            False,
        )
    try:
        routed = _routed_configuration_missing(cfg)
        ready = sorted(provider for provider, missing in routed.items() if not missing)
        configured = bool(ready)
    except Exception:
        ready = []
        configured = False
    if not configured:
        return (
            Check(
                "provider",
                "Model provider",
                "blocked",
                "No selected model route has complete offline prerequisites.",
                "maverick init",
            ),
            False,
        )
    return (
        Check(
            "provider",
            "Model provider",
            "ready",
            f"Ready selected provider route(s): {', '.join(ready)}.",
        ),
        True,
    )


def _model_specs(cfg: dict[str, Any]) -> tuple[str, ...]:
    """Deterministically resolve every model route a normal swarm may use.

    The resolver is shared with ``model_for_role`` for environment, tenant
    role edits, config, dashboard pins, and the admin allow-list. Dynamic
    cost/local/energy routing performs live probes and therefore belongs to
    ``doctor``, not this offline preflight.
    """
    try:
        from .llm import ROLE_MODELS, offline_model_for_role
    except Exception:
        return ()
    configured = cfg.get("models")
    configured = configured if isinstance(configured, dict) else {}
    roles = set(ROLE_MODELS) | {
        str(role)
        for role, value in configured.items()
        if isinstance(value, str) and value.strip()
    }
    specs = {
        offline_model_for_role(role, config=cfg)
        for role in roles
    }
    return tuple(sorted(specs))


def _provider_table(cfg: dict[str, Any], provider: str) -> Mapping[str, Any]:
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return {}
    value = providers.get(provider)
    return value if isinstance(value, dict) else {}


def _present(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _route_configuration_missing(
    provider: str,
    cfg: dict[str, Any],
) -> tuple[str, ...]:
    """Return missing non-network prerequisites for one routed provider."""

    from .config import (
        PROVIDER_KEY_ENV_MAP,
        azure_provider_configuration_missing,
    )

    table = _provider_table(cfg, provider)
    configured_key = _present(table.get("api_key")) or any(
        _present(os.environ.get(name))
        for name in PROVIDER_KEY_ENV_MAP.get(provider, ())
    )
    if provider in {"ollama", "vllm", "tgi"}:
        # Each has a documented localhost default and an internal placeholder
        # key. Live server reachability belongs to `doctor`.
        return ()
    if provider == "openai_compatible":
        base = _present(table.get("base_url")) or _present(
            os.environ.get("OPENAI_COMPATIBLE_BASE_URL")
        )
        key = configured_key or _present(
            os.environ.get("OPENAI_COMPATIBLE_API_KEY")
        )
        return tuple(
            item
            for item, present in (("base_url", base), ("api_key", key))
            if not present
        )
    if provider == "azure":
        return azure_provider_configuration_missing(dict(table))
    if provider == "bedrock":
        key = _present(table.get("api_key")) or _present(
            os.environ.get("BEDROCK_API_KEY")
        )
        region = _present(os.environ.get("AWS_REGION"))
        return tuple(
            item
            for item, present in (("api_key", key), ("region", region))
            if not present
        )
    if provider == "codex_cli":
        try:
            from .providers.codex_cli_provider import _auth_file_present

            authenticated = configured_key or _auth_file_present()
        except Exception:
            authenticated = configured_key
        return () if authenticated else ("login",)
    if provider not in {
        "anthropic",
        "openai",
        "openrouter",
        "gemini",
        "moonshot",
        "deepseek",
        "xai",
    }:
        return ("known provider",)
    return () if configured_key else ("api_key",)


def _routed_configuration_missing(
    cfg: dict[str, Any],
) -> dict[str, tuple[str, ...]]:
    """Canonical provider prerequisite state for every selected model route."""
    from .providers import _canonical

    routed: dict[str, tuple[str, ...]] = {}
    for spec in _model_specs(cfg):
        provider = _canonical(
            spec.split(":", 1)[0] if ":" in spec else "anthropic"
        )
        routed[provider] = _route_configuration_missing(provider, cfg)
    return routed


def _format_route_missing(
    missing: Mapping[str, tuple[str, ...]],
) -> str:
    """Render secret-free, copyable provider prerequisites for operators."""
    from .config import PROVIDER_KEY_ENV_MAP

    exact_hints = {
        ("openai_compatible", "base_url"): "OPENAI_COMPATIBLE_BASE_URL",
        ("azure", "endpoint"): "AZURE_OPENAI_ENDPOINT",
        ("azure", "deployment"): "AZURE_OPENAI_DEPLOYMENT",
        (
            "azure",
            "authentication",
        ): "AZURE_OPENAI_API_KEY or Azure Entra credentials",
        ("bedrock", "region"): "AWS_REGION",
        ("codex_cli", "login"): "codex login",
    }
    rendered: list[str] = []
    for provider, fields in sorted(missing.items()):
        labels: list[str] = []
        for field in fields:
            if field == "api_key" and PROVIDER_KEY_ENV_MAP.get(provider):
                labels.append("/".join(PROVIDER_KEY_ENV_MAP[provider]))
            else:
                labels.append(exact_hints.get((provider, field), field))
        rendered.append(f"{provider}: {', '.join(labels)}")
    return "; ".join(rendered)


def _role_configuration_missing(
    role: str,
    cfg: dict[str, Any],
) -> tuple[str, tuple[str, ...]]:
    """Return the deterministic effective provider and prerequisites for ROLE."""
    from .llm import offline_model_for_role
    from .providers import _canonical

    spec = offline_model_for_role(role, config=cfg)
    provider = _canonical(
        spec.split(":", 1)[0] if ":" in spec else "anthropic"
    )
    return provider, _route_configuration_missing(provider, cfg)


def _route_state(cfg: dict[str, Any] | None) -> Check:
    if cfg is None:
        return Check(
            "model_routes",
            "Model routes",
            "blocked",
            "Model routes cannot be checked until config is valid.",
            "maverick init --fast",
        )
    try:
        routed = _routed_configuration_missing(cfg)
    except Exception as exc:
        return Check(
            "model_routes",
            "Model routes",
            "blocked",
            f"Model routes could not be inspected ({type(exc).__name__}).",
            "maverick config-lint",
        )
    missing = {
        provider: fields for provider, fields in routed.items() if fields
    }
    if missing:
        detail = _format_route_missing(missing)
        return Check(
            "model_routes",
            "Model routes",
            "blocked",
            f"Routed provider setup is incomplete ({detail}).",
            "maverick init",
        )
    labels = ", ".join(sorted(routed)) or "none"
    return Check(
        "model_routes",
        "Model routes",
        "ready",
        f"Offline prerequisites are present for: {labels}.",
    )


def _sdk_state(
    cfg: dict[str, Any] | None,
    provider_configured: bool,
) -> Check:
    if cfg is None or not provider_configured:
        return Check(
            "provider_dependencies",
            "Provider dependencies",
            "blocked",
            "Dependency readiness is waiting on a configured provider.",
            "maverick init",
        )
    try:
        from .providers import missing_sdks

        missing = missing_sdks(_model_specs(cfg))
    except Exception as exc:
        return Check(
            "provider_dependencies",
            "Provider dependencies",
            "blocked",
            f"Provider dependencies could not be inspected ({type(exc).__name__}).",
            "maverick doctor",
        )
    if missing:
        return Check(
            "provider_dependencies",
            "Provider dependencies",
            "blocked",
            " ".join(missing),
            "maverick doctor",
        )
    return Check(
        "provider_dependencies",
        "Provider dependencies",
        "ready",
        "SDK or local-provider executable is available for routed models.",
    )


def _nearest_existing_parent(path: Path) -> Path | None:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return candidate


def _storage_state(cfg: dict[str, Any] | None) -> Check:
    if cfg is None:
        return Check(
            "storage",
            "Runtime storage",
            "blocked",
            "Storage readiness cannot be established until config is valid.",
            "maverick init --fast",
        )
    backend = str(
        os.environ.get("MAVERICK_WORLD_BACKEND")
        or (cfg.get("world_model") or {}).get("backend")
        or "sqlite"
    ).strip().lower()
    if backend != "sqlite":
        return Check(
            "storage",
            "Runtime storage",
            "blocked",
            f"Unsupported world-model backend {backend!r}.",
            "maverick config-lint",
        )
    try:
        from .paths import diagnostic_data_dir

        db_path = diagnostic_data_dir("world.db")
    except Exception as exc:
        return Check(
            "storage",
            "Runtime storage",
            "blocked",
            f"Storage path could not be resolved ({type(exc).__name__}).",
            "maverick doctor",
        )
    if db_path.exists():
        if not db_path.is_file() or not os.access(db_path, os.R_OK | os.W_OK):
            return Check(
                "storage",
                "Runtime storage",
                "blocked",
                f"{db_path} is not a readable, writable regular file.",
                "maverick doctor",
            )
        return Check(
            "storage",
            "Runtime storage",
            "ready",
            f"SQLite state is readable and writable at {db_path}.",
        )
    parent = _nearest_existing_parent(db_path.parent)
    if parent is None or not parent.is_dir() or not os.access(parent, os.W_OK):
        return Check(
            "storage",
            "Runtime storage",
            "blocked",
            f"The parent for {db_path} cannot be created or written.",
            "maverick doctor",
        )
    return Check(
        "storage",
        "Runtime storage",
        "ready",
        f"SQLite state will be created at {db_path} on first use.",
    )


def _dashboard_state(*, required: bool) -> Check:
    try:
        installed = importlib.util.find_spec("maverick_dashboard") is not None
    except Exception:
        installed = False
    if installed:
        return Check(
            "dashboard",
            "Operator dashboard",
            "ready",
            "Dashboard package is installed.",
        )
    return Check(
        "dashboard",
        "Operator dashboard",
        "blocked" if required else "attention",
        "Dashboard package is not installed.",
        (
            "python -m pip install -e ./packages/maverick-dashboard"
            if required
            else "maverick doctor"
        ),
    )


def _tenant_state(
    cfg: dict[str, Any] | None,
    *,
    required: bool,
) -> Check:
    """Report whether evidence records have an explicit company boundary."""

    if cfg is None:
        return Check(
            "tenant_scope",
            "Company / tenant scope",
            "blocked" if required else "attention",
            "Tenant scope cannot be checked until config is valid.",
            "maverick config-lint",
        )
    try:
        from .paths import current_tenant_id_strict

        tenant = current_tenant_id_strict()
    except Exception as exc:
        return Check(
            "tenant_scope",
            "Company / tenant scope",
            "blocked",
            f"Tenant scope could not be resolved ({type(exc).__name__}).",
            "maverick config-lint",
        )
    if tenant:
        return Check(
            "tenant_scope",
            "Company / tenant scope",
            "ready",
            f"Evidence state is bound to tenant {tenant!r}.",
        )
    return Check(
        "tenant_scope",
        "Company / tenant scope",
        "blocked" if required else "attention",
        (
            "No company or tenant is selected. Evidence records require an "
            "explicit tenant boundary."
        ),
        "maverick config edit",
    )


def _feature_enabled(
    cfg: dict[str, Any] | None,
    module_name: str,
) -> bool:
    """Mirror the three default-off feature gates without importing stores."""

    if cfg is None:
        return False
    graph = (
        isinstance(cfg.get("evidence_graph"), dict)
        and cfg["evidence_graph"].get("enable") is True
    )
    if module_name == "evidence_graph":
        return graph
    return False


def _feature_state(
    cfg: dict[str, Any] | None,
    check_id: str,
    label: str,
    module_name: str,
    remediation: str,
    *,
    required: bool,
) -> Check:
    active = _feature_enabled(cfg, module_name)
    if active:
        return Check(check_id, label, "ready", "Enabled with valid dependencies.")
    return Check(
        check_id,
        label,
        "blocked" if required else "attention",
        "Disabled or a required dependency is not enabled.",
        remediation,
    )


def collect(profile: Profile = "run") -> Report:
    """Collect a stable, offline preflight report.

    ``run`` blocks only what prevents a normal agent run. ``cockpit`` also
    requires the dashboard, an explicit tenant scope, and the evidence
    graph.  The function performs no network requests and creates no state.
    """

    if profile not in {"run", "cockpit"}:
        raise ValueError("profile must be 'run' or 'cockpit'")
    config_check, cfg = _config_state()
    provider_check, provider_configured = _provider_state(cfg)
    checks: list[Check] = [
        config_check,
        provider_check,
        _route_state(cfg),
        _sdk_state(cfg, provider_configured),
        _storage_state(cfg),
    ]
    cockpit_required = profile == "cockpit"
    checks.append(_dashboard_state(required=cockpit_required))
    checks.append(_tenant_state(cfg, required=cockpit_required))
    checks.extend(
        [
            _feature_state(
                cfg,
                "evidence_graph",
                "Evidence graph",
                "evidence_graph",
                (
                    "set [evidence_graph] enable = true in "
                    "~/.maverick/config.toml"
                ),
                required=cockpit_required,
            ),
        ]
    )
    blockers = [check for check in checks if check.status == "blocked"]
    attention = [check for check in checks if check.status == "attention"]
    if blockers:
        next_action = blockers[0].remediation
    elif profile == "run":
        next_action = "maverick dashboard  # compose your first goal there"
    elif attention:
        next_action = attention[0].remediation
    else:
        next_action = "maverick dashboard"
    return Report(
        schema=SCHEMA,
        profile=profile,
        ready=not blockers,
        blocker_count=len(blockers),
        attention_count=len(attention),
        checks=tuple(checks),
        next_action=next_action,
    )


def render(report: Report) -> str:
    """Render a compact, copyable text report."""

    verdict = "READY" if report.ready else "BLOCKED"
    lines = [
        f"Maverick offline preflight [{report.profile}]: {verdict}",
        (
            f"{report.blocker_count} blocker(s), "
            f"{report.attention_count} attention item(s)"
        ),
    ]
    marker = {"ready": "OK", "blocked": "BLOCK", "attention": "CHECK"}
    for check in report.checks:
        lines.append(f"  [{marker[check.status]}] {check.label}: {check.detail}")
        if check.remediation:
            lines.append(f"          Fix: {check.remediation}")
    lines.append(f"Next action: {report.next_action}")
    lines.append(
        "Offline preflight checks configuration only; run `maverick doctor` "
        "for live connectivity."
    )
    return "\n".join(lines)


def render_json(report: Report) -> str:
    """Render the stable JSON automation contract."""

    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


__all__ = [
    "Check",
    "Profile",
    "Report",
    "SCHEMA",
    "collect",
    "render",
    "render_json",
]
