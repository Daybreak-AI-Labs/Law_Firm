"""Consent prompts for destructive actions.

Tools that mutate user state (rm, force-push, mass-send, dd, mkfs)
call ``require_consent(action, risk_level)`` which:

  1. Checks the consent ledger -- a previously-granted consent for the
     same (action, scope) returns immediately.
  2. Checks ``MAVERICK_CONSENT_MODE`` env var:
        - "auto-approve" (default) -> grant + log (no friction out of the box)
        - "auto-deny"              -> deny + log
        - "ask"                    -> ask the user; in non-tty contexts, deny
        - "dashboard"              -> park in the approvals queue + poll
  3. Logs an audit event for prompt + result.

Threading: prompts serialize through a lock so two parallel agents
don't both pop a prompt simultaneously on the same TTY.

Note: this is the *primitive*. Tools wire it in themselves (the ``shell``
tool does). The base mode is ``auto-approve``, but under **secure-by-default**
**high-** and **critical-risk** actions fail closed to ``ask`` (routing to the
approvals queue / dashboard) instead of auto-approving; low/medium stay
non-interactive out of the box. An operator can widen or narrow this via
``MAVERICK_CONSENT_MODE`` (or per-action config), and ``[security]
secure_defaults = false`` / ``MAVERICK_SECURE_DEFAULT=0`` restores the old
fully-opt-in behavior.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..file_lock import (
    atomic_create_bytes,
    atomic_read_bytes,
    atomic_read_text,
    cross_process_lock,
    ensure_private_directory,
    ensure_private_file,
    open_private_append,
    require_private_directory,
)
from ..paths import TenantPolicyError, current_tenant_id_strict, data_dir

log = logging.getLogger(__name__)

# Legacy single-user override.  Keep this as a real attribute so test/integration
# patching restores ``None`` instead of materialising a tenant path returned by
# module ``__getattr__`` and pinning later calls to that stale location.
CONSENT_LEDGER_PATH: Path | None = None


def _same_ledger_path(left: Path, right: Path) -> bool:
    """Compare canonical paths, failing closed on resolution errors."""
    try:
        return left.expanduser().resolve(strict=False) == right.expanduser().resolve(
            strict=False
        )
    except (OSError, RuntimeError):
        return False


def _resolve_ledger_path(path: Path | None = None) -> Path:
    """Resolve a consent ledger without crossing a tenant boundary.

    ``CONSENT_LEDGER_PATH`` and the explicit ``path=`` argument predate tenant
    isolation.  They remain available to unscoped, single-user deployments,
    but a named tenant must always use its own data directory.  In particular,
    a stale process-wide override must never turn into a shared-grant fallback
    in a long-lived multi-tenant worker.
    """
    # Standing consent is an authorization boundary, so diagnostic identity
    # resolution is not sufficient here: corrupt client configuration must not
    # become indistinguishable from legitimate legacy/unbound mode. Resolve the
    # strict identity once and thread that exact value into path construction to
    # avoid a second lookup disagreeing with the admission decision.
    tenant = current_tenant_id_strict()
    if tenant is not None:
        active = data_dir("consent.ledger", tenant=tenant)
        if path is not None and not _same_ledger_path(Path(path), active):
            raise TenantPolicyError(
                "explicit consent ledger path does not match the active tenant"
            )
        return active
    if path is not None:
        return Path(path)
    override = CONSENT_LEDGER_PATH
    return Path(override) if override is not None else data_dir("consent.ledger")


def consent_ledger_path() -> Path:
    """The active tenant's consent ledger.

    A module-level path is unsafe in long-lived multi-tenant workers because it
    freezes whichever tenant imported this module first. An explicitly assigned
    legacy attribute remains an override for unscoped integrations/tests; it is
    deliberately ignored once a tenant or client floor is active.
    """
    return _resolve_ledger_path()


class ConsentDenied(Exception):
    """Raised when ``require_consent(..., raise_on_deny=True)`` is denied."""

    def __init__(self, action: str):
        super().__init__(f"consent denied for action: {action}")
        self.action = action


@dataclass(frozen=True)
class ConsentDecision:
    granted: bool
    source: str           # "ledger" | "auto" | "prompt" | "non-tty-deny"
    risk: str             # "low" | "medium" | "high" | "critical"
    ts: float
    actor: str = ""        # authenticated dashboard approver when available


_prompt_lock = threading.Lock()
_ledger_lock = threading.RLock()

_LEDGER_VERSION = 2
_MAX_LEDGER_BYTES = 4 * 1024 * 1024
_MAX_LEDGER_RECORDS = 10_000
_MAX_ACTION_CHARS = 256
_MAX_SCOPE_CHARS = 4096


class ConsentLedgerError(RuntimeError):
    """The standing-consent authority could not be verified safely."""


def _resolve_mode(risk: str | None = None) -> str:
    # Explicit operator setting always wins (any risk level).
    env = os.environ.get("MAVERICK_CONSENT_MODE")
    if env:
        return env.strip().lower()
    # Enterprise mode flips the default to 'ask' so destructive actions are gated
    # (and denied in non-interactive contexts) when handling sensitive data.
    try:
        from ..enterprise import enterprise_enabled
        if enterprise_enabled():
            return "ask"
    except Exception:
        pass
    # Secure-by-default: gate HIGH/CRITICAL-risk actions (an autonomous run can't
    # take a destructive action without an explicit decision -> 'ask', which
    # denies in a non-interactive context). Low/medium stay frictionless so
    # normal goals are unaffected. An explicit MAVERICK_CONSENT_MODE opts out.
    if str(risk or "").strip().lower() in ("high", "critical"):
        try:
            from ..security_defaults import secure_by_default
            if secure_by_default():
                return "ask"
        except Exception:
            pass
    return "auto-approve"


def _validate_ledger_field(value: str, *, name: str, limit: int, empty: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"consent {name} must be a string")
    if (not empty and not value) or len(value) > limit:
        qualifier = f"1..{limit}" if not empty else f"0..{limit}"
        raise ValueError(f"consent {name} must contain {qualifier} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"consent {name} cannot contain control characters")
    return value


def _validated_key(action: str, scope: str | None) -> tuple[str, str]:
    return (
        _validate_ledger_field(
            action, name="action", limit=_MAX_ACTION_CHARS, empty=False,
        ),
        _validate_ledger_field(
            scope or "", name="scope", limit=_MAX_SCOPE_CHARS, empty=True,
        ),
    )


def _principal_binding() -> str:
    try:
        from ..connections import current_principal

        principal = str(current_principal() or "local")
    except Exception:  # pragma: no cover - context lookup cannot widen authority
        principal = "local"
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()


def _policy_binding() -> str:
    try:
        from ..enterprise import enterprise_enabled

        enterprise = bool(enterprise_enabled())
    except Exception:
        enterprise = False
    try:
        from ..security_defaults import secure_by_default

        secure = bool(secure_by_default())
    except Exception:
        secure = True
    policy = {
        "enterprise": enterprise,
        "secure_defaults": secure,
        "version": _LEDGER_VERSION,
    }
    raw = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _authority_binding() -> tuple[str, str, str]:
    return (
        str(current_tenant_id_strict() or "shared"),
        _principal_binding(),
        _policy_binding(),
    )


def _is_default_ledger_path(path: Path) -> bool:
    return _same_ledger_path(path, data_dir("consent.ledger"))


def _prepare_ledger_parent(path: Path) -> None:
    if _is_default_ledger_path(path) or not path.parent.exists():
        ensure_private_directory(path.parent)
    else:
        # A caller-selected standing-authorization store is an integrity
        # boundary. Never claim a shared directory by changing its ACL.
        require_private_directory(path.parent)


@contextmanager
def _locked_ledger(path: Path | None = None) -> Iterator[Path]:
    resolved = _resolve_ledger_path(path)
    _prepare_ledger_parent(resolved)
    with _ledger_lock:
        with cross_process_lock(resolved, strict=True):
            yield resolved


def _key_path(path: Path) -> Path:
    return path.with_name(path.name + ".key")


def _ledger_key(path: Path, *, create: bool) -> bytes | None:
    key_path = _key_path(path)
    if key_path.exists():
        ensure_private_file(key_path)
        key = atomic_read_bytes(key_path)
        if len(key) != 32:
            raise ConsentLedgerError("consent ledger signing key is malformed")
        return key
    if not create:
        return None
    if path.exists() and path.stat().st_size:
        raise ConsentLedgerError(
            "unsigned or legacy consent ledger cannot authorize actions; "
            "move it aside and re-authorize standing grants"
        )
    key = secrets.token_bytes(32)
    try:
        atomic_create_bytes(key_path, key)
    except FileExistsError:
        ensure_private_file(key_path)
        key = atomic_read_bytes(key_path)
        if len(key) != 32:
            raise ConsentLedgerError("consent ledger signing key is malformed") from None
    return key


def _canonical_record(record: dict) -> bytes:
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _record_mac(key: bytes, unsigned: dict) -> str:
    return hmac.new(key, _canonical_record(unsigned), hashlib.sha256).hexdigest()


def _load_records_unlocked(path: Path, key: bytes | None) -> list[dict]:
    if not path.exists():
        return []
    ensure_private_file(path)
    size = path.stat().st_size
    if size > _MAX_LEDGER_BYTES:
        raise ConsentLedgerError("consent ledger exceeds its bounded size")
    if size == 0:
        return []
    if key is None:
        raise ConsentLedgerError("consent ledger has no signing key")
    text = atomic_read_text(path, encoding="utf-8")
    if not text.endswith("\n"):
        raise ConsentLedgerError("consent ledger contains a partial record")
    lines = text.splitlines()
    if len(lines) > _MAX_LEDGER_RECORDS:
        raise ConsentLedgerError("consent ledger contains too many records")
    records: list[dict] = []
    previous = "0" * 64
    expected_fields = {
        "action", "mac", "op", "policy", "prev", "principal", "scope",
        "seq", "tenant", "ts", "v",
    }
    for expected_seq, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ConsentLedgerError("consent ledger contains invalid JSON") from exc
        if not isinstance(record, dict) or set(record) != expected_fields:
            raise ConsentLedgerError("consent ledger record shape is invalid")
        if record["v"] != _LEDGER_VERSION or record["seq"] != expected_seq:
            raise ConsentLedgerError("consent ledger sequence is invalid")
        if record["op"] not in {"grant", "revoke"} or record["prev"] != previous:
            raise ConsentLedgerError("consent ledger chain is invalid")
        _validate_ledger_field(
            record["action"], name="action", limit=_MAX_ACTION_CHARS, empty=False,
        )
        _validate_ledger_field(
            record["scope"], name="scope", limit=_MAX_SCOPE_CHARS, empty=True,
        )
        if not all(
            isinstance(record[field], str)
            and 0 < len(record[field]) <= 256
            for field in ("tenant", "principal", "policy", "prev", "mac")
        ):
            raise ConsentLedgerError("consent ledger authority binding is invalid")
        if (
            isinstance(record["ts"], bool)
            or not isinstance(record["ts"], (int, float))
            or not math.isfinite(record["ts"])
            or record["ts"] < 0
        ):
            raise ConsentLedgerError("consent ledger timestamp is invalid")
        unsigned = {name: value for name, value in record.items() if name != "mac"}
        if not hmac.compare_digest(record["mac"], _record_mac(key, unsigned)):
            raise ConsentLedgerError("consent ledger signature is invalid")
        previous = record["mac"]
        records.append(record)
    return records


def _active_grants(records: list[dict]) -> dict[tuple[str, str], None]:
    tenant, principal, policy = _authority_binding()
    active: dict[tuple[str, str], None] = {}
    for record in records:
        if (
            record["tenant"] != tenant
            or record["principal"] != principal
            or record["policy"] != policy
        ):
            continue
        key = (record["action"], record["scope"])
        if record["op"] == "grant":
            active[key] = None
        else:
            active.pop(key, None)
    return active


def _append_record_unlocked(
    path: Path, key: bytes, records: list[dict], *, op: str,
    action: str, scope: str,
) -> None:
    if len(records) >= _MAX_LEDGER_RECORDS:
        raise ConsentLedgerError("consent ledger contains too many records")
    tenant, principal, policy = _authority_binding()
    unsigned = {
        "action": action,
        "op": op,
        "policy": policy,
        "prev": records[-1]["mac"] if records else "0" * 64,
        "principal": principal,
        "scope": scope,
        "seq": len(records) + 1,
        "tenant": tenant,
        "ts": time.time(),
        "v": _LEDGER_VERSION,
    }
    record = {**unsigned, "mac": _record_mac(key, unsigned)}
    encoded = _canonical_record(record) + b"\n"
    current_size = path.stat().st_size if path.exists() else 0
    if current_size + len(encoded) > _MAX_LEDGER_BYTES:
        raise ConsentLedgerError("consent ledger exceeds its bounded size")
    fd = open_private_append(path, require_private_parent=True)
    try:
        with os.fdopen(fd, "ab") as stream:
            fd = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _check_ledger(action: str, scope: str | None, path: Path | None = None) -> bool:
    """True only for an intact grant bound to this tenant/principal/policy."""
    try:
        key_tuple = _validated_key(action, scope)
        with _locked_ledger(path) as resolved:
            key = _ledger_key(resolved, create=False)
            return key_tuple in _active_grants(_load_records_unlocked(resolved, key))
    except (ConsentLedgerError, OSError, PermissionError, ValueError) as exc:
        log.warning("consent: standing grants unavailable: %s", exc)
        return False


def grant_persistent(
    action: str, scope: str | None = None, *, path: Path | None = None,
) -> None:
    """Record a forever-grant; subsequent require_consent() returns immediately.

    Use sparingly; the more we ledger, the less the prompts matter.
    """
    action, normalized_scope = _validated_key(action, scope)
    with _locked_ledger(path) as resolved:
        key = _ledger_key(resolved, create=True)
        assert key is not None
        records = _load_records_unlocked(resolved, key)
        if (action, normalized_scope) not in _active_grants(records):
            _append_record_unlocked(
                resolved, key, records, op="grant",
                action=action, scope=normalized_scope,
            )


def revoke(
    action: str,
    scope: str | None = None,
    *,
    path: Path | None = None,
    strict: bool = False,
    write_tombstone: bool = False,
) -> bool:
    """Remove all matching grants. Returns True if anything was removed.

    The default preserves the consent primitive's historical fail-soft
    administrative behavior. Security lifecycle transitions that must prove
    authority was revoked before proceeding (for example generated-tool
    deletion) pass ``strict=True`` so an unreadable or unwritable ledger aborts
    the mutation instead of looking identical to "there was no grant".
    ``write_tombstone=True`` appends a signed revocation even when no active
    grant exists, preserving durable evidence that the exact authority was
    invalidated. Repeating an already-current tombstone is idempotent.
    """
    action, normalized_scope = _validated_key(action, scope)
    try:
        with _locked_ledger(path) as resolved:
            key = _ledger_key(resolved, create=write_tombstone)
            records = _load_records_unlocked(resolved, key)
            was_active = (action, normalized_scope) in _active_grants(records)
            if key is None or (not was_active and not write_tombstone):
                return False
            if write_tombstone and not was_active:
                tenant, principal, policy = _authority_binding()
                last_matching = next(
                    (
                        record
                        for record in reversed(records)
                        if record["tenant"] == tenant
                        and record["principal"] == principal
                        and record["policy"] == policy
                        and record["action"] == action
                        and record["scope"] == normalized_scope
                    ),
                    None,
                )
                if last_matching is not None and last_matching["op"] == "revoke":
                    return False
            _append_record_unlocked(
                resolved, key, records, op="revoke",
                action=action, scope=normalized_scope,
            )
            return was_active
    except (ConsentLedgerError, OSError, PermissionError) as exc:
        if strict:
            raise
        log.warning("consent: cannot revoke standing grant: %s", exc)
        return False


def list_grants(*, path: Path | None = None) -> list[tuple[str, str]]:
    """Return [(action, scope), ...] of all current grants."""
    try:
        with _locked_ledger(path) as resolved:
            key = _ledger_key(resolved, create=False)
            return list(_active_grants(_load_records_unlocked(resolved, key)))
    except (ConsentLedgerError, OSError, PermissionError) as exc:
        log.warning("consent: cannot list standing grants: %s", exc)
        return []


def require_consent(
    action: str,
    *,
    risk: str = "medium",
    scope: str | None = None,
    detail: str | None = None,
    provenance: str | None = None,
    raise_on_deny: bool = False,
    allow_auto_approve: bool = True,
    consult_ledger: bool = True,
) -> ConsentDecision:
    """Gate a destructive action through user (or env) approval.

    ``action`` is a short identifier (e.g. "rm-rf", "force-push",
    "mass-dm"). ``scope`` is the resource being acted on (e.g.
    "/tmp/build", "main", "channel:#general"). ``detail`` is a
    human-readable description shown in the prompt. ``provenance`` is trusted
    caller-supplied metadata for dashboard labels; never derive it from
    user/model-controlled ``detail`` text.

    Returns a ConsentDecision. If ``raise_on_deny``, denials raise
    ConsentDenied instead.

    ``allow_auto_approve=False`` is for high-trust paths that require an
    explicit operator decision even though the consent primitive defaults to
    ``auto-approve`` for backwards compatibility. Ledger grants, dashboard
    approvals, and TTY prompts still work; silent auto-approval is treated as
    a denial.

    ``consult_ledger=False`` additionally disables the prior-grant fast-path, so
    a *fresh* decision is required even when a persistent grant for this
    ``(action, scope)`` exists. Used by the governance EU AI Act Art-14 gate
    when an operator opts into per-action human oversight
    (``[governance] require_fresh_human_approval``).
    """
    ts = time.time()
    # 1) Ledger fast-path (skipped when a fresh decision is demanded).
    if consult_ledger and _check_ledger(action, scope):
        return _emit(ConsentDecision(True, "ledger", risk, ts), action, scope, detail)
    # 2) Mode override (risk-aware: secure-by-default gates high/critical risk).
    mode = _resolve_mode(risk)
    if mode == "auto-approve":
        d = _emit(
            ConsentDecision(allow_auto_approve, "auto", risk, ts),
            action, scope, detail,
        )
        if not d.granted and raise_on_deny:
            raise ConsentDenied(action)
        return d
    if mode == "auto-deny":
        d = _emit(ConsentDecision(False, "auto", risk, ts), action, scope, detail)
        if raise_on_deny:
            raise ConsentDenied(action)
        return d
    if mode == "dashboard":
        d = _decide_via_dashboard(action, risk, scope, detail, provenance)
        if d is not None:
            d = _emit(d, action, scope, detail)
            if not d.granted and raise_on_deny:
                raise ConsentDenied(action)
            return d
        # Dashboard unavailable -> fall through to the interactive/non-tty
        # path below (fail-open: the kernel never *requires* the dashboard).
    # 3) Interactive prompt (or non-tty deny).
    if not sys.stdin.isatty():
        d = _emit(ConsentDecision(False, "non-tty-deny", risk, ts), action, scope, detail)
        if raise_on_deny:
            raise ConsentDenied(action)
        return d
    with _prompt_lock:
        msg = _format_prompt(action, risk, scope, detail)
        sys.stderr.write(msg)
        sys.stderr.flush()
        try:
            reply = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            reply = ""
    granted = reply in {"y", "yes"}
    d = _emit(ConsentDecision(granted, "prompt", risk, ts), action, scope, detail)
    if not granted and raise_on_deny:
        raise ConsentDenied(action)
    return d


def _dashboard_timeout() -> float:
    """How long (seconds) to wait for a dashboard approval before giving up.

    A timeout falls through to the interactive/non-tty path (fail-open),
    so a dashboard that's never opened doesn't wedge the agent forever.
    """
    try:
        return max(0.0, float(os.environ.get("MAVERICK_CONSENT_DASHBOARD_TIMEOUT", "300")))
    except ValueError:
        return 300.0


def _consent_requester(wm) -> str | None:
    """The principal on whose behalf consent is being requested.

    This is the owner of the goal currently executing (bound in the goal trace
    context and copied across ``asyncio.to_thread`` into the tool/consent
    worker). The goal owner shares the dashboard's approver namespace
    (``caller_principal``), so recording it as ``requested_by`` lets the
    dual-control self-approval bar in ``world_model.decide_approval`` fire:
    under N-of-M dual control the requester cannot count as one of the distinct
    approvers (segregation of duties).

    Returns None when consent runs outside a goal, or for an unowned goal
    (single-user / no-auth) -- the bar then stays inactive, which is the
    unchanged single-approver behaviour."""
    try:
        from ..logging_config import current_goal_id
        gid = current_goal_id()
        if gid is None:
            return None
        goal = wm.get_goal(gid)
        owner = (getattr(goal, "owner", "") or "").strip()
        return owner or None
    except Exception:  # pragma: no cover -- requester resolution never blocks consent
        return None


def _decide_via_dashboard(
    action: str,
    risk: str,
    scope: str | None,
    detail: str | None,
    provenance: str | None,
) -> ConsentDecision | None:
    """Park the action in the world model and poll for a dashboard decision.

    Returns a ConsentDecision once the operator approves/denies via the
    dashboard /approvals page, or ``None`` if the world model is
    unavailable or the wait times out -- the caller then falls back to
    the interactive/non-tty path (fail-open per the kernel contract).
    """
    try:
        from ..world_model import close_world_if_owned, open_world
        wm = open_world()  # client/tenant-floored: the same DB the dashboard reads
    except Exception as e:  # world model missing/unwritable -> fail-open
        log.warning("consent: dashboard mode unavailable, falling back: %s", e)
        return None
    # Approval-delegation routing (opt-in via [governance.delegation] rules):
    # a risk/tool rule can route this approval to a specific delegate. No-op
    # (route returns None) when no rules are configured, so the default queue
    # behaviour is unchanged. The delegate is recorded in detail for the
    # operator console.
    try:
        from ..approval_delegation import route as _delegate_route
        delegate = _delegate_route({"risk": risk, "tool": action})
        if delegate:
            detail = f"{detail or ''}\n[delegated to: {delegate}]".strip()
    except Exception:  # pragma: no cover -- delegation never blocks consent
        pass
    try:
        # N-of-M dual control: a risk band may require multiple distinct approvers
        # before the action is granted (segregation of duties). Default 1 ->
        # unchanged single-approver behaviour.
        from .dual_control import required_approvals
        required = required_approvals(risk)
    except Exception:  # pragma: no cover -- config never blocks consent
        required = 1
    try:
        approval_id = wm.create_approval(
            action, risk=risk, scope=scope, detail=detail, provenance=provenance,
            approvals_required=required, requested_by=_consent_requester(wm),
        )
    except Exception as e:
        log.warning("consent: cannot queue approval, falling back: %s", e)
        close_world_if_owned(wm)
        return None

    # monotonic for the elapsed-time window: a wall-clock jump must not collapse
    # the human-approval window (timing out a risky-action prompt early) or
    # extend it (stalling the agent). The decision record below keeps wall time.
    deadline = time.monotonic() + _dashboard_timeout()
    while time.monotonic() < deadline:
        try:
            row = wm.get_approval(approval_id)
        except Exception:
            close_world_if_owned(wm)
            return None
        if row is not None and row.status != "pending":
            granted = row.status == "approved"
            decision = ConsentDecision(
                granted,
                "dashboard",
                risk,
                time.time(),
                actor=str(getattr(row, "decided_by", "") or ""),
            )
            close_world_if_owned(wm)
            return decision
        time.sleep(1.0)
    close_world_if_owned(wm)
    return None  # timed out: caller falls back


def _format_prompt(action: str, risk: str, scope: str | None, detail: str | None) -> str:
    risk_tag = {"low": "?", "medium": "!", "high": "!!", "critical": "!!!"}.get(risk, "?")
    parts = [
        f"\n[CONSENT {risk_tag}] {action}",
    ]
    if scope:
        parts.append(f"  scope: {scope}")
    if detail:
        parts.append(f"  detail: {detail}")
    parts.append("Allow? [y/N]: ")
    return "\n".join(parts)


def _emit(
    decision: ConsentDecision,
    action: str,
    scope: str | None,
    detail: str | None,
) -> ConsentDecision:
    """Log the consent decision to the audit log (fail-safe)."""
    try:
        from ..audit import EventKind, record
        record(
            EventKind.CONSENT_PROMPT,
            action=action, risk=decision.risk,
            scope=scope, detail=detail,
        )
        record(
            EventKind.CONSENT_RESULT,
            action=action,
            decision="approve" if decision.granted else "deny",
            source=decision.source,
            decided_by=decision.actor,
        )
    except Exception:  # pragma: no cover -- never crash on audit
        pass
    return decision


__all__ = [
    "ConsentDecision",
    "ConsentDenied",
    "ConsentLedgerError",
    "require_consent",
    "grant_persistent",
    "revoke",
    "list_grants",
]
