"""Bring-your-own-agent: enroll agents built on OTHER platforms into the
governed workforce.

An Agentforce, Bedrock, OpenAI, LangChain, or home-grown agent runs on its own
runtime — Maverick cannot orchestrate it, but it CAN govern and account for
it. This module is the missing spine between the two registries that already
exist (the Agent Trust Plane for identity/ceilings, the fleet-memory roster
for learning) and the Operating Record:

* ``enroll``       — one call registers the agent in BOTH registries, stamps
                     platform provenance (which vendor runtime it lives on,
                     who owns it, which department it serves), and audits who
                     enrolled it.
* ``mint_token``   — issues the per-surface bearer credential the agent will
                     actually authenticate with (today that is a hand-edited
                     TOML field). The token value is returned exactly once.
* ``record_run``   — ingests a completed external run as a first-class
                     Operating Record row: a goal owned by ``agent:<id>``
                     with a step trail, an outcome, and a costed episode — so
                     Overview / Spend / Workforce / Savings and the audit
                     binder see foreign agents exactly like native ones.
* ``screen``       — the governance seam: the external agent asks BEFORE
                     acting. Trust admission (registry, direction, tool and
                     risk ceilings, revocation), cumulative budget, Shield
                     input scanning, and the ``[actions]`` approval floor all
                     apply; high-risk actions park a real approval row the
                     human decides in the dashboard queue.

The external boundary is fail-closed throughout: an unenrolled agent is
denied, a Shield scanner error DENIES (never waves through), a malformed
sidecar is treated as empty-but-alarmed, and over-budget agents are refused
further actions rather than silently accumulating spend.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

log = logging.getLogger("maverick.external_agents")

#: Platform semantics for enrollment — where the agent actually runs. This is
#: provenance (it shows on the roster, in audit rows, and as the fleet-memory
#: vendor), not a behavior switch: every platform gets the same governance.
PLATFORMS: dict[str, str] = {
    "agentforce": "Salesforce Agentforce",
    "bedrock": "AWS Bedrock Agents",
    "copilot": "Microsoft Copilot Studio",
    "openai": "OpenAI Agents",
    "langchain": "LangChain / LangGraph",
    "custom": "Custom / other runtime",
}

#: Credential surfaces an external agent can hold a bearer for. ``rest`` is
#: the external-agents HTTP API itself; the rest are the existing per-surface
#: tokens on the trust entry (distinct so a leak on one surface cannot
#: authenticate another).
TOKEN_SURFACES = ("rest", "grpc", "mcp")

_MAX_TEXT = 4000
_MAX_STEPS = 50
_MAX_AGENTS = 500
#: Remembered idempotency keys per agent (newest kept) — a retried
#: ``record_run`` returns the original goal instead of double-counting.
_MAX_RUN_KEYS = 200
#: Auto-containment threshold: this many screening denials inside the window
#: flips the agent to contained (fail-closed; an admin releases it).
_CONTAIN_DENIALS = 5
_CONTAIN_WINDOW_SECONDS = 24 * 3600

#: Budget accounting modes. ``monthly`` resets the meter each calendar month
#: (UTC); ``total`` is a lifetime cap. Pre-v1.1 enrollments carry no period
#: and are treated as ``total`` so their semantics never change under them.
BUDGET_PERIODS = ("monthly", "total")

#: Governed-execution ledger bounds. A parked (approved-not-yet-committed)
#: execution lives at most the TTL; a full pending backlog refuses new parks
#: rather than growing the sidecar without bound.
_MAX_PENDING_EXECUTIONS = 20
_EXECUTION_TTL_SECONDS = 24 * 3600
#: Canonical-JSON body ceiling for the execute tier — the sidecar ledger and
#: the approver display must stay readable; bulk payloads belong on the
#: system of record's own bulk surfaces, not proxied through the gateway.
_MAX_EXECUTE_BODY_BYTES = 32 * 1024
#: How long a claimed (``executing``) entry stays authoritative past its
#: claim before the status view reports it INDETERMINATE — the worker died
#: between the claim and the terminal write, so the effect may or may not
#: have fired and must never re-fire automatically.
_EXECUTING_GRACE_SECONDS = 600
#: Consumed step-up mint approvals remembered per agent (newest kept) — the
#: one-shot ledger that makes one approval mint exactly one credential.
_MAX_USED_MINT_APPROVALS = 50


class ExternalAgentsError(Exception):
    """Operator-facing failure (bad input, unknown agent, plane disabled)."""


class MintApprovalPending(ExternalAgentsError):
    """A gated credential mint parked a world approval and stopped.

    ``[external_agents] mint_approval`` requires a human decision before a
    bearer is issued; the caller re-runs the mint with ``mint_approval_id``
    once approval ``approval_id`` is approved."""

    def __init__(self, message: str, approval_id: int):
        super().__init__(message)
        self.approval_id = approval_id


def entitled() -> bool:
    """Bring-your-own-agent governance is a paid (Gold) capability.

    Fail-open like fleet governance: it only bites when a deployment has
    turned license enforcement on and the license lacks the feature. Roster
    reads are never gated — only the governed traffic (enroll / ingest /
    screen)."""
    try:
        from .entitlements import require
        return require("external_agents")
    except Exception:  # pragma: no cover - entitlements missing => don't block
        return True


def enabled() -> bool:
    """``[external_agents] enable`` or ``MAVERICK_EXTERNAL_AGENTS=1``."""
    import os
    if str(os.environ.get("MAVERICK_EXTERNAL_AGENTS", "")).strip() == "1":
        return True
    try:
        from .config import load_config
        return bool((load_config() or {}).get(
            "external_agents", {}).get("enable", False))
    except Exception:
        return False


# -- sidecar registry (platform/ownership metadata + reported-spend meter) ----

def _seat_limit() -> int:
    """Enrollment seat cap: ``[external_agents] max_enrolled`` clamped to the
    structural ceiling. The commercial seat count lives in config (set by the
    operator to match their license order); the hard cap protects the
    JSON-sidecar storage model."""
    try:
        from .config import load_config
        raw = int((load_config() or {}).get(
            "external_agents", {}).get("max_enrolled", _MAX_AGENTS))
        return max(1, min(raw, _MAX_AGENTS))
    except Exception:
        return _MAX_AGENTS


def note_seen(agent_id: str) -> None:
    """Stamp the agent's last authenticated contact (credential audit trail).

    Throttled to 5-minute granularity so the hot path never write-amplifies
    the sidecar; failures are swallowed — liveness bookkeeping must never
    break a request."""
    try:
        now = time.time()
        meta = _load_sidecar().get(agent_id)
        if meta is None or now - float(meta.get("last_seen") or 0) < 300:
            return
        with _sidecar_locked():
            sidecar = _load_sidecar()
            if agent_id in sidecar:
                sidecar[agent_id]["last_seen"] = now
                _save_sidecar(sidecar)
    except Exception:  # pragma: no cover -- bookkeeping never blocks
        log.debug("external_agents: last-seen stamp failed", exc_info=True)


def registry_path():
    """Enrollment metadata sidecar (``external_agents.json``), tenant-scoped.

    Identity and ceilings live in the Agent Trust Plane; this file carries what
    the trust entry deliberately does not: platform provenance, ownership, and
    the cumulative reported-spend meter the budget cutoff reads."""
    from .paths import data_dir
    return data_dir("external_agents.json")


def _load_sidecar() -> dict[str, dict]:
    path = registry_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("sidecar root must be an object")
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, UnicodeError, ValueError) as e:
        # Metadata loss must not widen trust (identity still gates), but it
        # must not pass silently either -- the spend meters live here.
        log.warning("external_agents: sidecar unreadable (%s); treating as "
                    "empty", e)
        return {}


def _save_sidecar(entries: dict[str, dict]) -> None:
    from .file_lock import atomic_write_text, ensure_private_directory
    path = registry_path()
    ensure_private_directory(path.parent)
    atomic_write_text(path, json.dumps(entries, indent=2, sort_keys=True),
                      mode=0o600)


def _sidecar_locked():
    from .file_lock import cross_process_lock
    return cross_process_lock(registry_path())


def _month_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(now if now is not None
                                              else time.time()))


def _period_spent(meta: dict, now: float | None = None) -> float:
    """The spend that counts against the cap under the agent's budget mode."""
    if meta.get("period") == "monthly":
        if meta.get("period_key") != _month_key(now):
            return 0.0  # the month rolled; nothing spent yet this period
        return float(meta.get("period_spent") or 0.0)
    return float(meta.get("spent_dollars") or 0.0)


def _parse_tool_entry(entry: str) -> tuple[str, str | None]:
    """Split an enrollment tool entry ``name`` / ``name:risk``.

    The optional risk is the OPERATOR'S rating for that tool and floors
    whatever the caller later declares — external tool names are arbitrary,
    so the registry, not the agent's honesty, decides how risky a tool is."""
    from .safety.tool_risk import RISK_LEVELS
    name, sep, risk = str(entry).partition(":")
    name = name.strip()
    if not sep:
        return name, None
    risk = risk.strip().lower()
    if risk not in RISK_LEVELS:
        raise ExternalAgentsError(
            f"tool entry {entry!r}: unknown risk {risk!r}; expected one of "
            f"{', '.join(RISK_LEVELS)}")
    return name, risk


# -- text hygiene at the boundary ---------------------------------------------

def _clean_text(text: Any, *, shield: Any | None = None) -> str | None:
    """Redact secrets, refuse injection payloads. ``None`` = reject.

    Same posture as the fleet-memory inbox: external text is untrusted; a
    tripwire hit rejects the field rather than laundering it into the record.
    A Shield instance, when provided, gets the final word — and a scanner
    ERROR rejects (fail-closed: this is an external boundary)."""
    safe = str(text or "")[:_MAX_TEXT]
    if not safe:
        return ""
    try:
        from .safety.secret_detector import redact
        safe = redact(safe)[0]
    except Exception:
        return None
    try:
        from .memory_guard import injection_markers
        if injection_markers(safe):
            return None
    except Exception:
        return None
    if shield is not None:
        try:
            verdict = shield.scan_input(safe)
            if getattr(verdict, "blocked", False):
                return None
        except Exception:
            return None
    return safe


# -- enrollment ---------------------------------------------------------------

def enroll(
    agent_id: str,
    platform: str,
    *,
    description: str = "",
    owner: str = "",
    department: str = "",
    allow_tools: list[str] | None = None,
    deny_tools: list[str] | None = None,
    max_risk: str | None = None,
    max_dollars: float | None = None,
    max_wall_seconds: float | None = None,
    data_scopes: list[str] | None = None,
    expires_days: float | None = None,
    budget_period: str = "monthly",
    enrolled_by: str = "",
) -> dict:
    """Enroll one external agent across every plane in a single call.

    Creates/replaces the managed Agent Trust entry (direction ``inbound`` —
    the agent calls us; we never dial an enrolled foreign runtime), registers
    the agent on the fleet-memory roster when that plane is enabled, and
    stamps the enrollment metadata sidecar. Returns a summary; raises
    :class:`ExternalAgentsError` on bad input."""
    from .agent_trust import AgentTrustError, put_agent
    platform = str(platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ExternalAgentsError(
            f"unknown platform {platform!r}; expected one of "
            f"{', '.join(sorted(PLATFORMS))}")
    if budget_period not in BUDGET_PERIODS:
        raise ExternalAgentsError(
            f"unknown budget_period {budget_period!r}; expected one of "
            f"{', '.join(BUDGET_PERIODS)}")
    # Tool entries may carry the operator's risk rating ("name:risk"); the
    # trust entry keeps the bare names, the sidecar keeps the risk map.
    tool_names: list[str] = []
    tool_risks: dict[str, str] = {}
    for raw in allow_tools or []:
        name, tool_risk = _parse_tool_entry(raw)
        if name:
            tool_names.append(name)
            if tool_risk:
                tool_risks[name] = tool_risk
    sidecar = _load_sidecar()
    if agent_id not in sidecar and len(sidecar) >= _seat_limit():
        raise ExternalAgentsError(
            f"enrollment limit reached ({_seat_limit()} seats); raise "
            "[external_agents] max_enrolled or unenroll unused agents")
    entry: dict[str, Any] = {
        "id": agent_id,
        "direction": "inbound",
        "allow_tools": sorted(tool_names),
        "deny_tools": sorted(deny_tools or []),
        "max_risk": max_risk,
        "max_dollars": max_dollars,
        "max_wall_seconds": max_wall_seconds,
        "data_scopes": sorted(data_scopes or []),
    }
    if expires_days is not None:
        if not isinstance(expires_days, (int, float)) or expires_days <= 0:
            raise ExternalAgentsError("expires_days must be a positive number")
        entry["expires_at"] = time.time() + float(expires_days) * 86400
    try:
        agent = put_agent(entry)
    except AgentTrustError as e:
        raise ExternalAgentsError(str(e)) from e
    fleet = "disabled"
    try:
        from . import fleet_memory
        if fleet_memory.enabled():
            fleet = ("registered" if fleet_memory.register_agent(
                agent.id, platform, description=description) else "refused")
    except Exception as e:
        fleet = "error"
        log.warning("external_agents: fleet roster registration failed for "
                    "%r: %s", agent.id, e)
    with _sidecar_locked():
        sidecar = _load_sidecar()
        prior = sidecar.get(agent.id, {})
        sidecar[agent.id] = {
            "platform": platform,
            "description": str(description or "")[:500],
            "owner": str(owner or "")[:200],
            "department": str(department or "")[:100],
            "enrolled_by": str(enrolled_by or "")[:200],
            "enrolled_at": prior.get("enrolled_at") or time.time(),
            "spent_dollars": float(prior.get("spent_dollars") or 0.0),
            "runs": int(prior.get("runs") or 0),
            "last_run_at": prior.get("last_run_at"),
            # v1.1 governance state (re-enrollment preserves the meters and
            # NEVER silently lifts a containment — that's the release action).
            "period": budget_period,
            "period_key": prior.get("period_key") or _month_key(),
            "period_spent": float(prior.get("period_spent") or 0.0),
            "tool_risks": tool_risks,
            "run_keys": dict(prior.get("run_keys") or {}),
            "denials": list(prior.get("denials") or []),
            "contained": bool(prior.get("contained")),
            "wall_violations": int(prior.get("wall_violations") or 0),
        }
        _save_sidecar(sidecar)
    from .audit import EventKind, audit_event
    audit_event(EventKind.EXTERNAL_AGENT_ENROLLED, agent="external_agents",
                external_agent=agent.id, platform=platform,
                department=department, enrolled_by=enrolled_by)
    return {"id": agent.id, "platform": platform, "trust": "registered",
            "fleet_memory": fleet, "expires_at": agent.expires_at}


def unenroll(agent_id: str) -> bool:
    """Remove the managed trust entry + sidecar metadata. The fleet-memory
    roster entry (learning provenance) is kept — history is not rewritten."""
    from .agent_trust import remove_agent
    removed = remove_agent(agent_id)
    with _sidecar_locked():
        sidecar = _load_sidecar()
        if sidecar.pop(agent_id, None) is not None:
            _save_sidecar(sidecar)
            removed = True
    return removed


def mint_token(agent_id: str, surface: str, *, minted_by: str = "",
               mint_approval_id: int | None = None) -> str:
    """Mint (or rotate) the bearer credential for one surface.

    Returns the token value — the ONLY time it is ever shown; the registry
    stores it for verification but no read path returns it. Raises on an
    unknown agent or surface.

    Step-up re-auth: with ``[external_agents] mint_approval`` on, a call
    without ``mint_approval_id`` parks a world approval (dual-control quorum
    at "high" risk, requested by ``minted_by``) and raises
    :class:`MintApprovalPending` carrying its id; the caller re-runs the mint
    with that id once a decision-maker approves. Approvals are one-shot and
    bound to exactly this agent + surface."""
    from .agent_trust import AgentTrustError, lookup, put_agent
    if surface not in TOKEN_SURFACES:
        raise ExternalAgentsError(
            f"unknown surface {surface!r}; expected one of "
            f"{', '.join(TOKEN_SURFACES)}")
    agent = lookup(agent_id)
    if agent is None:
        raise ExternalAgentsError(f"agent {agent_id!r} is not enrolled")
    approved_id: int | None = None
    if _mint_approval_required():
        if mint_approval_id is None:
            approval_id = _park_mint_approval(agent_id, surface, minted_by)
            raise MintApprovalPending(
                f"credential mint for {agent_id!r} ({surface}) requires "
                f"approval: parked as approval #{approval_id} — have a "
                "decision-maker approve it in the dashboard queue, then "
                "re-run the mint with that approval id", approval_id)
        approved_id = _consume_mint_approval(agent_id, surface,
                                             mint_approval_id)
    token = f"lw-{surface}-{secrets.token_urlsafe(32)}"
    entry = _entry_from_agent(agent)
    # At rest the registry holds only the hash — a leaked overlay backup
    # must never authenticate. The raw value exists in this return alone.
    from .agent_trust import hash_token
    entry[f"{surface}_token"] = hash_token(token)
    try:
        put_agent(entry)
    except AgentTrustError as e:
        raise ExternalAgentsError(str(e)) from e
    from .audit import EventKind, audit_event
    extra = {"mint_approval_id": approved_id} if approved_id else {}
    audit_event(EventKind.EXTERNAL_CREDENTIAL_MINTED, agent="external_agents",
                external_agent=agent_id, surface=surface, **extra)
    return token


def _mint_approval_required() -> bool:
    """``[external_agents] mint_approval``: step-up re-auth on credential
    minting — a mint parks a world approval a decision-maker must approve."""
    try:
        from .config import get_external_agents
        return bool(get_external_agents()["mint_approval"])
    except Exception:
        return False


def _mint_action(agent_id: str, surface: str) -> str:
    """The approval action string a mint approval is bound to — the exact-
    match check is what stops an approval minting for another agent/surface."""
    return f"mint-credential:{agent_id}:{surface}"


def _park_mint_approval(agent_id: str, surface: str, minted_by: str) -> int:
    """Park the step-up approval for one credential mint; returns its id."""
    from .safety.dual_control import required_approvals
    from .world_model import open_world
    return open_world().create_approval(
        _mint_action(agent_id, surface), risk="high",
        detail=f"Mint (or rotate) the {surface} bearer credential for "
               f"external agent {agent_id!r}. The token is shown once to the "
               "requester; the previous bearer stops working immediately.",
        provenance="external_agents",
        approvals_required=required_approvals("high"),
        requested_by=minted_by or "")


def _consume_mint_approval(agent_id: str, surface: str,
                           mint_approval_id: int) -> int:
    """Validate + spend one mint approval; returns its id, raises on refusal.

    The approval must exist under our provenance, match this agent + surface's
    action string exactly, and be approved. Consumption is recorded in the
    agent's sidecar (``used_mint_approvals``, bounded, checked under the lock)
    BEFORE the mint so one approval mints exactly one credential — a racing or
    repeated mint with the same id is refused, and a storage failure after the
    spend burns the approval rather than ever letting it cover two mints."""
    try:
        approval_id = int(mint_approval_id)
    except (TypeError, ValueError) as e:
        raise ExternalAgentsError(
            f"mint_approval_id {mint_approval_id!r} is not an approval "
            "id") from e
    from .world_model import open_world
    approval = open_world().get_approval(approval_id)
    if approval is None or approval.provenance != "external_agents":
        raise ExternalAgentsError(f"unknown mint approval {approval_id!r}")
    action = _mint_action(agent_id, surface)
    if approval.action != action:
        raise ExternalAgentsError(
            f"approval #{approval_id} was granted for {approval.action!r}, "
            f"not {action!r} — a mint approval is bound to exactly one "
            "agent and surface")
    if approval.status != "approved":
        raise ExternalAgentsError(
            f"approval #{approval_id} is {approval.status}; minting needs "
            "an approved decision")
    with _sidecar_locked():
        sidecar = _load_sidecar()
        meta = sidecar.setdefault(agent_id, {})
        # String-compare the ledger so a junk entry can never crash the
        # check (we only ever write ints; junk stays inert but preserved).
        used = list(meta.get("used_mint_approvals") or [])
        spent = (f"approval #{approval_id} has already minted a credential; "
                 "approvals are one-shot — run the mint without an approval "
                 "id to park a new one")
        if any(str(u) == str(approval_id) for u in used):
            raise ExternalAgentsError(spent)
        # A world approval never expires, so the bounded ledger alone would
        # let an id become reusable once it aged out. The floor is the
        # highest id ever EVICTED: everything at or below it is treated as
        # spent, so forgetting can only ever refuse, never re-authorize.
        floor = int(meta.get("mint_approval_floor") or 0)
        if approval_id <= floor:
            raise ExternalAgentsError(spent)
        used.append(approval_id)
        keep = used[-_MAX_USED_MINT_APPROVALS:]
        for stale in used[:len(used) - len(keep)]:
            try:
                floor = max(floor, int(stale))
            except (TypeError, ValueError):  # junk entry; nothing to raise to
                continue
        meta["mint_approval_floor"] = floor
        meta["used_mint_approvals"] = keep
        _save_sidecar(sidecar)
    return approval_id


def _entry_from_agent(agent) -> dict:
    """A managed-registry entry dict reconstructed from a parsed TrustedAgent,
    so read-modify-write flows (token mint, revoke-with-note) can go through
    ``put_agent`` without reaching into the overlay file format."""
    return {
        "id": agent.id,
        "pubkey": agent.pubkey,
        "direction": agent.direction,
        "allow_tools": sorted(agent.allow_tools),
        "deny_tools": sorted(agent.deny_tools),
        "max_risk": agent.max_risk,
        "max_dollars": agent.max_dollars,
        "max_wall_seconds": agent.max_wall_seconds,
        "data_scopes": sorted(agent.data_scopes),
        "rest_token": agent.rest_token,
        "grpc_token": agent.grpc_token,
        "mcp_token": agent.mcp_token,
        "jwt_issuer": agent.jwt_issuer,
        "jwt_audience": agent.jwt_audience,
        "jwks_file": agent.jwks_file,
        "hmac_secret_ref": agent.hmac_secret_ref,
        "not_before": agent.not_before,
        "expires_at": agent.expires_at,
        "revoked": agent.revoked,
    }


# -- run ingest onto the Operating Record -------------------------------------

def record_run(agent_id: str, run: dict, *, shield: Any | None = None) -> dict:
    """Ingest one COMPLETED external run as a governed Operating Record row.

    ``run`` schema: ``{title, outcome: success|failure, summary?, steps?:
    [str], cost_dollars?, input_tokens?, output_tokens?, tool_calls?,
    department?}``. The goal lands owned by ``agent:<id>`` so every
    owner-scoped surface (spend, workforce, savings, audit binder,
    containment) sees the foreign agent as a first-class principal.

    Returns ``{goal_id, over_budget}``; raises :class:`ExternalAgentsError`
    on deny (not enrolled, revoked, plane disabled, poisoned text)."""
    if not enabled():
        raise ExternalAgentsError("external agents plane is disabled")
    from .agent_trust import decide_inbound, record_denied
    # enforced=True: the gateway is an external boundary in its own right, so
    # the registry binds here even when the global trust plane is disengaged.
    decision = decide_inbound(agent_id, enforced=True)
    if not decision.allowed:
        record_denied(agent_id, decision, direction="inbound",
                      correlation_id="record_run")
        raise ExternalAgentsError(f"denied: {decision.reason}")
    sidecar = _load_sidecar()
    if agent_id not in sidecar:
        raise ExternalAgentsError(
            f"agent {agent_id!r} has a trust entry but no enrollment; "
            "enroll it before ingesting runs")
    if sidecar[agent_id].get("contained"):
        raise ExternalAgentsError(
            "denied: agent is contained pending review (an administrator "
            "can release it from the console)")
    # Exactly-once ingest: a retried report with the same key returns the
    # original goal instead of double-counting spend (same posture as the
    # A2A message-id ledger). Keys are per-agent and bounded.
    idem = str(run.get("idempotency_key") or "").strip()[:128]
    if idem:
        prior_gid = (sidecar[agent_id].get("run_keys") or {}).get(idem)
        if prior_gid is not None:
            return {"goal_id": prior_gid, "over_budget":
                    bool(sidecar[agent_id].get("over_budget")),
                    "duplicate": True}
    title = _clean_text(run.get("title"), shield=shield)
    summary = _clean_text(run.get("summary"), shield=shield)
    if title is None or summary is None or not (title or "").strip():
        raise ExternalAgentsError(
            "run title/summary rejected by the ingest screen")
    outcome = "success" if str(run.get("outcome", "")).lower() in (
        "success", "done", "ok") else "failure"
    steps: list[str] = []
    for raw in list(run.get("steps") or [])[:_MAX_STEPS]:
        cleaned = _clean_text(raw, shield=shield)
        if cleaned is None:
            raise ExternalAgentsError(
                "a run step was rejected by the ingest screen")
        if cleaned:
            steps.append(cleaned)
    cost = max(0.0, float(run.get("cost_dollars") or 0.0))
    meta = sidecar[agent_id]
    principal = f"agent:{agent_id}"
    from .world_model import open_world
    w = open_world()
    gid = w.create_goal(
        title, description=summary or "",
        owner=principal,
        domain=str(run.get("department") or meta.get("department") or ""))
    for step in steps:
        w.append_event(gid, principal, "status", step)
    eid = w.start_episode(gid)
    w.end_episode(
        eid, summary or title,
        "success" if outcome == "success" else "failure",
        cost_dollars=cost,
        input_tokens=max(0, int(run.get("input_tokens") or 0)),
        output_tokens=max(0, int(run.get("output_tokens") or 0)),
        tool_calls=max(0, int(run.get("tool_calls") or 0)),
    )
    w.set_goal_status(
        gid, "done" if outcome == "success" else "blocked",
        result=summary or title)
    # Wall-clock ceiling: a reported duration above the enrollment's
    # max_wall_seconds is ingested (spend must never go uncounted) but
    # flagged and counted — repeat offenders are visible on the roster.
    duration = max(0.0, float(run.get("duration_seconds") or 0.0))
    wall_cap = decision.agent.max_wall_seconds if decision.agent else None
    over_wall = bool(wall_cap is not None and duration > wall_cap)
    over_budget = _settle_spend(
        agent_id, gid, cost=cost, idem=idem, over_wall=over_wall,
        cap=decision.agent.max_dollars if decision.agent else None)
    from .audit import EventKind, audit_event
    audit_event(EventKind.EXTERNAL_RUN_INGESTED, agent="external_agents",
                goal_id=gid, external_agent=agent_id, outcome=outcome,
                cost_dollars=cost, steps=len(steps), over_budget=over_budget,
                over_wall=over_wall)
    return {"goal_id": gid, "over_budget": over_budget,
            "over_wall": over_wall}


def _settle_spend(agent_id: str, gid: int, *, cost: float, idem: str,
                  over_wall: bool, cap: float | None) -> bool:
    """Meter one run against the sidecar: spend, period rollover, run count,
    wall violations, idempotency ledger, and the over-budget flag. Shared by
    after-the-fact ingest and the live finish path."""
    with _sidecar_locked():
        sidecar = _load_sidecar()
        meta = sidecar.setdefault(agent_id, {})
        meta["spent_dollars"] = float(meta.get("spent_dollars") or 0.0) + cost
        if meta.get("period") == "monthly":
            month = _month_key()
            if meta.get("period_key") != month:
                meta["period_key"] = month
                meta["period_spent"] = 0.0
            meta["period_spent"] = float(meta.get("period_spent") or 0.0) + cost
        meta["runs"] = int(meta.get("runs") or 0) + 1
        meta["last_run_at"] = time.time()
        if over_wall:
            meta["wall_violations"] = int(meta.get("wall_violations") or 0) + 1
        if idem:
            keys = dict(meta.get("run_keys") or {})
            keys[idem] = gid
            if len(keys) > _MAX_RUN_KEYS:
                for stale in sorted(keys, key=keys.get)[:len(keys)
                                                        - _MAX_RUN_KEYS]:
                    keys.pop(stale, None)
            meta["run_keys"] = keys
        over_budget = bool(cap is not None and _period_spent(meta) > cap)
        meta["over_budget"] = over_budget
        _save_sidecar(sidecar)
    return over_budget


# -- live runs (start / heartbeat / finish) -----------------------------------

def start_run(agent_id: str, run: dict, *, shield: Any | None = None) -> dict:
    """Open a LIVE external run on the Operating Record.

    The goal lands ``active`` under ``agent:<id>`` so Oversight sees the work
    while it is happening, and the kill switch can bite mid-flight: the agent
    must heartbeat within the orphan-reclaim window (default 60s, see
    ``MAVERICK_ORPHAN_RECLAIM_SECONDS``) or the run is reclaimed to blocked
    with a restart marker. Schema: ``{title, summary?, department?}``."""
    if not enabled():
        raise ExternalAgentsError("external agents plane is disabled")
    from .agent_trust import decide_inbound, record_denied
    decision = decide_inbound(agent_id, enforced=True)
    if not decision.allowed:
        record_denied(agent_id, decision, direction="inbound",
                      correlation_id="start_run")
        raise ExternalAgentsError(f"denied: {decision.reason}")
    sidecar = _load_sidecar()
    meta = sidecar.get(agent_id)
    if meta is None:
        raise ExternalAgentsError(
            f"agent {agent_id!r} has a trust entry but no enrollment")
    if meta.get("contained"):
        raise ExternalAgentsError("denied: agent is contained pending review")
    title = _clean_text(run.get("title"), shield=shield)
    summary = _clean_text(run.get("summary"), shield=shield)
    if title is None or summary is None or not (title or "").strip():
        raise ExternalAgentsError("run title rejected by the ingest screen")
    principal = f"agent:{agent_id}"
    from .world_model import open_world
    w = open_world()
    gid = w.create_goal(
        title, description=summary or "", owner=principal,
        domain=str(run.get("department") or meta.get("department") or ""))
    # Freshly created by us; the id is unknown to anyone else, so the
    # ownerless CAS is safe (and the Postgres backend's owner-aware claim
    # deliberately fails closed).
    w.claim_goal_for_run(gid)
    from .audit import EventKind, audit_event
    audit_event(EventKind.EXTERNAL_RUN_STARTED, agent="external_agents",
                goal_id=gid, external_agent=agent_id)
    from .world_model import reclaim_window_seconds
    return {"goal_id": gid,
            "heartbeat_seconds": reclaim_window_seconds()}


def heartbeat(agent_id: str, goal_id: int) -> dict:
    """Keep a live run alive AND ask permission to continue.

    Returns ``{"continue": bool, "reason": str}``. ``continue: false`` is
    the kill switch reaching mid-flight work: containment, revocation,
    expiry, or the run having been reclaimed/finished all stop the agent.
    A truthful agent halts on false; an agent that ignores it stops
    refreshing its liveness anyway and gets reclaimed."""
    if not enabled():
        return {"continue": False, "reason": "external agents plane disabled"}
    from .agent_trust import decide_inbound
    decision = decide_inbound(agent_id, enforced=True)
    if not decision.allowed:
        return {"continue": False, "reason": decision.reason}
    meta = _load_sidecar().get(agent_id)
    if meta is None or meta.get("contained"):
        return {"continue": False,
                "reason": "agent is contained or unenrolled"}
    from .world_model import open_world
    alive = open_world().touch_goal(int(goal_id),
                                    expected_owner=f"agent:{agent_id}")
    if not alive:
        return {"continue": False,
                "reason": "run is not live under this agent (finished, "
                          "reclaimed, or not yours)"}
    return {"continue": True, "reason": "ok"}


def finish_run(agent_id: str, goal_id: int, run: dict,
               *, shield: Any | None = None) -> dict:
    """Close a live run: outcome, step trail, costed episode, spend meter.

    Same schema as ``record_run`` minus ``title``. The goal must be live and
    owned by the caller — cross-agent finishes are refused."""
    if not enabled():
        raise ExternalAgentsError("external agents plane is disabled")
    from .agent_trust import decide_inbound, lookup, record_denied
    decision = decide_inbound(agent_id, enforced=True)
    if not decision.allowed:
        record_denied(agent_id, decision, direction="inbound",
                      correlation_id="finish_run")
        raise ExternalAgentsError(f"denied: {decision.reason}")
    principal = f"agent:{agent_id}"
    from .world_model import open_world
    w = open_world()
    goal = w.get_goal(int(goal_id))
    if goal is None or goal.owner != principal or goal.status not in (
            "active", "pending"):
        raise ExternalAgentsError(
            "run is not live under this agent (finished, reclaimed, or "
            "not yours)")
    summary = _clean_text(run.get("summary"), shield=shield)
    if summary is None:
        raise ExternalAgentsError("run summary rejected by the ingest screen")
    outcome = "success" if str(run.get("outcome", "")).lower() in (
        "success", "done", "ok") else "failure"
    steps: list[str] = []
    for raw in list(run.get("steps") or [])[:_MAX_STEPS]:
        cleaned = _clean_text(raw, shield=shield)
        if cleaned is None:
            raise ExternalAgentsError(
                "a run step was rejected by the ingest screen")
        if cleaned:
            steps.append(cleaned)
    for step in steps:
        w.append_event(goal_id, principal, "status", step)
    cost = max(0.0, float(run.get("cost_dollars") or 0.0))
    eid = w.start_episode(goal_id)
    w.end_episode(
        eid, summary or goal.title,
        "success" if outcome == "success" else "failure",
        cost_dollars=cost,
        input_tokens=max(0, int(run.get("input_tokens") or 0)),
        output_tokens=max(0, int(run.get("output_tokens") or 0)),
        tool_calls=max(0, int(run.get("tool_calls") or 0)),
    )
    w.set_goal_status(
        goal_id, "done" if outcome == "success" else "blocked",
        result=summary or goal.title)
    agent = lookup(agent_id)
    duration = max(0.0, float(run.get("duration_seconds") or 0.0))
    wall_cap = agent.max_wall_seconds if agent else None
    over_wall = bool(wall_cap is not None and duration > wall_cap)
    over_budget = _settle_spend(
        agent_id, int(goal_id), cost=cost,
        idem=str(run.get("idempotency_key") or "").strip()[:128],
        over_wall=over_wall, cap=agent.max_dollars if agent else None)
    from .audit import EventKind, audit_event
    audit_event(EventKind.EXTERNAL_RUN_INGESTED, agent="external_agents",
                goal_id=int(goal_id), external_agent=agent_id,
                outcome=outcome, cost_dollars=cost, steps=len(steps),
                over_budget=over_budget, over_wall=over_wall, live=True)
    return {"goal_id": int(goal_id), "over_budget": over_budget,
            "over_wall": over_wall}


# -- learning plane (fleet memory over the gateway) ---------------------------

def memory_ingest(agent_id: str, record: dict,
                  *, shield: Any | None = None) -> tuple[bool, str]:
    """Deposit one experience record into fleet memory over the gateway.

    The learning plane was MCP-only; platforms that speak plain HTTPS
    (Agentforce External Services, Bedrock action groups) could not reach
    it. The vendor is the enrollment's platform — provenance comes from OUR
    registry, never the caller's claim — and the caller identity is bound so
    the trust plane's "an agent acts only as itself" rule applies."""
    if not enabled():
        return False, "external agents plane is disabled"
    from . import fleet_memory
    meta = _load_sidecar().get(agent_id)
    if meta is None:
        return False, f"agent {agent_id!r} is not enrolled"
    if meta.get("contained"):
        return False, "agent is contained pending review"
    body = {
        "agent_id": agent_id,
        "vendor": str(meta.get("platform") or "custom"),
        "kind": record.get("kind"),
        "goal_text": record.get("goal_text"),
        "reflection": record.get("reflection"),
        "tools_used": record.get("tools_used"),
        "domain": record.get("domain") or meta.get("department") or None,
    }
    with fleet_memory.bind_caller(agent_id):
        return fleet_memory.ingest(body, shield=shield)


def memory_recall(agent_id: str, query: str, *, domain: str | None = None,
                  shield: Any | None = None) -> tuple[str, str]:
    """Governed memory read over the gateway — same identity binding and
    data-scope gates as the MCP path; every read is audited."""
    if not enabled():
        return "", "external agents plane is disabled"
    from . import fleet_memory
    meta = _load_sidecar().get(agent_id)
    if meta is None:
        return "", f"agent {agent_id!r} is not enrolled"
    if meta.get("contained"):
        return "", "agent is contained pending review"
    with fleet_memory.bind_caller(agent_id):
        return fleet_memory.recall(
            query, agent_id=agent_id,
            vendor=str(meta.get("platform") or "custom"),
            domain=domain or meta.get("department") or None, shield=shield)


def _memory_counts() -> dict[str, dict]:
    """Per-agent fleet-memory contribution counts, keyed by agent id.

    Best-effort join against ``fleet_memory.status()`` (source keys are
    ``vendor:agent_id``); an unavailable learning plane yields {}."""
    try:
        from . import fleet_memory
        if not fleet_memory.enabled():
            return {}
        counts = fleet_memory.status().get("ingested") or {}
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for source, kinds in counts.items():
        _, _, agent_id = str(source).partition(":")
        if agent_id:
            row = out.setdefault(agent_id, {})
            for kind, n in (kinds or {}).items():
                row[kind] = int(row.get(kind, 0)) + int(n)
    return out


# -- pre-action screening (the governance seam) -------------------------------

def screen(
    agent_id: str,
    tool: str,
    *,
    detail: str = "",
    risk: str | None = None,
    shield: Any | None = None,
) -> dict:
    """Decide one proposed external action BEFORE the agent takes it.

    Applies, in order: containment, trust admission (registry / direction /
    revocation / tool + risk ceilings — the requested risk is FLOORED by the
    operator's per-tool rating from the enrollment, so an agent cannot
    under-declare its way beneath a gate), the period budget cutoff, Shield
    input scanning over ``detail`` (scanner error = DENY — external
    boundary), and the ``[actions] require_approval_at`` floor — at/above
    the floor the action parks a real approval row.

    Repeated denials inside the containment window flip the agent to
    ``contained`` (every call refused until an admin releases it).

    Returns ``{allowed, rule, reason, requires_approval?, approval_id?}``.
    Every decision is audited."""
    if not enabled():
        return _screen_verdict(agent_id, tool, False, "disabled",
                               "external agents plane is disabled")
    from .safety.tool_risk import risk_rank
    deny, meta, _decision, req_risk = _screen_admission(
        agent_id, tool, risk=risk, detail=detail, shield=shield)
    if deny is not None:
        return deny
    floor = _approval_floor()
    if req_risk and risk_rank(req_risk) >= risk_rank(floor):
        from .world_model import open_world
        approval_id = open_world().create_approval(
            f"external:{agent_id}:{tool}", risk=req_risk,
            scope=meta.get("department") or None,
            detail=str(detail or "")[:2000], provenance="external_agents",
            requested_by=f"agent:{agent_id}")
        _notify_approval(approval_id, agent_id, tool, req_risk,
                         meta.get("department") or "")
        out = _screen_verdict(
            agent_id, tool, False, "approval_required",
            f"risk {req_risk} is at/above the {floor} approval floor; "
            "awaiting a human decision")
        out["requires_approval"] = True
        out["approval_id"] = approval_id
        return out
    return _screen_verdict(agent_id, tool, True, "allow", "within ceilings")


def _screen_admission(
    agent_id: str,
    tool: str,
    *,
    risk: str | None,
    detail: str,
    shield: Any | None,
    correlation: str = "screen",
) -> tuple[dict | None, dict | None, Any, str]:
    """The admission chain shared by ``screen`` and the execute tier.

    Containment, risk validation + the operator's per-tool floor, trust
    admission (registry / direction / revocation / tool + risk ceilings),
    enrollment, the period budget cutoff, and Shield input scanning —
    everything except the approval floor, which each caller handles itself
    (a screen parks a bare approval; an execute parks a digest-bound pending
    execution). Returns ``(deny_verdict|None, meta, decision, req_risk)``;
    a non-None verdict has already been audited and, where it should,
    counted toward containment."""
    from .agent_trust import decide_inbound, record_denied
    from .safety.tool_risk import RISK_LEVELS, risk_rank
    meta = _load_sidecar().get(agent_id)
    if meta is not None and meta.get("contained"):
        return (_screen_verdict(
            agent_id, tool, False, "contained",
            "agent is contained pending review (repeated denials); an "
            "administrator can release it from the console"), meta, None, "")
    req_risk = str(risk or "").strip().lower()
    if req_risk and req_risk not in RISK_LEVELS:
        return (_deny_and_count(agent_id, tool, "bad_risk",
                                f"unknown risk level {req_risk!r}"),
                meta, None, "")
    # The operator's per-tool rating floors the caller's declaration:
    # external tool names are arbitrary, so the registry decides the risk.
    mapped = (meta or {}).get("tool_risks", {}).get(tool)
    if mapped and (not req_risk
                   or risk_rank(mapped) > risk_rank(req_risk)):
        req_risk = mapped
    decision = decide_inbound(
        agent_id, requested_tools=[tool] if tool else (),
        max_risk=req_risk or None, enforced=True)
    if not decision.allowed:
        record_denied(agent_id, decision, direction="inbound",
                      correlation_id=correlation)
        return (_deny_and_count(agent_id, tool, decision.rule,
                                decision.reason), meta, decision, req_risk)
    if meta is None:
        return (_screen_verdict(agent_id, tool, False, "not_enrolled",
                                "agent has a trust entry but no enrollment"),
                None, decision, req_risk)
    cap = decision.agent.max_dollars if decision.agent else None
    if cap is not None and _period_spent(meta) > cap:
        period = ("this month's" if meta.get("period") == "monthly"
                  else "cumulative")
        return (_deny_and_count(
            agent_id, tool, "budget",
            f"{period} reported spend exceeds the ${cap:,.2f} ceiling"),
            meta, decision, req_risk)
    if detail and shield is not None:
        try:
            verdict = shield.scan_input(str(detail)[:_MAX_TEXT])
            if getattr(verdict, "blocked", False):
                return (_deny_and_count(
                    agent_id, tool, "shield",
                    "action detail was blocked by the input screen"),
                    meta, decision, req_risk)
        except Exception:
            # Fail CLOSED: an erroring scanner must never wave an external
            # action through unscreened. OUR failure, not the agent's — it
            # does not count toward containment.
            return (_screen_verdict(
                agent_id, tool, False, "screen_error",
                "input screen unavailable; action denied (fail-closed)"),
                meta, decision, req_risk)
    return None, meta, decision, req_risk


def _notify_approval(approval_id: int, agent_id: str, tool: str,
                     risk: str, department: str) -> None:
    """Tell a human an external agent is blocked waiting — fail-soft.

    Approvals were pull-only: the agent sat in a poll loop until someone
    happened to open the queue. Two channels, both best-effort on a
    governance hot path: the ops push transport, and the signed outbound
    webhook (``[external_agents] approval_webhook``, HMAC + SSRF guard via
    the shared webhook machinery). The payload is provenance-only — never
    the ``detail`` string, which is untrusted external text."""
    try:
        from .ops_alert import alert
        alert("external_approval.pending",
              f"{agent_id} is waiting on approval #{approval_id}: "
              f"{tool} ({risk})", severity="high")
    except Exception:  # pragma: no cover -- notification never blocks
        log.warning("external_agents: approval push notification failed",
                    exc_info=True)
    try:
        from .config import load_config
        url = str((load_config() or {}).get("external_agents", {}).get(
            "approval_webhook", "")).strip()
        if url:
            from .webhooks import fire
            fire("external_approval.created",
                 {"approval_id": approval_id, "agent_id": agent_id,
                  "tool": tool, "risk": risk, "department": department},
                 urls=[url])
    except Exception:  # pragma: no cover -- notification never blocks
        log.warning("external_agents: approval webhook failed",
                    exc_info=True)


def _approval_floor() -> str:
    from .safety.tool_risk import RISK_LEVELS
    try:
        from .config import load_config
        v = str((load_config() or {}).get("actions", {}).get(
            "require_approval_at", "high")).strip().lower()
        return v if v in RISK_LEVELS else "high"
    except Exception:
        return "high"


def _screen_verdict(agent_id: str, tool: str, allowed: bool, rule: str,
                    reason: str) -> dict:
    from .audit import EventKind, audit_event
    audit_event(EventKind.EXTERNAL_ACTION_SCREENED, agent="external_agents",
                external_agent=agent_id, tool=tool, allowed=allowed,
                rule=rule, reason=reason)
    return {"allowed": allowed, "rule": rule, "reason": reason}


def _deny_and_count(agent_id: str, tool: str, rule: str, reason: str) -> dict:
    """A denial that counts toward auto-containment.

    Misbehavior denials (ceiling breaches, budget, blocked payloads, junk
    risk words) accumulate in a sliding window; at the threshold the agent
    is contained — every subsequent call refused until an administrator
    releases it. Platform-side failures (scanner errors) and the ordinary
    approval-floor park never count."""
    contained_now = False
    with _sidecar_locked():
        sidecar = _load_sidecar()
        meta = sidecar.get(agent_id)
        if meta is not None:
            now = time.time()
            denials = [t for t in (meta.get("denials") or [])
                       if now - float(t) < _CONTAIN_WINDOW_SECONDS]
            denials.append(now)
            meta["denials"] = denials[-(_CONTAIN_DENIALS * 4):]
            if len(denials) >= _CONTAIN_DENIALS and not meta.get("contained"):
                meta["contained"] = True
                contained_now = True
            _save_sidecar(sidecar)
    if contained_now:
        from .audit import EventKind, audit_event
        audit_event(EventKind.EXTERNAL_AGENT_CONTAINED,
                    agent="external_agents", external_agent=agent_id,
                    denials=_CONTAIN_DENIALS,
                    window_hours=_CONTAIN_WINDOW_SECONDS // 3600,
                    last_rule=rule)
    return _screen_verdict(agent_id, tool, False, rule, reason)


def release(agent_id: str, *, released_by: str = "") -> bool:
    """Admin action: lift a containment and clear the denial window."""
    with _sidecar_locked():
        sidecar = _load_sidecar()
        meta = sidecar.get(agent_id)
        if meta is None:
            return False
        meta["contained"] = False
        meta["denials"] = []
        _save_sidecar(sidecar)
    from .audit import EventKind, audit_event
    audit_event(EventKind.EXTERNAL_AGENT_RELEASED, agent="external_agents",
                external_agent=agent_id, released_by=released_by)
    return True


def reset_budget(agent_id: str, *, reset_by: str = "") -> bool:
    """Admin action: zero the active budget meter (period AND the flag).

    The lifetime total is preserved — the reset changes what counts against
    the cap, never what was historically reported."""
    with _sidecar_locked():
        sidecar = _load_sidecar()
        meta = sidecar.get(agent_id)
        if meta is None:
            return False
        meta["period_key"] = _month_key()
        meta["period_spent"] = 0.0
        if meta.get("period") != "monthly":
            # A lifetime-cap agent's meter IS spent_dollars; a reset converts
            # the remainder into an explicit baseline by moving to monthly
            # accounting from now on (documented in the console).
            meta["period"] = "monthly"
        meta["over_budget"] = False
        _save_sidecar(sidecar)
    from .audit import EventKind, audit_event
    audit_event(EventKind.EXTERNAL_BUDGET_RESET, agent="external_agents",
                external_agent=agent_id, reset_by=reset_by)
    return True


def approval_status(approval_id: int) -> dict:
    """Poll endpoint for a parked ``screen`` approval: the external agent
    holds its action until the human decides in the dashboard queue."""
    from .world_model import open_world
    approval = open_world().get_approval(int(approval_id))
    if approval is None or approval.provenance != "external_agents":
        raise ExternalAgentsError(f"unknown approval {approval_id!r}")
    return {"approval_id": approval.id, "status": approval.status,
            "risk": approval.risk}


# -- governed execution (the enforcement tier above screening) ----------------

def execute_connectors() -> list[str]:
    """Governed-REST connectors external agents may execute through.

    ``[external_agents] connectors`` (env override
    ``MAVERICK_EXTERNAL_CONNECTORS``, comma-separated), intersected with the
    reference factories — an unknown name can never open an egress path.
    Empty means the enforcement tier is off and the gateway is screen-only."""
    import os

    from .governed_rest import GOVERNED_REST_FACTORIES
    raw = os.environ.get("MAVERICK_EXTERNAL_CONNECTORS")
    if raw is not None:
        names = [p.strip().lower() for p in raw.split(",") if p.strip()]
    else:
        try:
            from .config import get_external_agents
            names = get_external_agents()["connectors"]
        except Exception:
            return []
    return [n for n in names if n in GOVERNED_REST_FACTORIES]


def _request_digest(connector: str, op: str, path: str,
                    body: dict | None) -> str:
    """Canonical fingerprint binding an approval to EXACTLY one request."""
    import hashlib
    canon = json.dumps(
        {"connector": connector, "op": op, "path": path, "body": body or {}},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _prune_executions(meta: dict, now: float | None = None) -> dict:
    """Drop expired execution-ledger entries in place; returns the live map.

    An ``executing`` entry gets the claim grace on top of the TTL: a commit
    whose network call straddles the TTL boundary must still find its row
    for the terminal status write — pruning it mid-flight would erase the
    ledger's memory of a fired effect."""
    now = time.time() if now is None else now
    ex: dict[str, dict] = {}
    for k, v in dict(meta.get("executions") or {}).items():
        if not isinstance(v, dict):
            continue
        horizon = _EXECUTION_TTL_SECONDS
        if v.get("status") == "executing":
            horizon += _EXECUTING_GRACE_SECONDS
        if now - float(v.get("created_at") or 0) <= horizon:
            ex[k] = v
    meta["executions"] = ex
    return ex


def _screen_execute_request(agent_id: str, request: dict, *,
                            shield: Any | None) -> tuple[dict | None, dict]:
    """Parse and boundary-screen one execute request.

    Validation bounds every component FIRST (detail 4k, path 2k, body 32k
    canonical JSON), then the injection tripwire and Shield scan cover the
    WHOLE blob with no truncation — nothing can pad another field outside
    the scan window. The checker erroring denies (external boundary,
    fail-closed), and the request is never rewritten: a redacted path would
    execute against the wrong record, so we flag, never launder. Returns
    ``(deny_verdict|None, parsed)``."""
    from .governed_rest import WRITE_OPS
    connector = str(request.get("connector") or "").strip().lower()
    op = str(request.get("op") or "").strip().lower()
    path = str(request.get("path") or "").strip()
    body = request.get("body")
    detail = str(request.get("detail") or "")
    tool = (f"{connector}.{'read' if op == 'get' else 'write'}"
            if connector else "execute")
    parsed = {"connector": connector, "op": op, "path": path,
              "body": body if isinstance(body, dict) else None,
              "preview": bool(request.get("preview")), "detail": detail,
              "tool": tool}
    if connector not in execute_connectors():
        return _screen_verdict(
            agent_id, tool, False, "connector_not_enabled",
            f"connector {connector!r} is not enabled for external "
            "execution ([external_agents] connectors)"), parsed
    if op != "get" and op not in WRITE_OPS:
        return _deny_and_count(
            agent_id, tool, "bad_op",
            f"unknown op {op!r}; expected get or one of "
            f"{', '.join(WRITE_OPS)}"), parsed
    if not path or len(path) > 2000:
        return _screen_verdict(
            agent_id, tool, False, "bad_path",
            "path is required (and bounded at 2000 chars)"), parsed
    if body is not None and not isinstance(body, dict):
        return _screen_verdict(agent_id, tool, False, "bad_body",
                               "body must be a JSON object"), parsed
    if len(detail) > _MAX_TEXT:
        return _screen_verdict(
            agent_id, tool, False, "detail_too_long",
            f"detail exceeds {_MAX_TEXT} chars"), parsed
    body_json = ""
    if body:
        body_json = json.dumps(body, sort_keys=True,
                               separators=(",", ":"), default=str)
        if len(body_json.encode("utf-8")) > _MAX_EXECUTE_BODY_BYTES:
            return _screen_verdict(
                agent_id, tool, False, "body_too_large",
                f"request body exceeds {_MAX_EXECUTE_BODY_BYTES} bytes"), \
                parsed
    scan_blob = "\n".join(x for x in (detail, path, body_json) if x)
    try:
        from .memory_guard import injection_markers
        if injection_markers(scan_blob):
            return _deny_and_count(
                agent_id, tool, "injection",
                "request payload tripped the injection screen"), parsed
    except Exception:
        return _screen_verdict(
            agent_id, tool, False, "screen_error",
            "injection screen unavailable; action denied "
            "(fail-closed)"), parsed
    if shield is not None:
        try:
            verdict = shield.scan_input(scan_blob)
            if getattr(verdict, "blocked", False):
                return _deny_and_count(
                    agent_id, tool, "shield",
                    "request payload was blocked by the input screen"), \
                    parsed
        except Exception:
            return _screen_verdict(
                agent_id, tool, False, "screen_error",
                "input screen unavailable; action denied "
                "(fail-closed)"), parsed
    return None, parsed


def execute(agent_id: str, request: dict, *,
            shield: Any | None = None) -> dict:
    """Perform one outbound action ON THE AGENT'S BEHALF through a governed
    connector — the enforcement tier above :func:`screen`.

    ``screen`` answers "may I?" and trusts the agent to act on its own
    platform; ``execute`` makes Maverick the actor. The request runs through
    the same admission chain, then through the governed-REST connector path —
    host IP-pinning, enterprise egress allowlists, no redirects — with a
    tamper-evident PREPARE/COMMIT receipt around the effect. Reads are
    low-risk and run immediately; writes are high-risk and park a
    DIGEST-BOUND approval: the agent re-sends the byte-identical request to
    :func:`execute_commit` once a human approves, so what executes is exactly
    what was authorized — never a swapped payload.

    ``request``: ``{connector, op: get|post|put|patch|delete, path, body?,
    preview?, detail?, goal_id?}``. ``preview: true`` describes a write
    without performing it (no network, no approval — there is no side
    effect). ``goal_id`` binds the effect to one of the agent's live runs so
    the receipt and the step trail land on that Operating Record row.
    Returns a verdict dict; every decision is audited."""
    if not enabled():
        return _screen_verdict(agent_id, "execute", False, "disabled",
                               "external agents plane is disabled")
    deny, parsed = _screen_execute_request(agent_id, request, shield=shield)
    if deny is not None:
        return deny
    connector, op, path = parsed["connector"], parsed["op"], parsed["path"]
    body, preview = parsed["body"], parsed["preview"]
    detail, tool = parsed["detail"], parsed["tool"]
    risk = "low" if (op == "get" or preview) else "high"
    # detail="" — the full blob (detail included) was scanned above; the
    # shared chain still applies containment, ceilings, and budget.
    deny, meta, _decision, req_risk = _screen_admission(
        agent_id, tool, risk=risk, detail="",
        shield=shield, correlation="execute")
    if deny is not None:
        return deny
    gid = request.get("goal_id")
    if gid is not None:
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            return _screen_verdict(agent_id, tool, False, "bad_goal",
                                   "goal_id must be an integer")
        from .world_model import open_world
        goal = open_world().get_goal(gid)
        if (goal is None or goal.owner != f"agent:{agent_id}"
                or goal.status not in ("active", "pending")):
            return _screen_verdict(
                agent_id, tool, False, "bad_goal",
                "goal is not a live run owned by this agent")
    from .governed_rest import GOVERNED_REST_FACTORIES
    conn = GOVERNED_REST_FACTORIES[connector]()
    digest = _request_digest(connector, op, path,
                             body if isinstance(body, dict) else None)
    if preview:
        if op == "get":
            return _screen_verdict(agent_id, tool, False, "bad_op",
                                   "preview applies to writes only")
        out = _screen_verdict(
            agent_id, tool, True, "preview",
            "described without performing (no side effect)")
        out["preview"] = conn.preview_write(
            {"op": op, "path": path, "body": body or {}})
        out["request_sha256"] = digest
        return out
    from .safety.tool_risk import risk_rank
    floor = _approval_floor()
    if req_risk and risk_rank(req_risk) >= risk_rank(floor):
        return _park_execution(agent_id, conn, meta, tool=tool,
                               connector=connector, op=op, path=path,
                               body=body, detail=detail, digest=digest,
                               req_risk=req_risk, floor=floor, goal_id=gid)
    return _perform_execution(agent_id, connector, op, path,
                              body if isinstance(body, dict) else None,
                              goal_id=gid, digest=digest, tool=tool,
                              approved=False)


def _park_execution(agent_id: str, conn, meta: dict, *, tool: str,
                    connector: str, op: str, path: str, body: dict | None,
                    detail: str, digest: str, req_risk: str, floor: str,
                    goal_id: int | None) -> dict:
    """Park one digest-bound execution behind a real approval row.

    The slot is reserved BEFORE the approval is created so a full backlog
    never strands an orphan approval in the queue; the approval id is bound
    to the slot right after. The approver sees the network-free preview and
    the request fingerprint — what they authorize is exactly what
    :func:`execute_commit` will verify before acting."""
    execution_id = secrets.token_urlsafe(12)
    now = time.time()
    backlog = enrolled = False
    with _sidecar_locked():
        sidecar = _load_sidecar()
        m = sidecar.get(agent_id)
        if m is not None:
            enrolled = True
            live = _prune_executions(m, now)
            if sum(1 for e in live.values()
                   if e.get("status") == "pending") >= _MAX_PENDING_EXECUTIONS:
                backlog = True
            else:
                live[execution_id] = {
                    "approval_id": None, "digest": digest,
                    "connector": connector, "op": op, "path": path[:500],
                    "goal_id": goal_id, "created_at": now,
                    "status": "pending"}
                _save_sidecar(sidecar)
    if not enrolled:
        return _screen_verdict(agent_id, tool, False, "not_enrolled",
                               "agent has a trust entry but no enrollment")
    if backlog:
        return _screen_verdict(
            agent_id, tool, False, "execution_backlog",
            f"{_MAX_PENDING_EXECUTIONS} executions already await decisions "
            "for this agent; wait for approvals or expiry")
    try:
        describe = (conn.preview_write({"op": op, "path": path,
                                        "body": body or {}})
                    if op != "get" else f"would GET {connector}{path}")
        from .world_model import open_world
        approval_id = open_world().create_approval(
            f"external-exec:{agent_id}:{tool}", risk=req_risk,
            scope=meta.get("department") or None,
            detail=(f"{describe}\nrequest sha256: {digest}\n"
                    f"{detail or ''}")[:2000],
            provenance="external_agents", requested_by=f"agent:{agent_id}")
    except Exception:
        # Nothing was authorized and nothing will fire — hand the reserved
        # slot back instead of stranding a 24h pending entry that has no
        # approval row and can never be committed.
        log.warning("external_agents: park failed for %r; releasing the "
                    "slot", agent_id, exc_info=True)
        _set_execution_status(agent_id, execution_id, None, drop=True,
                              expect="pending")
        return _screen_verdict(
            agent_id, tool, False, "park_failed",
            "could not create the approval; nothing was recorded — "
            "submit the request again")
    with _sidecar_locked():
        sidecar = _load_sidecar()
        entry = ((sidecar.get(agent_id) or {}).get("executions")
                 or {}).get(execution_id)
        if entry is not None:
            entry["approval_id"] = approval_id
            _save_sidecar(sidecar)
    _notify_approval(approval_id, agent_id, tool, req_risk,
                     meta.get("department") or "")
    out = _screen_verdict(
        agent_id, tool, False, "approval_required",
        f"risk {req_risk} is at/above the {floor} approval floor; re-send "
        "the identical request to the commit endpoint once approved")
    out.update({"requires_approval": True, "approval_id": approval_id,
                "execution_id": execution_id,
                "expires_at": now + _EXECUTION_TTL_SECONDS})
    return out


def execute_status(agent_id: str, execution_id: str) -> dict:
    """Poll one parked execution: ledger status joined with the live approval
    decision, so the agent knows when to re-send the request and commit."""
    meta = _load_sidecar().get(agent_id)
    entry = ((meta or {}).get("executions") or {}).get(str(execution_id))
    if entry is None:
        raise ExternalAgentsError(f"unknown execution {execution_id!r}")
    created = float(entry.get("created_at") or 0)
    status = str(entry.get("status") or "pending")
    now = time.time()
    if status == "pending" and now - created > _EXECUTION_TTL_SECONDS:
        status = "expired"
    elif status == "executing" and now - float(
            entry.get("claimed_at") or created) > _EXECUTING_GRACE_SECONDS:
        # The worker died between the claim and the terminal write: the
        # effect may or may not have fired. Never re-fire automatically —
        # the operator verifies in the system of record.
        status = "indeterminate"
    out = {"execution_id": str(execution_id), "status": status,
           "approval_id": entry.get("approval_id"),
           "connector": entry.get("connector"), "op": entry.get("op"),
           "expires_at": created + _EXECUTION_TTL_SECONDS}
    if status == "pending" and entry.get("approval_id"):
        try:
            from .world_model import open_world
            ap = open_world().get_approval(int(entry["approval_id"]))
            out["approval_status"] = ap.status if ap else "unknown"
        except Exception:
            out["approval_status"] = "unknown"
    return out


def execute_commit(agent_id: str, execution_id: str, request: dict, *,
                   shield: Any | None = None) -> dict:
    """Commit one approved execution — a digest-bound replay.

    The agent re-sends the IDENTICAL request; a sha256 mismatch voids the
    parked execution and counts as misbehavior (an attempt to swap the
    payload after approval). The admission chain runs again in full because
    the world may have changed while the approval sat in the queue —
    containment, revocation, ceilings, and budget all re-bind at commit
    time; the human's yes is necessary, not sufficient. At-most-once: the
    entry is claimed before the effect, so a racing or repeated commit
    reports the terminal status instead of re-firing."""
    if not enabled():
        return _screen_verdict(agent_id, "execute", False, "disabled",
                               "external agents plane is disabled")
    connector = str(request.get("connector") or "").strip().lower()
    op = str(request.get("op") or "").strip().lower()
    path = str(request.get("path") or "").strip()
    body = request.get("body")
    tool = (f"{connector}.{'read' if op == 'get' else 'write'}"
            if connector else "execute")
    entry = ((_load_sidecar().get(agent_id) or {}).get("executions")
             or {}).get(str(execution_id))
    if entry is None:
        return _screen_verdict(agent_id, tool, False, "unknown_execution",
                               f"unknown execution {str(execution_id)!r}")
    now = time.time()
    # Terminal state FIRST, regardless of age: a replayed commit on an
    # already-fired execution must always hear the terminal status — an
    # aged executed entry told "expired; submit it again" is an instruction
    # to duplicate the effect through a second park + approval.
    if entry.get("status") != "pending":
        status = str(entry.get("status") or "")
        if status == "executing" and now - float(
                entry.get("claimed_at")
                or entry.get("created_at") or 0) > _EXECUTING_GRACE_SECONDS:
            status = "indeterminate"
        out = _screen_verdict(
            agent_id, tool, False, "already_finished",
            f"execution already {status}; a commit never re-fires"
            + (" — verify in the system of record"
               if status == "indeterminate" else ""))
        out["status"] = status
        return out
    # The operator's connector allowlist re-binds at commit time: emptying
    # [external_agents] connectors is the documented kill switch, and an
    # approval parked before the switch flipped must not outrun it.
    if connector not in execute_connectors():
        return _screen_verdict(
            agent_id, tool, False, "connector_not_enabled",
            f"connector {connector!r} is no longer enabled for external "
            "execution ([external_agents] connectors)")
    if now - float(entry.get("created_at") or 0) > _EXECUTION_TTL_SECONDS:
        if not _set_execution_status(agent_id, str(execution_id), None,
                                     drop=True, expect="pending"):
            # A concurrent commit claimed it between our snapshot and the
            # void — report the live state instead of acting on the stale one.
            return execute_commit(agent_id, execution_id, request,
                                  shield=shield)
        return _screen_verdict(
            agent_id, tool, False, "execution_expired",
            "the parked execution expired before any effect; submit it "
            "again")
    if body is not None and not isinstance(body, dict):
        return _screen_verdict(agent_id, tool, False, "bad_body",
                               "body must be a JSON object")
    approval_id = entry.get("approval_id")
    if not approval_id:
        # The park never completed (approval creation failed after the slot
        # was reserved). Nothing was authorized and nothing fired — void the
        # slot and have the agent start over.
        _set_execution_status(agent_id, str(execution_id), None,
                              drop=True, expect="pending")
        return _screen_verdict(
            agent_id, tool, False, "park_incomplete",
            "the park never completed (no approval exists); submit the "
            "request again")
    digest = _request_digest(connector, op, path,
                             body if isinstance(body, dict) else None)
    if digest != entry.get("digest"):
        # Void only while still pending: a stale snapshot must never delete
        # an entry a concurrent (correct) commit has claimed and is
        # executing. The mismatch is misbehavior either way — count it.
        _set_execution_status(agent_id, str(execution_id), None,
                              drop=True, expect="pending")
        return _deny_and_count(
            agent_id, tool, "digest_mismatch",
            "the committed request does not match what was approved; the "
            "parked execution has been voided")
    approval = None
    if approval_id:
        try:
            from .world_model import open_world
            approval = open_world().get_approval(int(approval_id))
        except Exception:
            approval = None
    decision = getattr(approval, "status", None)
    if decision == "denied":
        _set_execution_status(agent_id, str(execution_id), "denied",
                              expect="pending")
        return _screen_verdict(agent_id, tool, False, "approval_denied",
                               "a human denied this action")
    if decision != "approved":
        out = _screen_verdict(agent_id, tool, False, "approval_pending",
                              "still awaiting a human decision")
        out.update({"requires_approval": True, "approval_id": approval_id,
                    "execution_id": str(execution_id)})
        return out
    risk = "low" if op == "get" else "high"
    deny, _meta, _decision, _risk = _screen_admission(
        agent_id, tool, risk=risk, detail="", shield=shield,
        correlation="execute_commit")
    if deny is not None:
        return deny
    if not _claim_execution(agent_id, str(execution_id)):
        out = _screen_verdict(
            agent_id, tool, False, "already_finished",
            "a concurrent commit claimed this execution first")
        return out
    verdict = _perform_execution(
        agent_id, connector, op, path,
        body if isinstance(body, dict) else None,
        goal_id=entry.get("goal_id"), digest=digest, tool=tool,
        approved=True)
    if verdict.get("rule") == "receipt_unavailable":
        # Nothing fired — hand the slot back so the agent can retry the
        # commit once the receipt store recovers.
        _set_execution_status(agent_id, str(execution_id), "pending",
                              expect="executing")
    else:
        _set_execution_status(agent_id, str(execution_id),
                              verdict.get("status") or "failed",
                              expect="executing")
    verdict["execution_id"] = str(execution_id)
    return verdict


def _claim_execution(agent_id: str, execution_id: str) -> bool:
    """Flip pending -> executing atomically; the loser of a race gets False."""
    with _sidecar_locked():
        sidecar = _load_sidecar()
        entry = ((sidecar.get(agent_id) or {}).get("executions")
                 or {}).get(execution_id)
        if entry is None or entry.get("status") != "pending":
            return False
        entry["status"] = "executing"
        entry["claimed_at"] = time.time()
        _save_sidecar(sidecar)
    return True


def _set_execution_status(agent_id: str, execution_id: str,
                          status: str | None, *, drop: bool = False,
                          expect: str | None = None) -> bool:
    """Mutate (or drop) one ledger entry under the lock.

    ``expect`` guards against acting on a stale snapshot: the change applies
    only while the entry's CURRENT status still matches — a drop decided
    against a pending snapshot must never delete an entry a concurrent
    commit has since claimed. Returns whether the change was applied."""
    with _sidecar_locked():
        sidecar = _load_sidecar()
        meta = sidecar.get(agent_id)
        if meta is None:
            return False
        ex = dict(meta.get("executions") or {})
        entry = ex.get(execution_id)
        if entry is None:
            return False
        if expect is not None and entry.get("status") != expect:
            return False
        if drop:
            ex.pop(execution_id, None)
        else:
            entry = dict(entry)
            entry["status"] = status
            if status in ("executed", "failed", "denied"):
                entry["finished_at"] = time.time()
                # The digest is spent — a finished entry must never re-fire.
                entry.pop("digest", None)
            ex[execution_id] = entry
        meta["executions"] = ex
        _save_sidecar(sidecar)
    return True


def _perform_execution(agent_id: str, connector: str, op: str, path: str,
                       body: dict | None, *, goal_id: int | None,
                       digest: str, tool: str, approved: bool) -> dict:
    """The effect itself, receipted.

    PREPARE is fsynced tamper-evident BEFORE any network I/O (no receipt =
    no effect, same posture as the internal governed-write path); the call
    goes through the governed-REST connector — the SSRF-safe,
    enterprise-egress-guarded request path — and a COMMIT (or, on a
    transport error, INDETERMINATE) receipt closes the transaction. The
    receipt carries field NAMES and the request fingerprint, never body
    values; the response is secret-redacted and bounded before it returns
    to the caller."""
    from .governed_actions import record_tool_lineage
    from .governed_rest import GOVERNED_REST_FACTORIES
    principal = f"agent:{agent_id}"
    fields = sorted(body) if isinstance(body, dict) and body else []
    receipt = {"path": path, "fields": fields, "request_sha256": digest}
    try:
        prepared = record_tool_lineage(
            goal_id, tool, receipt, actor=principal,
            sources=(f"external:{agent_id}",), transaction_id=digest[:16],
            phase="prepare", force=True, strict=True)
    except Exception:
        prepared = False
    if not prepared:
        return _screen_verdict(
            agent_id, tool, False, "receipt_unavailable",
            "could not persist the PREPARE receipt; refusing to act "
            "(fail-closed)")
    conn = GOVERNED_REST_FACTORIES[connector]()
    try:
        if op == "get":
            raw = conn.read({"path": path})
        else:
            raw = conn.write({"op": op, "path": path, "body": body or {}})
    except Exception as e:
        try:
            record_tool_lineage(
                goal_id, tool, receipt, actor=principal,
                transaction_id=digest[:16], phase="indeterminate",
                result=str(e)[:200], force=True)
        except Exception:  # pragma: no cover -- receipt is best-effort here
            log.warning("external_agents: INDETERMINATE receipt failed",
                        exc_info=True)
        _audit_execution(agent_id, connector, op, "failed", digest,
                         approved=approved, goal_id=goal_id)
        return {"allowed": True, "rule": "executed", "status": "failed",
                "result": "ERROR: the request failed in transit; its effect "
                          "is INDETERMINATE — verify in the system of record "
                          "before retrying"}
    outcome = "failed" if str(raw).lstrip().startswith("ERROR") else "executed"
    try:
        record_tool_lineage(
            goal_id, tool, receipt, actor=principal,
            transaction_id=digest[:16], phase="commit", result=outcome,
            force=True)
    except Exception:  # pragma: no cover -- effect already happened
        log.warning("external_agents: COMMIT receipt failed", exc_info=True)
    try:
        from .safety.secret_detector import redact
        result = redact(str(raw))[0][:_MAX_TEXT]
    except Exception:
        # The redactor erroring must not leak the raw response outward.
        result = f"({outcome}; response withheld — redaction unavailable)"
    if goal_id is not None:
        try:
            from .world_model import open_world
            open_world().append_event(
                int(goal_id), principal, "status",
                f"{op.upper()} {connector}{path} -> {outcome} "
                "(governed execution)")
        except Exception:  # pragma: no cover -- bookkeeping never blocks
            log.warning("external_agents: goal event append failed",
                        exc_info=True)
    try:
        with _sidecar_locked():
            sidecar = _load_sidecar()
            m = sidecar.get(agent_id)
            if m is not None:
                m["executions_total"] = int(
                    m.get("executions_total") or 0) + 1
                _save_sidecar(sidecar)
    except Exception:  # pragma: no cover -- bookkeeping never blocks
        log.warning("external_agents: execution meter failed", exc_info=True)
    _audit_execution(agent_id, connector, op, outcome, digest,
                     approved=approved, goal_id=goal_id)
    return {"allowed": True, "rule": "executed", "status": outcome,
            "result": result}


def _audit_execution(agent_id: str, connector: str, op: str, outcome: str,
                     digest: str, *, approved: bool,
                     goal_id: int | None) -> None:
    from .audit import EventKind, audit_event
    extra = {"goal_id": int(goal_id)} if goal_id is not None else {}
    audit_event(EventKind.EXTERNAL_ACTION_EXECUTED, agent="external_agents",
                external_agent=agent_id, connector=connector, op=op,
                outcome=outcome, request_sha256=digest, approved=approved,
                **extra)


# -- roster / detail (the dashboard's data) -----------------------------------

def roster() -> list[dict]:
    """Enrolled agents joined across the trust plane and the sidecar (never
    including token values — only which surfaces hold one)."""
    from .agent_trust import load_registry
    registry = load_registry()
    out = []
    for agent_id, meta in sorted(_load_sidecar().items()):
        agent = registry.get(agent_id)
        active, rule = agent.is_active() if agent else (False, "missing_trust")
        out.append({
            "id": agent_id,
            "platform": meta.get("platform", "custom"),
            "platform_label": PLATFORMS.get(
                str(meta.get("platform")), "Custom / other runtime"),
            "description": meta.get("description", ""),
            "owner": meta.get("owner", ""),
            "department": meta.get("department", ""),
            "enrolled_at": meta.get("enrolled_at"),
            "active": active,
            "lifecycle": rule,
            "max_risk": agent.max_risk if agent else None,
            "max_dollars": agent.max_dollars if agent else None,
            "spent_dollars": round(float(meta.get("spent_dollars") or 0.0), 4),
            "period": meta.get("period", "total"),
            "period_spent": round(_period_spent(meta), 4),
            "over_budget": bool(meta.get("over_budget")),
            "contained": bool(meta.get("contained")),
            "wall_violations": int(meta.get("wall_violations") or 0),
            "tool_risks": dict(meta.get("tool_risks") or {}),
            "runs": int(meta.get("runs") or 0),
            "last_run_at": meta.get("last_run_at"),
            "last_seen": meta.get("last_seen"),
            "executions_total": int(meta.get("executions_total") or 0),
            "pending_executions": sum(
                1 for e in (meta.get("executions") or {}).values()
                if isinstance(e, dict) and e.get("status") == "pending"
                and time.time() - float(e.get("created_at") or 0)
                <= _EXECUTION_TTL_SECONDS),
            "credentials": [s for s in TOKEN_SURFACES
                            if agent and getattr(agent, f"{s}_token", "")],
        })
    return out


def agent_detail(agent_id: str) -> dict:
    """One agent's roster row + its Operating Record footprint."""
    rows = [r for r in roster() if r["id"] == agent_id]
    if not rows:
        raise ExternalAgentsError(f"agent {agent_id!r} is not enrolled")
    row = rows[0]
    principal = f"agent:{agent_id}"
    try:
        from .world_model import open_world
        w = open_world()
        row["record"] = w.total_spend(owner=principal)
        row["episodes"] = [
            {"goal_id": e.goal_id, "started_at": e.started_at,
             "ended_at": e.ended_at, "outcome": e.outcome,
             "cost_dollars": e.cost_dollars}
            for e in w.list_episodes(limit=10, owner=principal)]
    except Exception as e:
        log.warning("external_agents: world lookup failed for %r: %s",
                    agent_id, e)
        row["record"] = {}
        row["episodes"] = []
    row["memory"] = _memory_counts().get(agent_id, {})
    return row


def status() -> dict:
    """Counts for the health surface and the dashboard header."""
    rows = roster()
    return {
        "enabled": enabled(),
        "enrolled": len(rows),
        "active": sum(1 for r in rows if r["active"]),
        "over_budget": sum(1 for r in rows if r["over_budget"]),
        "contained": sum(1 for r in rows if r["contained"]),
        "wall_violations": sum(r["wall_violations"] for r in rows),
        "platforms": sorted({r["platform"] for r in rows}),
        "runs": sum(r["runs"] for r in rows),
        "executions": sum(r["executions_total"] for r in rows),
        "pending_executions": sum(r["pending_executions"] for r in rows),
        "reported_dollars": round(
            sum(r["spent_dollars"] for r in rows), 4),
        "memory_contributions": sum(
            sum(kinds.values()) for kinds in _memory_counts().values()),
    }
