"""Configuration loader for Maverick.

Reads ``~/.maverick/config.toml`` (or the path set by ``$MAVERICK_CONFIG``).
Supports environment variable interpolation in string values: ``${VAR_NAME}``
is replaced with the env value, or the empty string if unset.

This is the surface the installer wizard writes to. Users can also edit
the TOML by hand. The kernel falls back to sensible defaults if no
config file exists, so research / dev use doesn't require running the
wizard first.

Schema overview::

    [providers.<name>]
    api_key = "${ANTHROPIC_API_KEY}"
    base_url = "..."  # optional

    [models]
    orchestrator = "anthropic:claude-opus-4-7"
    researcher   = "anthropic:claude-sonnet-4-6"
    # ...

    [budget]
    max_dollars = 5.0
    strict_pricing = true  # false => explicit legacy estimate-only accounting

    [safety]
    profile = "balanced"
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


# Note: do not cache the default at module import time. Both the platform home
# and the documented MAVERICK_HOME override can change in test/embedded
# processes, so resolve them dynamically inside config_path().
DEFAULT_CONFIG_BASENAME = (".maverick", "config.toml")


def _default_config_path() -> Path:
    from .paths import maverick_home

    return maverick_home() / DEFAULT_CONFIG_BASENAME[1]


def __getattr__(name: str):
    # Back-compat: `DEFAULT_CONFIG_PATH` used to be a module-level constant, but
    # caching it bound Path.home() at import time (stale under HOME changes /
    # monkeypatch — exactly what the comment above warns against). Resolve it
    # lazily on attribute access so every read reflects the current HOME.
    if name == "DEFAULT_CONFIG_PATH":
        return _default_config_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


CONFIG_OVERLAY_ENV = "MAVERICK_CONFIG_OVERLAY"

# Governed, in-process learning is the product baseline. High-authority
# actuation remains independently gated: code self-modification, live workflow
# auto-apply, and model-weight adoption do not inherit this default.
GOVERNED_LEARNING_DEFAULT = True

# Dashboard Settings overlay for config-read settings (provider keys,
# capability/feature toggles). Deep-merged over config.toml on every
# load_config() — always-on at a fixed path next to config.toml — so UI edits
# take effect on the next read without rewriting the user's config.toml. The
# dashboard owns this file (maverick_dashboard.settings_store). Distinct from
# runtime-overrides.toml (denied_tools / models / budget, read via own hooks).
DASHBOARD_OVERRIDES_BASENAME = "dashboard-config.toml"

# Accept lower/mixed-case names too: a hand-edited config referencing a
# lowercase env var (`${my_token}`) previously left the literal `${my_token}`
# in the value, silently un-substituted. The docstring promises "${VAR_NAME}
# is replaced" with no case restriction.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interp(value: Any) -> Any:
    """Recursively replace ``${VAR}`` with environment values."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interp(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interp(v) for v in value]
    return value


def config_path() -> Path:
    override = os.environ.get("MAVERICK_CONFIG")
    if override:
        return Path(override).expanduser()
    # Resolve dynamically so monkeypatch.setenv("HOME", ...) takes effect
    # mid-process — the prior `return DEFAULT_CONFIG_PATH` was evaluated
    # at import time and stayed stale.
    return _default_config_path()


# Parsed-TOML cache keyed by absolute path -> (mtime_ns, size, raw_dict). Only
# the expensive part (file read + TOML parse) is memoized; env-var interpolation
# and the overlay deep-merge still run on every load_config() call, so a changed
# ${VAR} or edited overlay is always reflected. Invalidated when the file's mtime
# OR size changes (a rewrite). load_config() is on many hot paths (a single
# inbound A2A delegate triggers ~6-10 full parses); this removes the redundant
# I/O + tokenize while preserving exact semantics.
_toml_cache: dict[str, tuple[int, int, dict]] = {}
# General configuration remains fail-soft, but security boundaries need to
# distinguish "file absent" from "present but unreadable/corrupt". Track the
# latter alongside the parsed cache so trust/auth callers can fail closed.
_toml_errors: dict[str, str] = {}
# Bound the cache so a deployment with many per-tenant config paths
# (~/.maverick/tenants/<t>/config.toml) can't grow it without limit. Entries
# are cheap to rebuild (a stat + parse), so a simple oldest-first cap is enough.
_TOML_CACHE_MAX = 512


def reset_config_cache() -> None:
    """Drop the parsed-TOML and .env-file caches (test hook; prod files rarely
    change)."""
    global _env_last_check
    _toml_cache.clear()
    _toml_errors.clear()
    _env_file_cache.clear()
    _env_last_check = None


# Wizard-persisted secrets: the installer writes every collected key to
# ``~/.maverick/.env`` (next to config.toml) and references it from config
# values as ``${VAR}``. Docker-compose (``env_file``) and the systemd unit
# (``EnvironmentFile=``) inject that file, but a bare pip/CLI install had no
# injector at all, so ``${VAR}`` interpolated to "" and a freshly validated
# key could never reach the runtime (`maverick start` exited 2 right after
# `maverick init` said "Setup complete"). load_config() exports the file
# itself -- ``setdefault`` only, so an explicitly exported variable always
# wins -- memoized by mtime+size so a change is a re-read, not a re-load.
# The freshness re-check itself is throttled to once per second: load_config()
# sits on per-row hot paths (e.g. _dec_field on every sealed column read), and
# an unconditional config_path()+stat() per call measurably breached the
# world_read perf SLA. A wizard-written key still lands within a second.
_ENV_FILE_BASENAME = ".env"
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ENV_RECHECK_SECONDS = 1.0
_env_file_cache: dict[str, tuple[int, int]] = {}
_env_last_check: float | None = None
# Bound at import: tests that monkeypatch time.monotonic (a process-global
# patch) to drive their OWN timeout logic must not have the values consumed
# by this unrelated guard on every load_config() call.
_monotonic = time.monotonic


def _load_wizard_env() -> None:
    """Throttled wrapper: re-check ``~/.maverick/.env`` at most once per
    :data:`_ENV_RECHECK_SECONDS` (first call always loads)."""
    global _env_last_check
    now = _monotonic()
    if _env_last_check is not None and now - _env_last_check < _ENV_RECHECK_SECONDS:
        return
    _env_last_check = now
    _load_env_file(config_path().parent / _ENV_FILE_BASENAME)


def _load_env_file(path: Path) -> None:
    """Export ``KEY=value`` lines from ``path`` into ``os.environ`` without
    overriding variables that are already set."""
    try:
        st = path.stat()
    except OSError:
        return  # no .env (or unreadable) -> nothing to load
    key = str(path)
    if _env_file_cache.get(key) == (st.st_mtime_ns, st.st_size):
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    _env_file_cache[key] = (st.st_mtime_ns, st.st_size)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not _ENV_NAME_RE.match(name):
            continue
        value = value.strip()
        # Tolerate hand-edited quoting (systemd EnvironmentFile strips it too).
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _read_toml_raw(path: Path) -> dict:
    """Parsed TOML for ``path`` (NO interpolation), memoized by mtime+size.
    Returns ``{}`` for a missing file, and ``{}`` + a warning for a corrupt one."""
    key = str(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        _toml_cache.pop(key, None)
        _toml_errors.pop(key, None)
        return {}  # an absent optional config legitimately means defaults
    except OSError as e:
        _toml_cache.pop(key, None)
        _toml_errors[key] = f"{type(e).__name__}: {e}"
        return {}  # general callers stay fail-soft; security callers inspect health
    cached = _toml_cache.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        # The kernel must tolerate a missing config (returns {} above); a
        # corrupt/unreadable one is the adjacent case. Fail soft to defaults
        # with a warning instead of crashing non-secure compatibility callers
        # or every get_safety caller on a hand-edited TOML typo. Secure model
        # selection separately fails closed when its exact pin is unavailable.
        logging.getLogger(__name__).warning(
            "ignoring unreadable %s (%s: %s); using defaults",
            path, type(e).__name__, e,
        )
        _toml_errors[key] = f"{type(e).__name__}: {e}"
        raw = {}
    else:
        _toml_errors.pop(key, None)
    # Evict oldest entries first when over the cap (dicts preserve insertion
    # order). Re-fetching ``key`` below keeps the just-read path resident.
    while len(_toml_cache) >= _TOML_CACHE_MAX:
        evicted = next(iter(_toml_cache))
        _toml_cache.pop(evicted, None)
        _toml_errors.pop(evicted, None)
    _toml_cache[key] = (st.st_mtime_ns, st.st_size, raw)
    return raw


def _load_config_file(path: Path) -> dict:
    # _interp returns a fresh dict tree on every call, so the cached raw dict is
    # never mutated by callers; env substitution stays live.
    return _interp(_read_toml_raw(path))


def _deep_merge_config(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def dashboard_overrides_path() -> Path:
    """Path to the dashboard-owned config overlay (next to config.toml)."""
    return config_path().parent / DASHBOARD_OVERRIDES_BASENAME


def load_governed_records_config() -> dict:
    """Load only sources allowed to select the governed-record backend.

    Tenant overlays must not select a deployment-global persistence plane.
    Base/operator configuration and environment variables remain the authority
    for that selection.
    """
    _load_wizard_env()
    cfg = _load_config_file(config_path())
    overlay = os.environ.get(CONFIG_OVERLAY_ENV)
    if overlay:
        cfg = _deep_merge_config(
            cfg,
            _load_config_file(Path(overlay).expanduser()),
        )
    return cfg


def governed_records_config_source_errors() -> dict[str, str]:
    """Unreadable sources participating in governed-backend selection."""
    paths = [config_path()]
    overlay = os.environ.get(CONFIG_OVERLAY_ENV)
    if overlay:
        paths.append(Path(overlay).expanduser())
    return _source_errors(paths)


def _source_errors(paths: list[Path]) -> dict[str, str]:
    """Return current parse/I/O errors for exactly ``paths``."""
    # Optional overlays are skipped by ``load_config`` once they disappear, so
    # a parse error cached while the file existed would otherwise remain
    # security-significant forever. Clear it only when the filesystem
    # definitively reports that the source is absent. Permission failures and
    # every other I/O error remain fail-closed: they may mean policy exists but
    # cannot be inspected.
    for path in paths:
        key = str(path)
        if key not in _toml_errors:
            continue
        try:
            path.stat()
        except FileNotFoundError:
            _toml_cache.pop(key, None)
            _toml_errors.pop(key, None)
        except OSError:
            pass
    return {
        str(path): _toml_errors[str(path)]
        for path in paths
        if str(path) in _toml_errors
    }
def config_source_errors(*, include_tenant: bool = True) -> dict[str, str]:
    """Unreadable/corrupt files among the active config sources.

    ``load_config`` intentionally returns defaults for malformed TOML so a typo
    does not crash ordinary model-selection calls. Authentication and trust
    callers inspect this seam immediately after loading and deny remote control
    if policy may have been lost.
    """
    paths = [config_path(), dashboard_overrides_path()]
    overlay = os.environ.get(CONFIG_OVERLAY_ENV)
    if overlay:
        paths.append(Path(overlay).expanduser())
    if include_tenant:
        tenant = tenant_config_path()
        if tenant is not None:
            paths.append(tenant)
    return _source_errors(paths)


def governed_learning_default() -> bool:
    """Default-on only when every active configuration source is trustworthy.

    The fail-soft TOML loader deliberately maps both an absent file and a
    malformed file to ``{}``. Mutating learning must distinguish them: absence
    gets the product default, while an unreadable/corrupt policy fails closed.
    """
    try:
        return GOVERNED_LEARNING_DEFAULT and not bool(config_source_errors())
    except Exception:  # pragma: no cover -- uncertainty cannot enable mutation
        return False


def governed_learning_env_flag(name: str) -> bool | None:
    """Tri-state learning override that cannot bypass corrupt policy.

    An explicit ``VAR=0`` remains a reliable opt-out. ``VAR=1`` is honored only
    after every active configuration source parses successfully.
    """
    try:
        load_config()
        if config_source_errors():
            return False
        value = env_flag(name)
        # ``env_flag`` intentionally treats absent and malformed values alike
        # for general feature switches. Default-on learning cannot: a present
        # but unrecognized value must never fall through and enable mutation.
        if value is None and name in os.environ:
            return False
        return value
    except Exception:  # pragma: no cover -- uncertainty cannot enable mutation
        return False


def tenant_config_path() -> Path | None:
    """Path to the active tenant's config overlay, or None in single-tenant mode.

    Resolves to ``~/.maverick/tenants/<tenant>/config.toml`` when a tenant is
    active via an explicit ``set_tenant`` scope or the ``MAVERICK_TENANT`` env
    var, and None otherwise. Deliberately config-free: it reads only the tenant
    ContextVar and env var, NOT ``current_tenant_id()`` (whose client-binding
    branch reads config and would recurse back into ``load_config`` on every
    call -- a hot-path blow-up). A client-bound single deployment already loads
    its own ``config.toml``, so it needs no separate per-tenant overlay.
    """
    try:
        from .paths import _TENANT, _tenant_segment, maverick_home
        tid = _TENANT.get() or os.environ.get("MAVERICK_TENANT", "").strip() or None
        if not tid:
            return None
        return maverick_home() / "tenants" / _tenant_segment(tid) / "config.toml"
    except Exception:  # pragma: no cover -- config resolution never blocks a run
        return None


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def env_flag(name: str) -> bool | None:
    """Parse an env var as a tri-state boolean: ``True``/``False`` for a
    recognized truthy/falsy value, ``None`` when unset or unrecognized. Lets the
    module ``enabled()`` gates share one parser instead of re-spelling the
    ``{"1","true","yes","on"}`` literal set (and its inverse) each time.

    Positive-only callers use ``if env_flag(name): ...``; tri-state callers use
    ``v = env_flag(name); if v is not None: return v``."""
    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return None


def _strict_config_bool(
    cfg: dict, key: str, default: bool, *, invalid: bool = False,
) -> bool:
    """Read a policy boolean without Python's string-truthiness footgun.

    TOML normally supplies a real bool, but dashboard overlays and callers may
    provide malformed values. Missing uses the documented default; a present
    non-bool uses the caller's fail-closed posture.
    """
    if key not in cfg:
        return bool(default)
    value = cfg.get(key)
    return value if isinstance(value, bool) else bool(invalid)


def load_global_config() -> dict:
    """Load deployment-global config without a per-tenant overlay.

    Repo-global control planes such as DGM must not change authority depending
    on whichever tenant happens to be active in the current request context.
    The dashboard overlay remains writable by the global admin; the optional
    operator-owned config overlay still has higher precedence.
    """
    # Wizard-persisted secrets must be in the environment BEFORE interpolation,
    # or every "${ANTHROPIC_API_KEY}" the wizard wrote resolves to "" on a bare
    # pip/CLI install. setdefault semantics: a real export always wins.
    _load_wizard_env()
    cfg = _load_config_file(config_path())
    # Dashboard Settings overlay (always-on, fixed path): UI-edited provider
    # keys / capability+feature toggles merge over config.toml without touching it.
    dash = dashboard_overrides_path()
    if dash.exists():
        cfg = _deep_merge_config(cfg, _load_config_file(dash))
    overlay = os.environ.get(CONFIG_OVERLAY_ENV)
    if overlay:
        cfg = _deep_merge_config(cfg, _load_config_file(Path(overlay).expanduser()))
    return cfg


def load_config(path: Path | None = None) -> dict:
    if path is not None:
        return _load_config_file(path)

    cfg = load_global_config()
    # Per-tenant overlay (highest precedence): when a tenant is active, its own
    # config.toml wins, so each client supplies its own provider API keys, model
    # choices and budget without sharing one global credential set. Skipped in
    # single-tenant mode, so the legacy path is byte-for-byte unchanged.
    tcfg = tenant_config_path()
    if tcfg is not None and tcfg.exists():
        cfg = _deep_merge_config(cfg, _load_config_file(tcfg))
    return cfg


def get_role_model(
    role: str,
    *,
    config: dict[str, Any] | None = None,
) -> str | None:
    """Return the model spec ("provider:model-id") for a role, or None.

    This is a legacy/development compatibility reader for role-keyed config.
    Secure firm execution ignores it and requires one exact ``[models] default``
    through :func:`maverick.llm.model_for_role`.

    ``config`` lets legacy callers reuse an already-admitted snapshot instead
    of loading configuration twice.
    """
    cfg = load_config() if config is None else config
    spec = cfg.get("models", {}).get(role)
    return spec if isinstance(spec, str) and spec else None


def get_provider_config(provider: str) -> dict:
    cfg = load_config()
    raw = cfg.get("providers", {}).get(provider, {})
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    key = out.get("api_key")
    if isinstance(key, str):
        from .crypto_at_rest import unseal_from_str

        out["api_key"] = unseal_from_str(key)
    return out


# Canonical provider -> API-key env var(s). These values are safe to pass as
# the provider client's ``api_key`` argument; non-key credentials (for example
# Azure's pre-fetched Entra token) belong in PROVIDER_CREDENTIAL_ENV_MAP below.
# Aliases (GROK/GOOGLE) are included.
PROVIDER_KEY_ENV_MAP: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "xai": ("XAI_API_KEY", "GROK_API_KEY"),
    "tgi": ("TGI_API_KEY",),
    "vllm": ("VLLM_API_KEY",),
    "azure": ("AZURE_OPENAI_API_KEY",),
    "bedrock": ("BEDROCK_API_KEY",),
    "openai_compatible": ("OPENAI_COMPATIBLE_API_KEY",),
    # ChatGPT/Codex access token for the codex_cli provider. Consumed by
    # `codex login --with-access-token` (fed on STDIN, never argv/logs).
    "codex_cli": ("CODEX_ACCESS_TOKEN",),
}

PROVIDER_KEY_ENV_VARS = tuple(
    dict.fromkeys(v for vs in PROVIDER_KEY_ENV_MAP.values() for v in vs)
)

# Every environment credential that can authenticate a provider, including
# credentials that are deliberately not API keys. Security boundaries such as
# the GitHub Action output sanitizer and the "is anything configured?" probe
# use this broader map; client construction continues to use the key-only map.
PROVIDER_CREDENTIAL_ENV_MAP: dict[str, tuple[str, ...]] = {
    **PROVIDER_KEY_ENV_MAP,
    "azure": (*PROVIDER_KEY_ENV_MAP["azure"], "AZURE_OPENAI_AD_TOKEN"),
}
PROVIDER_CREDENTIAL_ENV_VARS = tuple(
    dict.fromkeys(v for vs in PROVIDER_CREDENTIAL_ENV_MAP.values() for v in vs)
)

# Self-hosted endpoints configured by env var (the mechanism each provider's
# docstring documents). Ollama has no env var: its only custom-URL surface is
# ``[providers.ollama] base_url`` in config, covered below.
PROVIDER_BASE_URL_ENV_VARS = (
    "VLLM_BASE_URL", "TGI_BASE_URL", "OPENAI_COMPATIBLE_BASE_URL",
)


def azure_provider_configuration_missing(
    provider_config: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return Azure OpenAI's missing non-network prerequisites.

    Keep this in the configuration layer because both the generic
    ``any_provider_configured`` gate and the route-specific operator preflight
    need the exact same answer.  In particular, a bare Entra token is not a
    runnable Azure route, while managed identity needs no literal secret when
    Entra authentication is selected explicitly.

    Authentication resolution mirrors ``LLM._provider_client_config`` and
    ``providers.azure_openai_provider._azure_auth``: a non-empty
    ``[providers.azure].auth_mode`` overrides ``AZURE_OPENAI_AUTH``, a
    configured API key overrides the environment key, and ambiguous or unknown
    authentication selections fail closed.
    """
    table = provider_config if isinstance(provider_config, dict) else {}

    def present(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def normalized_secret(value: object) -> str:
        if isinstance(value, str):
            return value.strip()
        return str(value).strip() if value else ""

    endpoint = present(table.get("base_url")) or present(
        os.environ.get("AZURE_OPENAI_ENDPOINT")
    )
    key = normalized_secret(table.get("api_key"))
    if not key:
        key = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
    ad_token = os.environ.get("AZURE_OPENAI_AD_TOKEN", "").strip()

    configured_auth_mode = table.get("auth_mode")
    auth_mode = (
        configured_auth_mode.strip().lower()
        if isinstance(configured_auth_mode, str) and configured_auth_mode.strip()
        else os.environ.get("AZURE_OPENAI_AUTH", "").strip().lower()
    )
    if auth_mode not in {"", "api_key", "entra_id"} or (key and ad_token):
        authentication = False
    elif auth_mode == "api_key":
        authentication = bool(key)
    elif auth_mode == "entra_id":
        # A static token is optional: without one the runtime intentionally
        # constructs DefaultAzureCredential after the SDK preflight.
        authentication = not key
    else:
        authentication = bool(key) ^ bool(ad_token)

    deployment = present(os.environ.get("AZURE_OPENAI_DEPLOYMENT"))
    return tuple(
        item
        for item, configured in (
            ("endpoint", endpoint),
            ("authentication", authentication),
            ("deployment", deployment),
        )
        if not configured
    )


def any_provider_configured() -> bool:
    """The ONE predicate for "can this install reach some LLM provider?".

    Three legitimate configuration surfaces, all honored:
      1. a well-known credential env var (hosted providers);
      2. a self-hosted base-URL env var (vLLM / TGI / OpenAI-compatible);
      3. a ``[providers.<name>]`` config table carrying a non-empty
         ``api_key`` or ``base_url`` (``${VAR}`` interpolates to "" when
         unset, so an empty interpolation does not count).
      4. an existing Codex CLI login at ``$CODEX_HOME/auth.json``.

    Found by running the platform as a user: the CLI preflight, the LLM
    clients, and the dashboard each implemented a different subset, so a
    keyless self-hosted setup was accepted by one component and rejected by
    the next. Use this helper instead of growing a fourth variant.
    """
    # Read config FIRST: load_config() also exports the wizard-persisted
    # ~/.maverick/.env into the environment (setdefault), so the env-var checks
    # below see installer-collected keys even when config.toml carries no
    # [providers] table.
    providers = load_config().get("providers") or {}
    # Azure is the exception to the useful "a credential is enough" shortcut:
    # its dedicated client also requires an endpoint and deployment, and
    # DefaultAzureCredential can be intentionally selected without a literal
    # secret.  Do not let a lone token unblock the CLI or reject a complete
    # managed-identity route.
    non_azure_credentials = (
        name
        for provider, names in PROVIDER_CREDENTIAL_ENV_MAP.items()
        if provider != "azure"
        for name in names
    )
    if any(os.environ.get(v) for v in non_azure_credentials):
        return True
    if any(os.environ.get(v) for v in PROVIDER_BASE_URL_ENV_VARS):
        return True
    try:
        from .providers.codex_cli_provider import _auth_file_present

        if _auth_file_present():
            return True
    except Exception:  # pragma: no cover -- optional provider must not break probe
        pass
    azure_table = providers.get("azure") if isinstance(providers, dict) else None
    if not azure_provider_configuration_missing(azure_table):
        return True
    for provider, pcfg in providers.items():
        if provider == "azure":
            continue
        if not isinstance(pcfg, dict):
            continue
        if str(pcfg.get("api_key", "")).strip() or str(pcfg.get("base_url", "")).strip():
            return True
    return False


def get_budget_overrides() -> dict:
    return load_config().get("budget", {})


def get_capabilities() -> dict:
    """Return the [capabilities] section (computer_use / browser / web_search /
    mobile_tools). These gate the optional high-impact tools in
    ``tools.base_registry``; all default off."""
    cfg = load_config().get("capabilities", {}) or {}
    return {
        "computer_use": bool(cfg.get("computer_use", False)),
        "browser": bool(cfg.get("browser", False)),
        "web_search": bool(cfg.get("web_search", False)),
        "mobile_tools": bool(cfg.get("mobile_tools", False)),
        # Programmatic tool calling: a sandboxed Python script that orchestrates
        # declared tool calls (also enableable via MAVERICK_CODE_EXEC).
        "code_exec": bool(cfg.get("code_exec", False)),
    }

def get_features() -> dict:
    """Return the [features] section. These toggle agent-facing behaviors that
    are otherwise always on:

    - ``skills``      inject distilled/installed skills into agent prompts.
                      The MAVERICK_USE_SKILLS env var, when set, overrides this.
    - ``streaming``   stream live progress to the terminal during `maverick
                      start`. The MAVERICK_NO_PROGRESS env var / non-TTY output
                      still suppress it.
    - ``pack_editing`` allow editing/overriding domain packs (agents) from the
                      dashboard editor. Off = the editor is read-only and the
                      mutating REST endpoints return 403, so an operator can
                      lock the agent roster in a governed deployment. Writing
                      override TOML on the host is unaffected.
    - ``role_editing`` allow editing the core agent roles (orchestrator, coder,
                      ...) from the dashboard -- a per-tenant system-prompt
                      addendum plus model/effort overrides per role (which win
                      over the global [models]/[effort] config). Off = the roles
                      editor is read-only and its mutating endpoints 403.
    All default on.
    """
    cfg = load_config().get("features", {}) or {}
    return {
        "skills": bool(cfg.get("skills", True)),
        "streaming": bool(cfg.get("streaming", True)),
        "pack_editing": bool(cfg.get("pack_editing", True)),
        "role_editing": bool(cfg.get("role_editing", True)),
    }


def get_safety() -> dict:
    """Return safety section with sensible defaults filled in."""
    cfg = load_config().get("safety", {})
    return {
        "profile": cfg.get("profile", "balanced"),
        "block_threshold": cfg.get("block_threshold", "high"),
        "scan_input": cfg.get("scan_input", True),
        "scan_tool_calls": cfg.get("scan_tool_calls", True),
        "scan_output": cfg.get("scan_output", True),
        # Operator-defined policy rules consumed by Shield.from_config().
        "constitution": cfg.get("constitution", []),
        # Agent compartments: a swarm-shared threat ledger so one agent's
        # blocked threat immunizes the rest for the run. Off by default.
        "compartments": cfg.get("compartments", False),
        # Who may clear a latched Rung-2 sector seal: "human" | "orchestrator"
        # | "both". Default human-only (safest for a security boundary).
        "compartment_unseal": cfg.get("compartment_unseal", "human"),
    }


def get_skills() -> dict:
    """Return the ``[skills]`` section with signing defaults filled in.

    ``trusted_pubkeys`` is a list of hex-encoded Ed25519 publisher keys; a
    signed skill is only accepted if its ``pubkey`` is in this list (when
    the list is non-empty). ``require_signed`` rejects unsigned local skills.
    Remote catalog installation is not part of the law-firm runtime.
    """
    cfg = load_config().get("skills", {})
    pubkeys = cfg.get("trusted_pubkeys", [])
    return {
        "trusted_pubkeys": [str(k) for k in pubkeys] if isinstance(pubkeys, list) else [],
        "require_signed": bool(cfg.get("require_signed", False)),
        # Recall the shipped first-party skills library at runtime (opt-out).
        "builtin": bool(cfg.get("builtin", True)),
        # Relevance GATES on skill recall. Precision >> recall for agent memory:
        # weakly-relevant retrieved context regresses the agent (hard negatives
        # flip answers -- GSM-DC/GSM-IC; large/noisy memory degrades -- Lifelong-
        # AgentBench). The embedding path keeps a skill only above this cosine;
        # the lexical fallback keeps one only at/above this RAW score (a real
        # two-word or phrase match), so noise is never injected -> warm is never
        # worse than cold.
        "embed_threshold": float(cfg.get("embed_threshold", 0.35)),
        "lexical_min_relevance": float(cfg.get("lexical_min_relevance", 4.0)),
    }


def get_sandbox() -> dict:
    cfg = load_config().get("sandbox", {})
    return {
        "backend": cfg.get("backend", "local"),
        "workdir": cfg.get("workdir", "~/maverick-workspace"),
        "timeout": cfg.get("timeout", 60),
    }


#: Per-embedder (model, dim) defaults. A vector dimension that disagrees with
#: the model that produced it is not a cosmetic mismatch -- the store raises on
#: a dim mismatch rather than returning garbage, so a wrong default here reads
#: as a broken knowledge base.
_EMBEDDER_DEFAULTS: dict[str, tuple[str, int]] = {
    # Firm deployments must name and pin an operator-provisioned local model
    # directory.  A repository id here would let sentence-transformers fetch
    # code or weights at runtime.
    "local": ("", 384),
    "deterministic": ("", 256),
}


def get_knowledge() -> dict:
    """Return the ``[knowledge]`` section (per-domain vector RAG).

    ``embedder`` selects local/deterministic; the vector store is the embedded
    SQLite one. Client-matter chunks have no hosted embedding path. Retained
    legal profiles may require exact-matter knowledge and then fail closed when
    this package or its configured collection is unavailable.

    The local model intentionally has no default. Operators must configure an
    absolute, already-provisioned model directory and its canonical
    ``model_digest``. This prevents a repository id from triggering a runtime
    Hugging Face download. ``dim`` remains per-embedder and must match the
    pinned model/corpus.
    """
    cfg = load_config().get("knowledge", {}) or {}
    embedder = cfg.get("embedder", "local")
    model_default, dim_default = _EMBEDDER_DEFAULTS.get(
        str(embedder).lower(), _EMBEDDER_DEFAULTS["local"])
    return {
        "enable": bool(cfg.get("enable", False)),
        "embedder": embedder,
        "store": cfg.get("store", "sqlite"),
        "model": cfg.get("model", model_default),
        "model_digest": cfg.get("model_digest", ""),
        "dim": int(cfg.get("dim", dim_default)),
        "path": cfg.get("path", ""),
    }


def get_self_learning() -> dict:
    """Return the ``[self_learning]`` section with defaults filled in.

    Governed local learning and distillation are on by default. Extra
    task/result-bearing provider calls remain a separate authority decision.
    """
    cfg = load_config().get("self_learning", {})
    return {
        "enable": _strict_config_bool(
            cfg, "enable", governed_learning_default()),
        # Extra learning-only model calls may select a different configured
        # provider/role and can expose task or child-result text. Keep that
        # egress a separate explicit authority even though the local learning
        # engines themselves default on.
        "allow_provider_egress": cfg.get("allow_provider_egress") is True,
        "distill_local": _strict_config_bool(cfg, "distill_local", True),
    }


def get_self_harness() -> dict:
    """Return the ``[self_harness]`` section with defaults filled in.

    ON by default under the conservative ``risk_limited`` profile. The loop
    still cannot promote without held-out evidence, calibration and its
    promotion-controller gates:

    - ``risk_limited``: select the conservative unattended-promotion profile
      (held-out/effect/confidence floors, sealed best-of-3 selection, replicated
      judge votes, durable holdout accounting, operational caps, recent judge
      calibration, metamorphic checks, and canary staging). Safety-critical
      constituent settings may tighten but cannot weaken this contract.
    - ``min_support``: recurring failures before a weakness is mined (>=1).
    - ``require_held_out`` / ``min_held_out`` / ``min_delta``: the validation
      evidence floors (default off / 0 / 0.0) -- never promote on only the mined
      examples, an unseen-sample floor, an effect-size floor.
    - ``semantic_mining``: cluster failure diagnoses by embedding similarity
      when embeddings are available (default off -> deterministic Jaccard).
    - ``candidates_per_signature``: best-of-N proposing -- generate this many
      candidate lines per weakness and promote the strongest validated one
      (>=1; default 1 = single candidate, only matters with a stochastic
      proposer, a deterministic one collapses to 1).
    - ``mine_bucket_by``: extra dimensions to bucket mining by, beyond the
      always-on (model, failure_class). Allowlisted (``domain``) so a weakness
      can be mined -- and its guidance scoped -- per department; default empty
      (model-wide). Unknown dims are dropped.
    - ``retire_after_days``: auto-retire lines not active within this many days
      (0 = never; only acted on when a driver/CLI runs retirement).
    """
    cfg = load_config().get("self_harness", {})
    # A deployment with no harness policy gets the new conservative default-on
    # profile. An existing partial [self_harness] table keeps its historical
    # constituent defaults unless the operator (or dashboard/installer)
    # explicitly selects risk_limited/auto_run. This prevents an upgrade from
    # silently rewriting evaluation thresholds or starting scheduled spend.
    pristine_default = not cfg
    # One explicit profile for unattended, research-grade promotion.  It changes
    # defaults plus non-weakenable safety floors/ceilings. Constituent knobs may
    # still be tightened deliberately.
    risk_limited = _strict_config_bool(
        cfg, "risk_limited", pristine_default, invalid=True)

    def _num(key, default, cast):
        try:
            value = cast(cfg.get(key, default))
            if isinstance(value, float) and not math.isfinite(value):
                return cast(default)
            return value
        except (OverflowError, TypeError, ValueError):
            return default

    raw_by_class = cfg.get("min_support_by_class", {})
    by_class = {}
    if isinstance(raw_by_class, dict):
        for k, v in raw_by_class.items():
            try:
                by_class[str(k)] = max(3 if risk_limited else 1, int(v))
            except (OverflowError, TypeError, ValueError):
                continue

    # Mining bucket dimensions: keep only allowlisted reflexion dims
    # (mirrors self_harness._BUCKET_DIMS) and de-dupe, preserving order.
    _allowed_dims = ("domain", "tool", "role")
    raw_dims = cfg.get("mine_bucket_by", [])
    if isinstance(raw_dims, str):
        raw_dims = [raw_dims]
    elif not isinstance(raw_dims, (list, tuple)):
        raw_dims = []
    bucket_by = tuple(dict.fromkeys(
        d for d in (raw_dims or []) if isinstance(d, str) and d in _allowed_dims))

    def _factor(key, default=None):
        v = cfg.get(key)
        try:
            value = float(v) if v is not None else default
            # NaN defeats every ``> cap`` comparison and infinity silently
            # disables a cap. In the risk profile, an invalid override must
            # fall back to the conservative default rather than turn it off.
            return value if value is None or math.isfinite(value) else default
        except (OverflowError, TypeError, ValueError):
            return default

    def _risk_cap(key: str, ceiling: float) -> float | None:
        value = _factor(key, ceiling if risk_limited else None)
        if not risk_limited:
            return value
        if value is None or value <= 0.0:
            return ceiling
        return min(value, ceiling)

    raw_family_alpha = _num("holdout_family_alpha", 0.05, float)
    raw_query_alpha = _num(
        "holdout_query_alpha", 0.025 if risk_limited else 0.05, float)
    raw_max_queries = max(1, _num(
        "holdout_max_queries", 2 if risk_limited else 1, int))
    if risk_limited:
        family_alpha = (
            min(raw_family_alpha, 0.05)
            if 1e-12 <= raw_family_alpha < 1.0 else 0.05)
        holdout_max_queries = min(raw_max_queries, 2)
        query_alpha = (
            min(raw_query_alpha, 0.025)
            if 1e-12 <= raw_query_alpha < 1.0 else 0.025)
        # A stricter family budget must also tighten each allocated query so the
        # fixed policy remains internally valid instead of failing at runtime.
        query_alpha = min(
            query_alpha, family_alpha / holdout_max_queries)
    else:
        family_alpha = raw_family_alpha
        query_alpha = raw_query_alpha
        holdout_max_queries = raw_max_queries

    raw_calibration_age = max(
        0.0, _num(
            "calibration_max_age_hours", 24.0 if risk_limited else 0.0, float))
    calibration_max_age = (
        min(raw_calibration_age, 24.0)
        if risk_limited and raw_calibration_age > 0.0
        else (24.0 if risk_limited else raw_calibration_age)
    )
    raw_eval_budget_value = cfg.get(
        "eval_budget_dollars", 5.0 if risk_limited else None)
    eval_budget_valid = True
    if "eval_budget_dollars" in cfg:
        eval_budget_valid = (
            isinstance(raw_eval_budget_value, (int, float))
            and not isinstance(raw_eval_budget_value, bool)
            and math.isfinite(float(raw_eval_budget_value))
        )
    raw_eval_budget = (
        float(raw_eval_budget_value)
        if eval_budget_valid and raw_eval_budget_value is not None
        else (5.0 if risk_limited else None)
    )
    if risk_limited:
        eval_budget = (
            min(raw_eval_budget, 5.0)
            if raw_eval_budget is not None and raw_eval_budget > 0.0 else 5.0)
    else:
        eval_budget = (
            raw_eval_budget
            if raw_eval_budget is not None and raw_eval_budget > 0.0 else None)

    return {
        "enable": _strict_config_bool(
            cfg, "enable", governed_learning_default()),
        "risk_limited": risk_limited,
        # Run the governed cycle for the selected run model as part of
        # `maverick dream` (the nightly learning beat), so the harness operates
        # itself without a second cron entry. A pristine deployment defaults on;
        # an existing partial section retains its historical off default.
        "auto_run": (
            _strict_config_bool(cfg, "auto_run", pristine_default)
            and eval_budget_valid
        ),
        "min_support": (
            max(3, _num("min_support", 3, int)) if risk_limited
            else max(1, _num("min_support", 3, int))),
        "min_support_by_class": by_class,
        "require_held_out": (
            True if risk_limited else _strict_config_bool(
                cfg, "require_held_out", False)),
        "min_held_out": (
            max(8, _num("min_held_out", 8, int)) if risk_limited
            else max(0, _num("min_held_out", 0, int))),
        "min_delta": (
            max(0.02, _num("min_delta", 0.02, float)) if risk_limited
            else max(0.0, _num("min_delta", 0.0, float))),
        "confidence_z": (
            max(1.96, _num("confidence_z", 1.96, float)) if risk_limited
            else max(0.0, _num("confidence_z", 0.0, float))),
        "max_cost_factor": _risk_cap("max_cost_factor", 1.25),
        "max_latency_factor": _risk_cap("max_latency_factor", 1.25),
        "max_tool_calls_factor": _risk_cap("max_tool_calls_factor", 1.10),
        "semantic_mining": _strict_config_bool(cfg, "semantic_mining", False),
        # Keep the risk study's search multiplicity fixed. More proposals are
        # not a tighter control: they increase selection pressure and compute.
        "candidates_per_signature": (
            3 if risk_limited else max(
                1, _num("candidates_per_signature", 1, int))),
        # A risk-limited pass may activate at most one isolated winner.  A
        # second candidate must be evaluated in a new cycle against a freshly
        # snapshotted deployed prompt, rather than being composed onto a stale
        # baseline that never included the first winner.
        "max_promotions_per_cycle": (
            1 if risk_limited else max(
                0, _num("max_promotions_per_cycle", 0, int))),
        # Cross-validate a candidate across this many holdout-fold rotations
        # before promoting (1 = single fixed split, the historical behavior; >1
        # requires the lift to generalize across every rotation).
        "holdout_rotations": (
            1 if risk_limited else max(
                1, _num("holdout_rotations", 1, int))),
        # A sealed holdout is a consumable statistical resource. Risk-limited
        # evaluation requires an explicitly provisioned durable query ledger;
        # runtime code never creates it because deletion must not reset budget.
        "holdout_ledger": (str(cfg["holdout_ledger"])
                           if cfg.get("holdout_ledger") else None),
        "holdout_family_alpha": family_alpha,
        "holdout_query_alpha": query_alpha,
        "holdout_max_queries": holdout_max_queries,
        # Self-consistency for the LLM-as-judge in the auto-built evaluator: ask
        # the judge this many times (diverse framings) and take the majority vote
        # (1 = single call, the historical behavior).
        "judge_samples": (
            max(3, _num("judge_samples", 3, int)) if risk_limited
            else max(1, _num("judge_samples", 1, int))),
        # Feed the auto-built LLM judge's verdicts into the calibration
        # interlock: each corpus-labeled verdict is recorded as a
        # (vote-share confidence, agrees-with-label) sample, so
        # calibration.learning_frozen can detect THIS loop's judge drifting.
        # Default off -- telemetry only, never load-bearing.
        "calibrate_judge": (
            True if risk_limited else _strict_config_bool(
                cfg, "calibrate_judge", False)),
        # A conservative run requires a recent adequate calibration receipt;
        # collecting judge telemetry alone is not an interlock.
        "calibration_max_age_hours": calibration_max_age,
        # Where the learning stores (addenda and provenance) live: "files" =
        # local JSON under ~/.maverick (default); "world" = durable world DB
        # tables. Unknown values mean "files".
        "store": (lambda v: v if v in ("files", "world") else "files")(
            str(cfg.get("store", "files")).strip().lower()),
        # Corpus bootstrapping from hindsight pairs (a failed goal whose wording
        # later ran to done): "propose" stages candidates for operator review
        # (`self-harness corpus review`); "auto" merges them straight into the
        # live corpus. Anything else = "off" -- the corpus stays hand-authored.
        "corpus_harvest": (lambda v: v if v in ("propose", "auto") else "off")(
            str(cfg.get("corpus_harvest", "off")).strip().lower()),
        # Relapse recency guard: re-probate (back to canary) a graduated line
        # whose recent-outcomes window carries at least this failing share
        # (0 = off, the default; clamped to (0, 1]). relapse_min_outcomes is
        # the evidence floor before the window is judged at all.
        "relapse_failure_share": (lambda v: v if v is not None and 0 < v <= 1 else 0.0)(
            _factor("relapse_failure_share")),
        "relapse_min_outcomes": max(1, _num("relapse_min_outcomes", 5, int)),
        # Metamorphic validation on the auto path: paraphrase the held-out cases
        # (the selected run model acting as summarizer) and require the
        # candidate's lift to survive
        # the rewording -- rejects lines overfit to exact phrasing. Default off;
        # tolerance allows a small slip on the paraphrases.
        "metamorphic": (
            True if risk_limited else _strict_config_bool(
                cfg, "metamorphic", False)),
        "metamorphic_tolerance": (
            0.0 if risk_limited
            else max(0.0, _num("metamorphic_tolerance", 0.0, float))),
        "mine_bucket_by": bucket_by,
        # Stage every promotion as a CANARY (on probation): still recalled, but
        # graduated to permanent / demoted from real run outcomes by the cycle's
        # canary review, rather than permanent on arrival (default off -- a
        # promotion lands permanent, the historical behavior).
        "promote_as_canary": (
            True if risk_limited else _strict_config_bool(
                cfg, "promote_as_canary", False)),
        "retire_after_days": max(0.0, _num("retire_after_days", 0.0, float)),
        # Re-measure each existing line's live A/B lift each cycle and DEMOTE
        # dead weight (default off -- a general corpus can under-credit a line
        # that helps only its own failure class, so this is operator opt-in).
        "efficacy_review": _strict_config_bool(cfg, "efficacy_review", False),
        # Path to an eval corpus ({model|domain: [{goal, expected}]}) JSON. When
        # set, the driver auto-builds the LIVE A/B (model generates, verifier
        # judges) so a scheduled pass can actually PROMOTE; unset -> dry pass.
        "eval_corpus": (str(cfg["eval_corpus"]) if cfg.get("eval_corpus") else None),
        # Spend ceiling (in dollars) for ONE auto-evaluated cycle's LLM calls
        # (runner + judge share the pot; kernel rule 3). None/<=0 = uncapped,
        # the historical behavior. An exhausted budget fails the evaluation
        # CLOSED -- candidates are rejected, never promoted on partial scores.
        "eval_budget_dollars": eval_budget,
        "eval_budget_valid": eval_budget_valid,
    }


def get_autonomy() -> dict:
    """Return the ``[autonomy]`` section with defaults filled in.

    The autonomy gate (``maverick.autonomy``) is OFF by default so the kernel
    runs unchanged out of the box. When enabled, ``tighten_on_low_trust`` drops
    the effective risk ceiling for high-risk tools when run trust is low.
    ``disagreement_high`` and ``min_confidence`` are clamped to [0, 1].
    """
    cfg = load_config().get("autonomy", {})

    def _clamp01(key: str, default: float) -> float:
        try:
            v = float(cfg.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(0.0, min(1.0, v))

    return {
        "enable": bool(cfg.get("enable", False)),
        "min_confidence": _clamp01("min_confidence", 0.5),
        "disagreement_high": _clamp01("disagreement_high", 0.5),
        "tighten_on_low_trust": bool(cfg.get("tighten_on_low_trust", True)),
        # Independent axis (resolved without ``enable``): assume-and-proceed
        # instead of blocking on ``ask_user`` when no human can answer
        # (headless / batch / benchmark runs). Default off.
        "headless_assume": bool(cfg.get("headless_assume", False)),
    }


def get_calibration() -> dict:
    """Return the ``[calibration]`` section with defaults filled in.

    The verifier-calibration interlock (``maverick.calibration``) is OFF by
    default: ``enforce`` must be true for a failed assessment to freeze
    self-improvement. ``min_samples`` is the minimum
    labeled samples before an assessment is trusted; ``min_discrimination`` is
    the floor on mean(confidence|correct) - mean(confidence|incorrect) below
    which the verifier is judged to have drifted.
    """
    cfg = load_config().get("calibration", {})

    def _int(key: str, default: int) -> int:
        try:
            return max(1, int(cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    try:
        min_disc = float(cfg.get("min_discrimination", 0.15))
    except (TypeError, ValueError):
        min_disc = 0.15
    try:
        min_res = float(cfg.get("min_resistance", 0.0))
    except (TypeError, ValueError):
        min_res = 0.0
    return {
        "enforce": bool(cfg.get("enforce", False)),
        "min_samples": _int("min_samples", 20),
        "min_discrimination": max(0.0, min(1.0, min_disc)),
        "collect_from_coding": bool(cfg.get("collect_from_coding", False)),
        # Reward-laundering interlock floor: freeze learning when the verifier's
        # edge on adversarial probes falls below this fraction of its edge on
        # natural traffic. 0.0 = advisory (measured, never freezes).
        "min_resistance": max(0.0, min(1.0, min_res)),
    }


def get_credit() -> dict:
    """Return the ``[credit]`` section (counterfactual swarm credit assignment).

    ON by default but budget-limited: CSCA costs N+1 verifier passes per swarm. ``max_children``
    caps the swarm size it will attribute (skip larger ones); ``min_budget_
    headroom`` is the fraction of budget that must remain before it runs.
    """
    cfg = load_config().get("credit", {})
    try:
        maxc = max(2, int(cfg.get("max_children", 6)))
    except (TypeError, ValueError):
        maxc = 6
    try:
        head = float(cfg.get("min_budget_headroom", 0.4))
    except (TypeError, ValueError):
        head = 0.4
    return {
        "enable": _strict_config_bool(
            cfg, "enable", governed_learning_default()),
        "max_children": maxc,
        "min_budget_headroom": max(0.0, min(1.0, head)),
    }


def get_adaptive_compute() -> dict:
    """Return the ``[adaptive_compute]`` section (SOTA: spend compute on
    uncertainty). OFF by default. ``low_uncertainty`` is the threshold below
    which fan-out width is scaled down; ``min_width`` is the floor."""
    cfg = load_config().get("adaptive_compute", {})
    try:
        low = float(cfg.get("low_uncertainty", 0.2))
    except (TypeError, ValueError):
        low = 0.2
    try:
        minw = max(1, int(cfg.get("min_width", 1)))
    except (TypeError, ValueError):
        minw = 1
    return {
        "enable": bool(cfg.get("enable", False)),
        "low_uncertainty": max(0.0, min(1.0, low)),
        "min_width": minw,
    }


def get_search() -> dict:
    """Return the ``[search]`` section (verifier-guided best-of-N generation).
    OFF by default. ``n`` is the number of candidate answers to sample."""
    cfg = load_config().get("search", {})
    try:
        n = max(1, int(cfg.get("n", 3)))
    except (TypeError, ValueError):
        n = 3
    return {"enable": bool(cfg.get("enable", False)), "n": n}


def get_memory() -> dict:
    """Return the ``[memory]`` section. ``temporal`` keeps a bitemporal history
    of every fact value (validity windows) instead of overwriting, so the
    Operating Record can answer "what did we believe on date X, and why".
    OFF by default (one extra append per fact change); the live-value read path
    is unchanged when off. Also honored via ``MAVERICK_TEMPORAL_MEMORY=1``."""
    cfg = load_config().get("memory", {})
    return {"temporal": bool(cfg.get("temporal", False))}


def get_memory_guard() -> dict:
    """Return the ``[memory_guard]`` section (OWASP ASI06 controls). OFF by
    default. When on, every memory write is screened for injection/poisoning and
    stamped with provenance + a trust tier, and memory below ``min_recall_trust``
    is filtered out of the agent's standing brief (trust-aware retrieval).
    ``min_recall_trust`` is a :class:`maverick.memory_guard.TrustTier` value
    (default 1 = drop only EXTERNAL/untrusted memory). Also honored via
    ``MAVERICK_MEMORY_GUARD=1``."""
    cfg = load_config().get("memory_guard", {})
    return {
        "enable": bool(cfg.get("enable", False)),
        "min_recall_trust": int(cfg.get("min_recall_trust", 1)),
    }


def get_fairness_monitor() -> dict:
    """Return the ``[fairness_monitor]`` section (continuous group-fairness
    monitoring, ISO/IEC 42001 A.6.2.6). OFF by default: a deployment opts in by
    feeding decision outcomes to :class:`maverick.fairness_monitor.FairnessMonitor`.
    ``threshold`` is the four-fifths cutoff, ``window`` the rolling number of
    outcomes retained, ``min_samples`` the noise floor before evaluating, and
    ``drift_tolerance`` how far the minimum impact ratio may fall below baseline
    before a drift alert. Also honored via ``MAVERICK_FAIRNESS_MONITOR=1``."""
    cfg = load_config().get("fairness_monitor", {})
    enabled = bool(cfg.get("enable", False))
    flag = env_flag("MAVERICK_FAIRNESS_MONITOR")
    if flag is not None:
        enabled = flag
    return {
        "enable": enabled,
        "threshold": float(cfg.get("threshold", 0.8)),
        "window": int(cfg.get("window", 1000)),
        "min_samples": int(cfg.get("min_samples", 30)),
        "drift_tolerance": float(cfg.get("drift_tolerance", 0.1)),
    }


def get_domains() -> dict:
    """Return the ``[domains]`` section with defaults filled in.

    ``discipline`` appends the per-suite operating-discipline block to every
    domain pack's persona at spawn (ON by default — it is pack content, not a
    new capability; see :mod:`maverick.domain_discipline`). ``memory`` injects
    the department's recalled lessons into a specialist's brief at spawn —
    only meaningful when reflexion/dreaming are themselves enabled.
    """
    cfg = load_config().get("domains", {})
    return {
        "discipline": bool(cfg.get("discipline", True)),
        "memory": bool(cfg.get("memory", True)),
    }


def get_experience() -> dict:
    """Return the ``[experience]`` section (outcome-guided orchestration).
    ON by default."""
    cfg = load_config().get("experience", {})
    return {"enable": _strict_config_bool(
        cfg, "enable", governed_learning_default())}


def get_dreaming() -> dict:
    """Return the ``[dreaming]`` section (offline experience consolidation).

    ON by default. ``min_cluster`` is the evidence floor before a recurring
    pattern is consolidated (a one-off is noise); ``max_insights`` caps the
    persisted insight store; ``prune`` lets a dream cycle compact the
    reflexion log down to ``keep_reflexions`` deduplicated entries.
    """
    cfg = load_config().get("dreaming", {})

    def _int(key: str, default: int) -> int:
        try:
            return max(1, int(cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _ratio(key: str, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    def _nonneg_int(key: str, default: int) -> int:
        try:
            return max(0, int(cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _nonneg_float(key: str, default: float) -> float:
        try:
            return max(0.0, float(cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    return {
        "enable": _strict_config_bool(
            cfg, "enable", governed_learning_default()),
        "min_cluster": _int("min_cluster", 2),
        "max_insights": _int("max_insights", 100),
        "prune": bool(cfg.get("prune", True)),
        "keep_reflexions": _int("keep_reflexions", 500),
        # Shared promotion is disabled by default: department-scoped failures
        # must not be written into globally recallable insights.
        "promote_shared": bool(cfg.get("promote_shared", False)),
        # Insights unconfirmed for this many days retire; 0 = never expire.
        "insight_ttl_days": _nonneg_int("insight_ttl_days", 90),
        # Retire a failure insight once this many NEWER similar successes
        # contradict it ("we now reliably do X").
        "contradiction_successes": _int("contradiction_successes", 2),
        # Fact consolidation deletes operator data, so it remains a separate
        # opt-in inside default-on dreaming.
        "prune_facts": bool(cfg.get("prune_facts", False)),
        "facts_max_age_days": _nonneg_int("facts_max_age_days", 180),
        "facts_cap": _nonneg_int("facts_cap", 2000),
        # Distill explicit user-preference statements into per-user notes.
        # Verbatim cross-conversation preference persistence is a separate
        # privacy decision, not inherited from default-on consolidation.
        "user_notes": bool(cfg.get("user_notes", False)),
        # Quarantine this cycle's NEW skills while the continuously-tracked
        # benchmark suite is regressing (learning-side canary).
        "benchmark_gate": bool(cfg.get("benchmark_gate", True)),
        # Learning rollback: snapshot every learned store before a CLI dream
        # cycle mutates it, keeping the last N snapshots.
        "snapshots": bool(cfg.get("snapshots", True)),
        "snapshot_keep_last": _int("snapshot_keep_last", 5),
        # Dream-time rehearsal is a separate trust
        # decision from consolidation: it spends real agent runs. Default off.
        "rehearse": bool(cfg.get("rehearse", False)),
        "max_rehearsals": _int("max_rehearsals", 3),
        # Forgetting loop: retire learned skills whose recall track record
        # decayed below the floor (after enough attempts to judge).
        "retire_skills": bool(cfg.get("retire_skills", True)),
        "retire_min_uses": _int("retire_min_uses", 5),
        "retire_below": _ratio("retire_below", 0.25),
        # LLM-in-the-loop consolidation: when on (and a provider/key is
        # configured), the cheap "summarizer" role rewrites each clustered
        # failure into a transferable lesson instead of the deterministic
        # template. Inputs AND the model's output are secret-redacted +
        # Shield-scanned, the call is budget-metered, and it FAILS OPEN to the
        # deterministic text. OFF by default -- the LLM-free path is the
        # injection-safe baseline. `llm_consolidation_budget` caps the spend
        # one dream cycle may use enriching insights.
        "llm_consolidation": bool(cfg.get("llm_consolidation", False)),
        "llm_consolidation_budget": _nonneg_float(
            "llm_consolidation_budget", 1.0
        ),
    }


def get_self_improvement() -> dict:
    """Return the ``[self_improvement]`` section (governed learning promotion).

    ON by default for governed prompt/policy learning. When ``enable`` is false,
    the Self-Improvement Controller never promotes a self-change. ``min_improvement``
    is the eval margin a candidate
    must beat its own baseline by before any rung is eligible; ``max_auto_rung``
    is the highest rung that may be promoted without a human (anything above it
    -- e.g. ``code``/``weights`` -- always requires explicit approval).
    """
    cfg = load_config().get("self_improvement", {})

    raw_margin = cfg.get("min_improvement", 0.0)
    margin_valid = (
        isinstance(raw_margin, (int, float))
        and not isinstance(raw_margin, bool)
        and math.isfinite(float(raw_margin))
        and 0.0 <= float(raw_margin) <= 1.0
    )
    # A negative sentinel is intentionally outside the controller's accepted
    # domain.  A present malformed policy must freeze promotion, never collapse
    # to the permissive zero-margin default used only when the key is absent.
    min_improvement = float(raw_margin) if margin_valid else -1.0

    def _ratio(key: str, default: float) -> float:
        try:
            return max(0.0, float(cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    raw_keys = cfg.get("approver_keys")
    keys_valid = (
        "approver_keys" not in cfg
        or (
            isinstance(raw_keys, list)
            and all(isinstance(key, str) for key in raw_keys)
        )
    )
    approver_keys = (
        [key.strip() for key in raw_keys if key.strip()]
        if keys_valid and isinstance(raw_keys, list) else [])
    raw_keys_dir = cfg.get("approver_keys_dir")
    keys_dir_valid = (
        "approver_keys_dir" not in cfg
        or raw_keys_dir is None
        or isinstance(raw_keys_dir, str)
    )
    require_signed_valid = (
        "require_signed_approval" not in cfg
        or isinstance(cfg.get("require_signed_approval"), bool)
    )

    return {
        "enable": _strict_config_bool(
            cfg, "enable", governed_learning_default()),
        "min_improvement": min_improvement,
        "promotion_policy_valid": margin_valid,
        "max_auto_rung": str(cfg.get("max_auto_rung", "policy")).strip().lower() or "policy",
        # Phase-0 capture fuels the default-on flywheel. Provider egress
        # remains a separate opt-in.
        "capture": _strict_config_bool(cfg, "capture", True),
        "prm_guidance": _strict_config_bool(
            cfg, "prm_guidance", governed_learning_default()),
        # Counterfactual promotion: judge a self-change on its confounder-adjusted
        # CAUSAL effect (maverick.promotion_effect) rather than a correlational
        # baseline/candidate diff. On by default; set false to use the direct
        # baseline/candidate evidence path.
        "causal_promotion": _strict_config_bool(
            cfg, "causal_promotion", True, invalid=True),
        # Evaluator co-evolution (maverick.evaluator_evolution): instead of only
        # FREEZING learning when the judge drifts, promote a better judge --
        # a challenger evaluator replaces the incumbent only when its agreement
        # with a fixed ground-truth ANCHOR (by the epsilon-best-belief lower bound)
        # beats it, on the dedicated ``evaluator`` rung. On by default; the
        # anchor is the guardrail (immutable, checksum-locked) so a weak anchor
        # cannot launder drift. ``evaluator_eps`` is the confidence level of the
        # lower bound (lower = more conservative). After arXiv 2606.26294.
        "evaluator_evolution": _strict_config_bool(
            cfg, "evaluator_evolution", True),
        "evaluator_eps": _ratio("evaluator_eps", 0.05),
        # Cryptographic approval of code/weights promotions
        # (maverick.approval_signing): trusted approver Ed25519 public keys (hex)
        # and an optional dir of raw ``*.pub`` files, plus a strict flag that
        # requires a signed approval even before keys are provisioned. Empty
        # (default) keeps the legacy self-settable boolean-approval path.
        "approver_keys": approver_keys,
        "approver_keys_dir": (
            raw_keys_dir.strip() or None
            if isinstance(raw_keys_dir, str) else None
        ),
        "approval_policy_valid": (
            keys_valid and keys_dir_valid and require_signed_valid
        ),
        "require_signed_approval": _strict_config_bool(
            cfg, "require_signed_approval", False, invalid=True),
    }


def get_reasoning_reward() -> dict:
    """Return the ``[reasoning_reward]`` section (structured, auditable rewards).

    ON by default: the verifier judges FINAL answers with the multi-faceted
    rubric reward (maverick.reasoning_reward, Agent-RRM) -- per-dimension scores
    with a safety veto, attached to the verdict for the learning audit -- and
    falls back to the scalar verdict for any non-rubric reply. Set
    ``enable = false`` (or MAVERICK_REASONING_REWARD=0) to force the scalar
    verifier."""
    cfg = load_config().get("reasoning_reward", {})
    return {
        "enable": _strict_config_bool(
            cfg, "enable", governed_learning_default()),
        # Sign each structured reward into the tamper-evident audit chain
        # (provable learning). ON by default; set false to suppress these audit
        # rows. See reasoning_reward.audit_rewards_enabled.
        "audit_rewards": _strict_config_bool(
            cfg, "audit_rewards", governed_learning_default()),
    }


def get_jit_rl() -> dict:
    """Return the ``[jit_rl]`` section (gradient-free test-time adaptation).

    ON by default, but a no-op with a cold store: it records verifier-guided
    best-of-N outcomes and factors learned advantage into candidate selection
    (maverick.jit_rl), reducing to the pure verifier choice until experience
    accumulates. ``k`` is the k-NN neighbourhood size and ``beta`` the steering
    gain; ``max_experiences`` bounds the (state, action, return) buffer. Defaults
    mirror ``jit_rl.DEFAULT_K`` / ``DEFAULT_BETA`` / ``DEFAULT_MAX_EXPERIENCES``
    (kept literal here to avoid a config->jit_rl import cycle). Set
    ``enable = false`` (or MAVERICK_JIT_RL=0) to disable."""
    cfg = load_config().get("jit_rl", {})

    def _int(key: str, default: int) -> int:
        try:
            return max(1, int(cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    try:
        beta = float(cfg.get("beta", 1.0))
    except (TypeError, ValueError):
        beta = 1.0
    return {
        "enable": _strict_config_bool(
            cfg, "enable", governed_learning_default()),
        "k": _int("k", 8),
        "beta": beta,
        "max_experiences": _int("max_experiences", 5000),
    }


def get_rehearsal() -> dict:
    """Return the ``[rehearsal]`` section (pre-execution rehearsal gate).

    The governance half of the Operating Twin: before a risky plan executes, it
    is simulated against the learned world-model and gated on the prediction.
    ON by default and uncertainty escalates. When explicitly disabled,
    ``gate_action`` is a no-op that proceeds. ``outcome_floor``
    is the predicted-outcome below which a *confident* rehearsal blocks;
    ``min_support`` is the (state, action) observations the model needs before it
    will vouch for a move (below it the action is escalated, never waved through);
    ``max_uncertainty`` escalates an over-uncertain rollout.
    """
    cfg = load_config().get("rehearsal", {})

    def _num(key: str, default: float, cast=float):
        try:
            return cast(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "enable": _strict_config_bool(
            cfg, "enable", governed_learning_default()),
        "outcome_floor": _num("outcome_floor", 0.5),
        "min_support": _num("min_support", 5, int),
        "max_uncertainty": _num("max_uncertainty", 0.25),
        "horizon": _num("horizon", 8, int),
        "rollouts": _num("rollouts", 200, int),
    }


def get_data_engine() -> dict:
    """Return the ``[data_engine]`` section (the Cognitive Data Engine).

    The governed improvement flywheel for the firm: production
    failures are causally triaged, a fix is mined + validated in the world-model,
    promoted through the safety ladder, and measured against real outcomes. ON
    by default; empty or insufficient evidence produces no candidate.
    ``failure_threshold`` is the outcome below which an
    episode counts as a failure; ``min_support`` is the evidence a causal-impact
    estimate needs before a failure class is ranked.
    """
    cfg = load_config().get("data_engine", {})

    def _num(key: str, default: float, cast=float):
        try:
            return cast(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "enable": _strict_config_bool(
            cfg, "enable", governed_learning_default()),
        "failure_threshold": _num("failure_threshold", 0.5),
        "min_support": _num("min_support", 8, int),
        "top_k": _num("top_k", 10, int),
    }


def get_consequence() -> dict:
    """Return the ``[consequence]`` section (the Consequence Engine).

    Reality as the reward: real downstream outcomes (invoice paid, contract
    renewed, ticket stayed closed), reported by a system-of-record connector and
    joined back to the episode that acted, become the grounded learning signal.
    ON by default -- when ``enable`` is false the data-engine join keeps using
    the verifier-confidence proxy, so an opted-out deployment is unaffected;
    recording outcomes is always harmless (it just stores).
    """
    cfg = load_config().get("consequence", {})
    return {"enable": _strict_config_bool(
        cfg, "enable", governed_learning_default())}


def get_deployment() -> dict:
    """Return the ``[deployment]`` section (install provenance).

    Written by the installer wizard to record where Maverick runs
    (local/docker/vps). Read back so a re-run of ``maverick init``
    can default to the prior choice. ``type`` is empty when never recorded.
    """
    cfg = load_config().get("deployment", {}) or {}
    return {"type": str(cfg.get("type", "")).strip()}


def get_flows() -> dict:
    """Return the ``[flows]`` section (the flow engine).

    A flow is a deterministic control-flow skeleton (branch / foreach / parallel /
    approval / delay) over agentic + tool steps -- the "do both" model that keeps
    a migrated workflow's structure while any node can still be a full agentic
    goal. OFF by default -- when ``enable`` is false the engine never runs, so a
    default deployment is unaffected.
    """
    cfg = load_config().get("flows", {})
    try:
        max_node_retries = int(cfg.get("max_node_retries", 5) or 5)
    except (TypeError, ValueError):
        max_node_retries = 5
    return {
        "enable": bool(cfg.get("enable", False)),
        # Opt-in autonomous self-correction: a scheduled pass reverts an applied
        # node-kind rewrite whose grounded outcomes measurably regressed. OFF by
        # default -- surfacing proposals + human apply works without it; this only
        # governs whether the loop is allowed to *undo* a bad change on its own.
        "auto_evolve": bool(cfg.get("auto_evolve", False)),
        # Opt-in autonomous forward apply: the loop enacts a self-rewrite proposal
        # (not just reverts a bad one). Strictly gated -- auto_apply_enabled() also
        # requires auto_evolve on for the revert safety net. OFF by default. Without
        # this key here auto_apply could never be read from config (only the env
        # var / a monkeypatch), so the [flows] auto_apply knob was inert.
        "auto_apply": bool(cfg.get("auto_apply", False)),
        # Per-node action/agent retries are useful for transient failures, but
        # must stay bounded because every retry may invoke tools/providers. Env
        # MAVERICK_FLOW_MAX_NODE_RETRIES can override this at runtime; the flow
        # IR also hard-caps the effective value defensively.
        "max_node_retries": max_node_retries,
    }


def get_operations_scientist() -> dict:
    """Return the ``[operations_scientist]`` section (the discovery engine).

    An agent that discovers a better process and proves it causally: it pairs a
    harmful action with the beneficial habit that should replace it, validates the
    swap in the world-model, then (downstream) runs a real experiment and ships
    the proven win. ON by default; it remains evidence- and promotion-gated.
    """
    cfg = load_config().get("operations_scientist", {})
    return {"enable": _strict_config_bool(
        cfg, "enable", governed_learning_default())}


def get_emergent_protocol() -> dict:
    """Return the ``[emergent_protocol]`` section (learned coordination shorthand).

    The swarm evolves short codes for the boilerplate it repeats, paying frontier
    tokens only for what's new -- but every code decodes exactly back to English
    (the auditable translation layer). OFF by default -- when ``enable`` is false
    the codec is the identity transform, so a default deployment is unaffected.
    """
    cfg = load_config().get("emergent_protocol", {})
    return {"enable": bool(cfg.get("enable", False))}


def get_emergent_codec() -> dict:
    """Return the ``[emergent_codec]`` section (live token-aware compression).

    The token-aware codec (``maverick.emergent_tokens``) is the implementation that
    actually saves *frontier tokens*, not just bytes -- byte-stuffed ~2-token codes
    instead of the ~5-token sentinels. When ``enable`` is true the blackboard
    measures, on the real coordination stream, what the codec would save (telemetry
    only -- the rendered text agents see is unchanged, so the audit/Shield path is
    untouched). OFF by default: a default deployment measures nothing and pays
    nothing. Flipping agents to actually *read* codes is a separate, stricter step.
    """
    cfg = load_config().get("emergent_codec", {})
    return {"enable": bool(cfg.get("enable", False))}


def get_durable() -> dict:
    """Return the ``[durable]`` section with defaults filled in.

    Durable execution (checkpoint/resume) is OFF by default so the kernel
    keeps current warm-restart behavior out of the box. ``keep_last`` caps how
    many checkpoints are retained per agent for rewind/history.
    """
    cfg = load_config().get("durable", {})
    try:
        keep = int(cfg.get("keep_last", 5))
    except (TypeError, ValueError):
        keep = 5
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "keep_last": max(1, keep),
    }


def get_assessments() -> dict:
    """Return the retained local assessment-memory setting.

    ``learn`` enables advisory suggestions distilled from the firm's own past
    assessments. It never bypasses the human review gate. The removed connected
    document-discovery plane has no configuration fallback.
    """
    import os
    cfg = load_config().get("assessments", {}) or {}
    disabled = os.environ.get("MAVERICK_ASSESS_LEARN", "").strip().lower()
    return {"learn": bool(cfg.get("learn", True))
            and disabled not in ("0", "false", "no")}

# End retained assessment settings.
