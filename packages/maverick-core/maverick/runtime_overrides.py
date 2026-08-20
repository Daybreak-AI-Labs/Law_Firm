"""Dashboard-owned runtime overrides.

The dashboard's permissions page lets a user disable a tool with one
click. Writing that into ``config.toml`` would clobber the user's
hand-tuned, comment-annotated, wizard-generated file. Instead the
dashboard owns a separate ``~/.maverick/runtime-overrides.toml`` that
the kernel unions into the deny-list at registry-build time.

Only a small, well-defined surface lives here today:

    [security]
    denied_tools = ["computer", "browser"]

``tool_acl.resolve_lists`` reads ``denied_tools`` and unions it with
the config + channel + user deny-lists, so a disable takes effect on
the next goal with no restart. config.toml is never touched.
"""
from __future__ import annotations

import copy
import functools
import logging
import math
import re
import threading
from collections import OrderedDict
from pathlib import Path

from .paths import data_dir

log = logging.getLogger(__name__)

OVERRIDES_PATH = data_dir("runtime-overrides.toml")
_VALID_TOOL_NAME = re.compile(r"^[a-z0-9_-]+$")

# One overlay file holds every retained surface (denied_tools, model, budget,
# allowed_models, style); each mutator re-reads the whole overlay and
# rewrites it. Two dashboard requests racing would otherwise lose one change --
# e.g. a set_budget that read a stale denied_tools drops a just-applied
# disable_tool, silently re-enabling a tool the operator just denied. Serialize
# every mutator's read-modify-write in-process (RLock) and cross-process (flock).
_OVERRIDE_LOCK = threading.RLock()
_mutation_context = threading.local()
_LKG_LIMIT = 8
_LOAD_FAILURE_LIMIT = 32
_MAX_OVERRIDE_BYTES = 1024 * 1024
_KNOWN_TOP_LEVEL = frozenset({"security", "models", "budget", "access", "styles"})
_last_known_good: OrderedDict[str, dict] = OrderedDict()
_announced_load_failures: OrderedDict[tuple[str, str], None] = OrderedDict()


class RuntimeOverridesSecurityError(RuntimeError):
    """The operator policy exists but cannot be read or validated safely."""


def _serialized(fn):
    """Serialize a mutator's whole read-modify-write of the overlay file. The
    public mutators never call one another, so the non-reentrant flock can't
    self-deadlock."""
    @functools.wraps(fn)
    def _wrap(*args, **kwargs):
        from .file_lock import cross_process_lock
        with _OVERRIDE_LOCK, cross_process_lock(OVERRIDES_PATH):
            previous = getattr(_mutation_context, "strict_reads", False)
            _mutation_context.strict_reads = True
            try:
                return fn(*args, **kwargs)
            finally:
                _mutation_context.strict_reads = previous
    return _wrap


def _tomllib():
    try:
        import tomllib  # 3.11+
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib


def _cache_key() -> str:
    """Stable lexical identity for the policy path.

    Resolving symlinks makes the key depend on whether the link currently
    exists: after a valid symlink-backed policy is loaded, deleting the link
    would otherwise change the key and bypass its last-known-good policy.
    """
    try:
        return str(OVERRIDES_PATH.absolute())
    except OSError:
        return str(OVERRIDES_PATH)


def _remember_valid(key: str, state: dict) -> dict:
    """Cache a bounded, detached last-known-good policy for transient damage."""
    detached = copy.deepcopy(state)
    _last_known_good[key] = detached
    _last_known_good.move_to_end(key)
    while len(_last_known_good) > _LKG_LIMIT:
        _last_known_good.popitem(last=False)
    return copy.deepcopy(detached)


def _read_overlay(path: Path) -> dict:
    with open(path, "rb") as f:
        content = f.read(_MAX_OVERRIDE_BYTES + 1)
    if len(content) > _MAX_OVERRIDE_BYTES:
        raise ValueError(
            f"runtime override exceeds {_MAX_OVERRIDE_BYTES} bytes"
        )
    return _tomllib().loads(content.decode("utf-8"))


def _require_table(state: dict, name: str) -> dict:
    value = state.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a TOML table")
    return value


def _reject_unknown_keys(table: dict, allowed: set[str] | frozenset[str], label: str) -> None:
    unknown = sorted(str(key) for key in set(table) - set(allowed))
    if unknown:
        raise ValueError(f"{label} contains unknown key(s): {', '.join(unknown)}")


def _require_string_list(
    table: dict,
    key: str,
    pattern: re.Pattern[str],
    *,
    label: str,
) -> None:
    if key not in table:
        return
    values = table[key]
    if not isinstance(values, list):
        raise ValueError(f"{label} must be a list")
    for value in values:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"{label} contains an invalid entry")


def _validate_loaded_state(state: object) -> dict:
    """Validate every dashboard-owned surface before it can affect policy.

    A syntactically valid but structurally damaged security table must not be
    treated as an empty deny/allow list.  Validating all known sections also
    prevents a dashboard mutation from silently discarding malformed state.
    Unknown top-level sections are rejected so misspelled security controls
    cannot silently become inert.
    """
    if not isinstance(state, dict):
        raise ValueError("runtime override root must be a TOML table")
    _reject_unknown_keys(state, _KNOWN_TOP_LEVEL, "runtime override")

    security = _require_table(state, "security")
    _reject_unknown_keys(security, {"denied_tools"}, "security")
    _require_string_list(
        security,
        "denied_tools",
        _VALID_TOOL_NAME,
        label="security.denied_tools",
    )

    models = _require_table(state, "models")
    _reject_unknown_keys(models, {"default"}, "models")
    if "default" in models:
        model = models["default"]
        if (
            not isinstance(model, str)
            or not _VALID_MODEL.fullmatch(model.strip())
            or ":" not in model
            or not all(part.strip() for part in model.split(":", 1))
        ):
            raise ValueError("models.default must be an exact provider:model")

    budget = _require_table(state, "budget")
    _reject_unknown_keys(budget, {"max_dollars"}, "budget")
    if "max_dollars" in budget:
        value = budget["max_dollars"]
        if isinstance(value, bool):
            raise ValueError("budget.max_dollars must be a positive number")
        try:
            amount = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("budget.max_dollars must be a positive number") from exc
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("budget.max_dollars must be a positive number")

    access = _require_table(state, "access")
    _reject_unknown_keys(access, {"allowed_models"}, "access")
    _require_string_list(
        access,
        "allowed_models",
        _VALID_MODEL,
        label="access.allowed_models",
    )
    for model in access.get("allowed_models", []):
        if ":" not in model or not all(
            part.strip() for part in model.split(":", 1)
        ):
            raise ValueError(
                "access.allowed_models entries must be exact provider:model specs"
            )

    styles = _require_table(state, "styles")
    _reject_unknown_keys(styles, {"active"}, "styles")
    if "active" in styles and (
        not isinstance(styles["active"], str) or not styles["active"].strip()
    ):
        raise ValueError("styles.active must be a non-empty string")

    return state


def _load() -> dict:
    key = _cache_key()
    try:
        # lstat distinguishes a genuinely absent policy from a dangling or
        # unreadable symlink, which must fail closed rather than look empty.
        OVERRIDES_PATH.lstat()
    except FileNotFoundError:
        with _OVERRIDE_LOCK:
            retained = key in _last_known_good
            for failure in tuple(_announced_load_failures):
                if failure[0] == key:
                    _announced_load_failures.pop(failure, None)
        if retained:
            return _recover_or_raise(
                key,
                FileNotFoundError(
                    "operator policy disappeared after a valid policy was loaded"
                ),
            )
        return {}
    except OSError as exc:
        return _recover_or_raise(key, exc)
    try:
        state = _validate_loaded_state(_read_overlay(OVERRIDES_PATH))
    except Exception as exc:
        # Every read and schema-validation failure has the same fail-closed
        # contract. Critical consumers special-case
        # RuntimeOverridesSecurityError; leaking a KeyError/TypeError here lets
        # their generic resilience catches silently discard the entire policy.
        return _recover_or_raise(key, exc)
    with _OVERRIDE_LOCK:
        for failure in tuple(_announced_load_failures):
            if failure[0] == key:
                _announced_load_failures.pop(failure, None)
        return _remember_valid(key, state)


def _recover_or_raise(key: str, exc: Exception) -> dict:
    with _OVERRIDE_LOCK:
        if (
            key in _last_known_good
            and not getattr(_mutation_context, "strict_reads", False)
        ):
            state = copy.deepcopy(_last_known_good[key])
            _last_known_good.move_to_end(key)
            fingerprint = (key, f"{type(exc).__name__}: {exc}")
            if fingerprint not in _announced_load_failures:
                _announced_load_failures[fingerprint] = None
                _announced_load_failures.move_to_end(fingerprint)
                while len(_announced_load_failures) > _LOAD_FAILURE_LIMIT:
                    _announced_load_failures.popitem(last=False)
                log.error(
                    "runtime_overrides: cannot read or validate %s; retaining "
                    "the in-process last-known-good policy: %s",
                    OVERRIDES_PATH,
                    exc,
                )
            return state
    raise RuntimeOverridesSecurityError(
        f"refusing to continue with unreadable or invalid operator policy "
        f"{OVERRIDES_PATH}; repair or restore the file"
    ) from exc


_ANNOUNCED_LIMIT = 64
_announced: OrderedDict[str, None] = OrderedDict()


def denied_tools() -> set[str]:
    """Tools the dashboard has disabled. Unioned into the ACL deny-list.

    Re-validates each name against the same charset the writer enforces, so a
    hand-edited / corrupt override file can't push junk or oversized entries
    into ACL resolution. Logs once (per distinct denial set) that the override
    file is actively restricting tools -- this file influences the security
    ACL but lives outside config.toml, so its effect should not be silent.
    """
    sec = (_load().get("security") or {})
    raw = sec.get("denied_tools") or []
    valid = {str(n) for n in raw if isinstance(n, str) and _VALID_TOOL_NAME.match(n)}
    dropped = [n for n in raw if not (isinstance(n, str) and _VALID_TOOL_NAME.match(n))]
    if dropped:
        log.warning(
            "runtime_overrides: ignoring %d invalid denied_tools entr(y/ies) in %s: %r",
            len(dropped), OVERRIDES_PATH, dropped[:10],
        )
    if valid:
        key = ",".join(sorted(valid))
        if key not in _announced:
            _announced[key] = None
            while len(_announced) > _ANNOUNCED_LIMIT:
                _announced.popitem(last=False)
            log.info(
                "runtime_overrides: %s is denying %d tool(s) via the dashboard "
                "overlay: %s", OVERRIDES_PATH, len(valid), ", ".join(sorted(valid)),
            )
    return valid


# Firm model authority is one exact ``provider:model-id``. Keep the charset
# tight so a hand-edited / corrupt override can't push junk into resolution.
_VALID_MODEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


def _models_overlay() -> dict[str, str]:
    """Return the dashboard's one run-wide model pin, if configured."""
    raw = _load().get("models") or {}
    value = raw.get("default")
    return {"default": value.strip()} if isinstance(value, str) else {}


def default_model_override() -> str | None:
    """The dashboard's one run-wide exact model pin, or ``None``."""
    return _models_overlay().get("default")


def budget_override() -> float | None:
    """The per-goal spend cap (USD) the dashboard's settings page has set, or
    None. Consulted by ``budget.budget_from_config`` above the ``[budget]``
    config section. Re-validated on read; only a finite, positive number wins."""
    raw = (_load().get("budget") or {}).get("max_dollars")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def allowed_models() -> set[str]:
    """The admin allow-list of model specs (dashboard ``[access] allowed_models``).
    When non-empty, ``llm.model_for_role`` caps every role to this set and the
    settings pickers offer only these. Empty = no restriction. Re-validated on
    read so a tampered file can't inject junk."""
    raw = (_load().get("access") or {}).get("allowed_models") or []
    return {s.strip() for s in raw
            if isinstance(s, str) and _VALID_MODEL.fullmatch(s.strip())}


def _write_state(denied: set[str], models: dict[str, str] | None,
                 budget: float | None,
                 allowed: set[str] | None = None,
                 style: str | None = None) -> None:
    """Serialise the whole overlay: [security] denied_tools + optional [models]
    (one run-wide default) + [budget] max_dollars + [access] allowed_models +
    [styles] active. One file holds every retained
    surface, so each write renders the full state -- changing one must not drop
    the others. Optional params default to the on-disk overlay so existing
    callers preserve what they don't touch. Atomic write at 0o600.
    """
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    rendered = ", ".join(_toml_string(n) for n in sorted(denied))
    body = (
        "# Dashboard-managed overrides. Edit via the dashboard's\n"
        "# permissions page, not by hand (the dashboard rewrites this\n"
        "# file). Your config.toml is never touched by the dashboard.\n\n"
        "[security]\n"
        f"denied_tools = [{rendered}]\n"
    )
    if models:
        body += "\n[models]\n"
        body += f"default = {_toml_string(models['default'])}\n"
    if budget is not None:
        body += f"\n[budget]\nmax_dollars = {float(budget)}\n"
    allow = allowed_models() if allowed is None else allowed
    if allow:
        body += ("\n[access]\nallowed_models = ["
                 f"{', '.join(_toml_string(s) for s in sorted(allow))}]\n")
    active_style = style_override() if style is None else style
    if active_style:
        body += f"\n[styles]\nactive = {_toml_string(active_style)}\n"
    # Unique temp + os.replace (0600): a fixed ".toml.tmp" collides under two
    # concurrent writers -- one os.replace moves it out from under the other.
    from .file_lock import atomic_write_text
    # Parse and validate before replacement. A renderer/schema bug must never
    # atomically replace the last valid operator policy with corrupt TOML.
    state = _validate_loaded_state(_tomllib().loads(body))
    atomic_write_text(OVERRIDES_PATH, body)
    # Keep the fallback synchronized with writes from this process.
    with _OVERRIDE_LOCK:
        _remember_valid(_cache_key(), state)


def _toml_string(value: str) -> str:
    import json
    return json.dumps(value)


def _validate_tool_name(name: str) -> str:
    n = (name or "").strip()
    if not _VALID_TOOL_NAME.fullmatch(n):
        raise ValueError("invalid tool name")
    return n


def _validate_model(model: str) -> str:
    m = (model or "").strip()
    if (
        not _VALID_MODEL.fullmatch(m)
        or ":" not in m
        or not all(part.strip() for part in m.split(":", 1))
        or m.casefold() == "openrouter:auto"
    ):
        raise ValueError("model must be an exact provider:model")
    return m


@_serialized
def disable_tool(name: str) -> set[str]:
    """Add ``name`` to the overlay deny-list. Returns the new set."""
    current = denied_tools()
    current.add(_validate_tool_name(name))
    _write_state(current, _models_overlay() or None, budget_override())
    return current


@_serialized
def enable_tool(name: str) -> set[str]:
    """Remove ``name`` from the overlay deny-list. Returns the new set.

    Note: this only clears a dashboard-set override. If a tool is
    denied in config.toml itself, re-enabling requires editing config.
    """
    current = denied_tools()
    current.discard(_validate_tool_name(name))
    _write_state(current, _models_overlay() or None, budget_override())
    return current


@_serialized
def set_default_model(model: str) -> str:
    """Pin the dashboard's global default model. Returns the stored spec."""
    spec = _validate_model(model)
    models = _models_overlay()
    models["default"] = spec
    _write_state(denied_tools(), models, budget_override())
    return spec


@_serialized
def clear_default_model() -> None:
    """Drop the dashboard's global model pin."""
    models = _models_overlay()
    models.pop("default", None)
    _write_state(denied_tools(), models or None, budget_override())


@_serialized
def set_budget(max_dollars: float) -> float:
    """Set the dashboard's per-goal spend cap (USD). Returns the stored value."""
    try:
        v = float(max_dollars)
    except (TypeError, ValueError) as exc:
        raise ValueError("budget must be a number") from exc
    if not math.isfinite(v) or v <= 0:
        raise ValueError("budget must be a positive number")
    _write_state(denied_tools(), _models_overlay() or None, v)
    return v


@_serialized
def clear_budget() -> None:
    """Drop the dashboard spend cap, reverting to config.toml / defaults."""
    _write_state(denied_tools(), _models_overlay() or None, None)


def style_override() -> str | None:
    """The dashboard-selected output style name, or ``None`` if unset."""
    v = _load().get("styles", {})
    name = v.get("active") if isinstance(v, dict) else None
    return name if isinstance(name, str) and name.strip() else None


@_serialized
def set_style(name: str) -> str:
    """Select the active output style (a name from ``styles.all_styles()``).
    Validated against the registry so a typo can't silently take effect."""
    n = (name or "").strip()
    from .styles import all_styles
    if n not in all_styles():
        raise ValueError(f"unknown output style: {name!r}")
    _write_state(denied_tools(), _models_overlay() or None, budget_override(), style=n)
    return n


@_serialized
def clear_style() -> None:
    """Drop the output style, reverting to the default voice."""
    _write_state(denied_tools(), _models_overlay() or None, budget_override(), style="")


@_serialized
def set_allowed_models(specs) -> set[str]:
    """Set the admin model allow-list (an empty list clears it). Validates each
    spec before writing; returns the stored set."""
    allow: set[str] = set()
    for s in (specs or []):
        m = (str(s) or "").strip()
        if not m:
            continue
        allow.add(_validate_model(m))
    _write_state(denied_tools(), _models_overlay() or None, budget_override(),
                 allowed=allow)
    return allow


__all__ = [
    "RuntimeOverridesSecurityError",
    "denied_tools", "disable_tool", "enable_tool",
    "default_model_override", "set_default_model", "clear_default_model",
    "budget_override", "set_budget", "clear_budget",
    "allowed_models", "set_allowed_models",
    "OVERRIDES_PATH",
]
