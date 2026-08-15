"""The agent-facing half of the bring-your-own-agent gateway.

Agents that run on OTHER platforms (Agentforce, Bedrock, Copilot Studio,
LangChain, custom runtimes) call these routes to be governed and accounted by
Lightwork while doing their work elsewhere:

* ``POST /api/v1/external/screen``     — ask before acting (trust ceilings,
  budget cutoff, Shield input scan, approval floor).
* ``POST /api/v1/external/runs``       — report a completed run; it lands as
  a first-class Operating Record goal owned by ``agent:<id>``.
* ``GET  /api/v1/external/approvals/{id}`` — poll a parked high-risk action.
* ``GET  /api/v1/external/openapi.json``  — the importable description of the
  three routes above (drops into Agentforce External Services / Bedrock
  action groups as-is).

Auth is the per-agent ``rest`` bearer minted on the /external-agents page —
or a platform-native credential pinned on the trust entry (signed Ed25519
request envelope, webhook-format HMAC, or platform JWT; see
``maverick.external_identity``) — NOT the dashboard session/bearer. The
``/api/v1/external/`` prefix is
therefore registered as self-authenticating (see ``auth.py`` and the
dashboard-token middleware in ``app.py``), same pattern as the HMAC
webhooks: an external platform has no dashboard credential, and
pressuring operators to share one across two trust domains would be worse.
Every route self-gates on ``[external_agents] enable`` (404 when off) and the
Gold entitlement (403), so including the router unconditionally is inert.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger("maverick.dashboard.external_gateway")

router = APIRouter(prefix="/api/v1/external", tags=["external-agents"])

# Per-caller request throttles (window: 60s). Budgets gate dollars; these
# gate volume, so a misbehaving or looping agent cannot hammer the gateway.
# Keyed by verified agent id — except the unauthenticated schema route,
# which keys by client address.
_RATE_LIMITS = {"screen": 120, "runs": 60, "approvals": 120, "openapi": 30,
                "heartbeat": 240, "memory": 60, "execute": 30,
                "executions": 120}
_RATE_BUCKET_CAP = 10_000
_rate_times: dict[tuple[str, str], deque[float]] = {}
_rate_lock = threading.Lock()


def _rate_limit(key: str, action: str) -> None:
    now = time.monotonic()
    limit = _RATE_LIMITS[action]
    bucket = (action, key)
    with _rate_lock:
        if bucket not in _rate_times and len(_rate_times) >= _RATE_BUCKET_CAP:
            for candidate, window in tuple(_rate_times.items()):
                while window and now - window[0] >= 60:
                    window.popleft()
                if not window:
                    _rate_times.pop(candidate, None)
            if len(_rate_times) >= _RATE_BUCKET_CAP:
                raise HTTPException(
                    status_code=429,
                    detail="gateway rate-limit capacity reached",
                    headers={"Retry-After": "60"})
        window = _rate_times.setdefault(bucket, deque())
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            raise HTTPException(
                status_code=429,
                detail=f"rate limit reached for {action} "
                       f"({limit}/minute); slow down and retry",
                headers={"Retry-After": "60"})
        window.append(now)


class ExternalRunIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    outcome: str = Field(max_length=20)
    summary: str = Field(default="", max_length=4000)
    steps: list[str] = Field(default_factory=list, max_length=50)
    cost_dollars: float = Field(default=0.0, ge=0, le=1_000_000)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)
    department: str = Field(default="", max_length=100)
    # Send a stable key per logical run and retries become exactly-once:
    # a replay returns the original goal instead of double-counting.
    idempotency_key: str = Field(default="", max_length=128)


class ExternalScreenIn(BaseModel):
    tool: str = Field(min_length=1, max_length=128)
    detail: str = Field(default="", max_length=4000)
    risk: str = Field(default="", max_length=10)


class ExternalMemoryIngestIn(BaseModel):
    kind: str = Field(pattern="^(success|failure|lesson)$")
    goal_text: str = Field(min_length=1, max_length=2000)
    reflection: str = Field(default="", max_length=2000)
    tools_used: list[str] = Field(default_factory=list, max_length=16)
    domain: str = Field(default="", max_length=100)


class ExternalMemoryRecallIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    domain: str = Field(default="", max_length=100)


class ExternalRunStartIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=4000)
    department: str = Field(default="", max_length=100)


class ExternalRunFinishIn(BaseModel):
    outcome: str = Field(max_length=20)
    summary: str = Field(default="", max_length=4000)
    steps: list[str] = Field(default_factory=list, max_length=50)
    cost_dollars: float = Field(default=0.0, ge=0, le=1_000_000)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0)
    idempotency_key: str = Field(default="", max_length=128)


# Deliberately permissive: the core adjudicates connector/op/path/body and
# answers with typed verdict rules (connector_not_enabled, bad_op, bad_path,
# bad_body, body_too_large...); pre-rejecting here would replace those
# readable verdicts with a 422. Only basic JSON types are enforced.
class ExternalExecuteIn(BaseModel):
    connector: str
    op: str
    path: str
    body: dict | None = None
    preview: bool = False
    detail: str = ""
    goal_id: int | None = None


class ExternalExecuteCommitIn(BaseModel):
    connector: str
    op: str
    path: str
    body: dict | None = None


def _gate() -> None:
    """Self-gate: 404 while the plane is off (the route doesn't exist for
    this deployment), 403 when the license lacks the paid capability."""
    from maverick import external_agents as xa
    if not xa.enabled():
        raise HTTPException(status_code=404, detail="not found")
    if not xa.entitled():
        raise HTTPException(
            status_code=403,
            detail="external-agent governance is a paid (Gold) add-on not "
                   "included in this license")


def _looks_like_jwt(token: str) -> bool:
    """Route an Authorization bearer: exactly three dot-separated segments
    whose first decodes to a JSON object is a JWT for the platform-identity
    verifier; everything else stays on the minted-bearer path. Minted tokens
    (``lw-rest-<urlsafe>``) can never contain a dot, so no collision."""
    if token.count(".") != 2:
        return False
    header = token.split(".", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))
        return isinstance(json.loads(raw), dict)
    except ValueError:
        return False


async def _require_agent(request: Request) -> str:
    """Dependency: resolve the caller to its enrolled agent id — solved
    BEFORE the body model, so an unauthenticated caller gets 401, never a
    422 that leaks the request schema.

    Resolution order: the Ed25519 request envelope (agent-id + timestamp +
    nonce + signature headers), the webhook-format HMAC (agent-id +
    timestamp + signature), a platform JWT on the Authorization header, then
    the minted per-agent ``rest`` bearer. The credential is the identity: no
    session, no CSRF surface, no fallback to the dashboard credential. Every
    refusal is the same generic 401 (so a rotated credential reads as
    "re-authenticate" and the reply never says whether the id exists or
    which stage refused); the precise rule goes to the server log only."""
    _gate()
    from maverick import external_identity
    from maverick.agent_trust import AgentTrustError, agent_for_token, load_trust_state
    try:
        # ONE trust snapshot per request (the grpc server pattern), so a
        # registry edit mid-request cannot split the auth decision.
        _, registry = load_trust_state()
    except AgentTrustError as e:
        log.warning("external gateway: trust state unreadable (fail-closed)")
        raise HTTPException(status_code=401,
                            detail="invalid credentials") from e
    agent_hdr = request.headers.get("x-lightwork-agent-id", "")
    ts = request.headers.get("x-maverick-timestamp", "")
    nonce = request.headers.get("x-lightwork-nonce", "")
    envelope_sig = request.headers.get("x-lightwork-request-signature", "")
    hmac_sig = request.headers.get("x-maverick-signature", "")
    if agent_hdr and ts and nonce and envelope_sig:
        scheme = "envelope"
        # Starlette caches the body, so the endpoint model still parses it.
        body = await request.body()
        agent, rule = external_identity.verify_agent_envelope(
            agent_hdr, body, ts, nonce, envelope_sig, registry=registry)
    elif agent_hdr and ts and hmac_sig:
        scheme = "hmac"
        body = await request.body()
        agent, rule = external_identity.verify_agent_hmac(
            agent_hdr, body, ts, hmac_sig, registry=registry)
    else:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not token:
            raise HTTPException(status_code=401,
                                detail="missing credentials")
        if _looks_like_jwt(token):
            scheme = "jwt"
            agent, rule = external_identity.verify_agent_jwt(
                token, registry=registry)
        else:
            scheme = "bearer"
            agent = agent_for_token(token, "rest", registry=registry)
            rule = "ok" if agent is not None else "invalid_token"
            if agent is not None and (agent.pubkey or agent.jwt_issuer
                                      or agent.hmac_secret_ref):
                # [external_agents] require_signed: an agent enrolled with a
                # strong credential must present it — its bearer alone no
                # longer authenticates. Token-only agents are unaffected.
                from maverick.config import get_external_agents
                if get_external_agents().get("require_signed"):
                    agent, rule = None, "require_signed"
    if agent is None:
        log.info("external gateway: %s auth refused (%s)", scheme, rule)
        raise HTTPException(status_code=401, detail="invalid credentials")
    from maverick import external_agents as xa
    xa.note_seen(agent.id)  # throttled credential-audit stamp
    return agent.id


def _shield():
    """The deployment's Shield instance for input scanning, or ``None``.

    Kernel rule: the platform never *requires* agent-shield. Screening
    degrades to the redaction + injection-tripwire layer when it is absent;
    when it IS present, a scanner error denies (enforced in core)."""
    try:
        from maverick_shield import Shield
        return Shield()
    except Exception:
        return None


@router.post("/runs", status_code=201)
async def external_run_report(
    payload: ExternalRunIn, agent_id: str = Depends(_require_agent),
) -> dict:
    """Ingest one completed external run onto the Operating Record."""
    _rate_limit(agent_id, "runs")
    from maverick import external_agents as xa
    try:
        out = xa.record_run(agent_id, payload.model_dump(), shield=_shield())
    except xa.ExternalAgentsError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return {"ok": True, **out}


@router.post("/runs/start", status_code=201)
async def external_run_start(
    payload: ExternalRunStartIn, agent_id: str = Depends(_require_agent),
) -> dict:
    """Open a LIVE run: the goal is visible on the Operating Record while
    the work happens, and the reply names the heartbeat interval the agent
    must honor or the run is reclaimed."""
    _rate_limit(agent_id, "runs")
    from maverick import external_agents as xa
    try:
        out = xa.start_run(agent_id, payload.model_dump(), shield=_shield())
    except xa.ExternalAgentsError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return {"ok": True, **out}


@router.post("/runs/{goal_id}/heartbeat")
async def external_run_heartbeat(
    goal_id: int, agent_id: str = Depends(_require_agent),
) -> dict:
    """Keep a live run alive and learn whether to continue — ``continue:
    false`` is the kill switch reaching mid-flight work."""
    _rate_limit(agent_id, "heartbeat")
    from maverick import external_agents as xa
    return xa.heartbeat(agent_id, goal_id)


@router.post("/runs/{goal_id}/finish")
async def external_run_finish(
    goal_id: int, payload: ExternalRunFinishIn,
    agent_id: str = Depends(_require_agent),
) -> dict:
    """Close a live run with its outcome, steps, and cost."""
    _rate_limit(agent_id, "runs")
    from maverick import external_agents as xa
    try:
        out = xa.finish_run(agent_id, goal_id, payload.model_dump(),
                            shield=_shield())
    except xa.ExternalAgentsError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return {"ok": True, **out}


@router.post("/screen")
async def external_screen(
    payload: ExternalScreenIn, agent_id: str = Depends(_require_agent),
) -> dict:
    """Decide a proposed action before the external agent takes it."""
    _rate_limit(agent_id, "screen")
    from maverick import external_agents as xa
    return xa.screen(agent_id, payload.tool, detail=payload.detail,
                     risk=payload.risk or None, shield=_shield())


@router.post("/memory/ingest", status_code=201)
async def external_memory_ingest(
    payload: ExternalMemoryIngestIn, agent_id: str = Depends(_require_agent),
) -> dict:
    """Deposit one experience record into the governed learning plane —
    the REST twin of the MCP fleet-ingest tool, for platforms that only
    speak HTTPS. Vendor provenance comes from the enrollment, never the
    caller. Refusals carry the plane's own reason (roster, scope, Shield)."""
    _rate_limit(agent_id, "memory")
    from maverick import external_agents as xa
    ok, reason = xa.memory_ingest(agent_id, payload.model_dump(),
                                  shield=_shield())
    if not ok:
        raise HTTPException(status_code=403, detail=reason)
    return {"ok": True}


@router.post("/memory/recall")
async def external_memory_recall(
    payload: ExternalMemoryRecallIn, agent_id: str = Depends(_require_agent),
) -> dict:
    """Governed memory read: department-scoped lessons for the asking agent.
    An empty context with a reason is a refusal, not an error — the caller
    proceeds without memory."""
    _rate_limit(agent_id, "memory")
    from maverick import external_agents as xa
    context, reason = xa.memory_recall(agent_id, payload.query,
                                       domain=payload.domain or None,
                                       shield=_shield())
    return {"context": context, "reason": reason}


@router.get("/approvals/{approval_id}")
async def external_approval_status(
    approval_id: int, agent_id: str = Depends(_require_agent),
) -> dict:
    """Poll a parked approval (the human decides in the dashboard queue)."""
    _rate_limit(agent_id, "approvals")
    from maverick import external_agents as xa
    try:
        out = xa.approval_status(approval_id)
    except xa.ExternalAgentsError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    # The approval row itself is cross-checked to this provenance in core;
    # any enrolled agent may poll only external_agents approvals.
    log.debug("external approval poll by %s: %s", agent_id, out["status"])
    return out


@router.post("/execute")
async def external_execute(
    payload: ExternalExecuteIn, agent_id: str = Depends(_require_agent),
) -> dict:
    """Perform one outbound action on the agent's behalf through a governed
    connector — the enforcement tier above /screen. Reads run immediately;
    writes park a digest-bound approval to commit later; every decision
    comes back as a typed verdict, never an exception."""
    _rate_limit(agent_id, "execute")
    from maverick import external_agents as xa
    return xa.execute(agent_id, payload.model_dump(), shield=_shield())


@router.get("/executions/{execution_id}")
async def external_execution_status(
    execution_id: str, agent_id: str = Depends(_require_agent),
) -> dict:
    """Poll a parked execution (ledger status joined with the approval
    decision) so the agent knows when to re-send the request and commit."""
    _rate_limit(agent_id, "executions")
    from maverick import external_agents as xa
    try:
        return xa.execute_status(agent_id, execution_id)
    except xa.ExternalAgentsError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/executions/{execution_id}/commit")
async def external_execution_commit(
    execution_id: str, payload: ExternalExecuteCommitIn,
    agent_id: str = Depends(_require_agent),
) -> dict:
    """Commit one approved execution: a digest-bound replay of the identical
    request. Shares the execute rate bucket — a commit can fire a write."""
    _rate_limit(agent_id, "execute")
    from maverick import external_agents as xa
    return xa.execute_commit(agent_id, execution_id, payload.model_dump(),
                             shield=_shield())


#: Hand-maintained on purpose: FastAPI's generated schema spans the whole
#: dashboard; external platforms need exactly these three operations, with
#: nothing to leak. Served without auth like /openapi.json (pure shape, no
#: data) so an operator can import it before any credential exists.
_OPENAPI: dict = {
    "openapi": "3.0.3",
    "info": {
        "title": "Lightwork External Agent Gateway",
        "version": "1",
        "description": (
            "Governance and run accounting for agents built on other "
            "platforms. Authenticate every call with the per-agent rest "
            "bearer minted on the Lightwork /external-agents page."),
    },
    "components": {
        "securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer"},
        },
    },
    "security": [{"bearer": []}],
    "paths": {
        "/api/v1/external/screen": {
            "post": {
                "operationId": "screenAction",
                "summary": "Ask before acting: is this action within my "
                           "ceilings, budget, and approval floor?",
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["tool"],
                        "properties": {
                            "tool": {"type": "string",
                                     "description": "Action/tool name"},
                            "detail": {"type": "string",
                                       "description": "What will be done"},
                            "risk": {"type": "string",
                                     "enum": ["low", "medium", "high"]},
                        }}}}},
                "responses": {"200": {"description": "Decision", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "allowed": {"type": "boolean"},
                            "rule": {"type": "string"},
                            "reason": {"type": "string"},
                            "requires_approval": {"type": "boolean"},
                            "approval_id": {"type": "integer"},
                        }}}}}},
            },
        },
        "/api/v1/external/runs": {
            "post": {
                "operationId": "reportRun",
                "summary": "Report a completed run for the Operating Record",
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["title", "outcome"],
                        "properties": {
                            "title": {"type": "string"},
                            "outcome": {"type": "string",
                                        "enum": ["success", "failure"]},
                            "summary": {"type": "string"},
                            "steps": {"type": "array",
                                      "items": {"type": "string"}},
                            "cost_dollars": {"type": "number"},
                            "input_tokens": {"type": "integer"},
                            "output_tokens": {"type": "integer"},
                            "tool_calls": {"type": "integer"},
                            "duration_seconds": {"type": "number"},
                            "department": {"type": "string"},
                            "idempotency_key": {
                                "type": "string",
                                "description": "Stable key per logical run; "
                                               "retries return the original "
                                               "record instead of "
                                               "double-counting."},
                        }}}}},
                "responses": {"201": {"description": "Recorded", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "ok": {"type": "boolean"},
                            "goal_id": {"type": "integer"},
                            "over_budget": {"type": "boolean"},
                        }}}}}},
            },
        },
        "/api/v1/external/runs/start": {
            "post": {
                "operationId": "startRun",
                "summary": "Open a LIVE run (visible while working; "
                           "heartbeat required)",
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["title"],
                        "properties": {
                            "title": {"type": "string"},
                            "summary": {"type": "string"},
                            "department": {"type": "string"},
                        }}}}},
                "responses": {"201": {"description": "Opened", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "ok": {"type": "boolean"},
                            "goal_id": {"type": "integer"},
                            "heartbeat_seconds": {
                                "type": "number",
                                "description": "Beat at least this often "
                                               "or the run is reclaimed."},
                        }}}}}},
            },
        },
        "/api/v1/external/runs/{goal_id}/heartbeat": {
            "post": {
                "operationId": "heartbeatRun",
                "summary": "Keep a live run alive; continue=false is the "
                           "kill switch — stop working when you see it",
                "parameters": [{"name": "goal_id", "in": "path",
                                "required": True,
                                "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Liveness", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "continue": {"type": "boolean"},
                            "reason": {"type": "string"},
                        }}}}}},
            },
        },
        "/api/v1/external/runs/{goal_id}/finish": {
            "post": {
                "operationId": "finishRun",
                "summary": "Close a live run with outcome, steps, and cost",
                "parameters": [{"name": "goal_id", "in": "path",
                                "required": True,
                                "schema": {"type": "integer"}}],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["outcome"],
                        "properties": {
                            "outcome": {"type": "string",
                                        "enum": ["success", "failure"]},
                            "summary": {"type": "string"},
                            "steps": {"type": "array",
                                      "items": {"type": "string"}},
                            "cost_dollars": {"type": "number"},
                            "input_tokens": {"type": "integer"},
                            "output_tokens": {"type": "integer"},
                            "tool_calls": {"type": "integer"},
                            "duration_seconds": {"type": "number"},
                            "idempotency_key": {"type": "string"},
                        }}}}},
                "responses": {"200": {"description": "Closed", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "ok": {"type": "boolean"},
                            "goal_id": {"type": "integer"},
                            "over_budget": {"type": "boolean"},
                            "over_wall": {"type": "boolean"},
                        }}}}}},
            },
        },
        "/api/v1/external/memory/ingest": {
            "post": {
                "operationId": "memoryIngest",
                "summary": "Deposit an experience record into the governed "
                           "learning plane (fleet memory)",
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "required": ["kind", "goal_text"],
                        "properties": {
                            "kind": {"type": "string",
                                     "enum": ["success", "failure",
                                              "lesson"]},
                            "goal_text": {"type": "string"},
                            "reflection": {"type": "string"},
                            "tools_used": {"type": "array",
                                           "items": {"type": "string"}},
                            "domain": {"type": "string"},
                        }}}}},
                "responses": {"201": {"description": "Deposited", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}}}}}}},
            },
        },
        "/api/v1/external/memory/recall": {
            "post": {
                "operationId": "memoryRecall",
                "summary": "Read governed memory scoped to this agent's "
                           "permitted departments",
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["query"],
                        "properties": {
                            "query": {"type": "string"},
                            "domain": {"type": "string"},
                        }}}}},
                "responses": {"200": {"description": "Context", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "context": {"type": "string"},
                            "reason": {"type": "string"},
                        }}}}}},
            },
        },
        "/api/v1/external/approvals/{approval_id}": {
            "get": {
                "operationId": "approvalStatus",
                "summary": "Poll a parked high-risk action",
                "parameters": [{"name": "approval_id", "in": "path",
                                "required": True,
                                "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Status", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "approval_id": {"type": "integer"},
                            "status": {"type": "string"},
                            "risk": {"type": "string"},
                        }}}}}},
            },
        },
        "/api/v1/external/execute": {
            "post": {
                "operationId": "executeAction",
                "summary": "Perform one action through a governed connector "
                           "(reads run now; writes park a digest-bound "
                           "approval)",
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "required": ["connector", "op", "path"],
                        "properties": {
                            "connector": {"type": "string",
                                          "description": "Enabled governed "
                                                         "connector name"},
                            "op": {"type": "string",
                                   "enum": ["get", "post", "put", "patch",
                                            "delete"]},
                            "path": {"type": "string"},
                            "body": {"type": "object"},
                            "preview": {
                                "type": "boolean",
                                "description": "Describe the write without "
                                               "performing it (no side "
                                               "effect)."},
                            "detail": {"type": "string",
                                       "description": "What will be done"},
                            "goal_id": {
                                "type": "integer",
                                "description": "Bind the effect to one of "
                                               "this agent's live runs."},
                        }}}}},
                "responses": {"200": {"description": "Verdict", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "allowed": {"type": "boolean"},
                            "rule": {"type": "string"},
                            "reason": {"type": "string"},
                            "status": {"type": "string"},
                            "result": {"type": "string"},
                            "requires_approval": {"type": "boolean"},
                            "approval_id": {"type": "integer"},
                            "execution_id": {"type": "string"},
                            "expires_at": {"type": "number"},
                            "preview": {"type": "string"},
                            "request_sha256": {"type": "string"},
                        }}}}}},
            },
        },
        "/api/v1/external/executions/{execution_id}": {
            "get": {
                "operationId": "executionStatus",
                "summary": "Poll a parked execution; commit once its "
                           "approval is granted",
                "parameters": [{"name": "execution_id", "in": "path",
                                "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"200": {"description": "Status", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "execution_id": {"type": "string"},
                            "status": {"type": "string"},
                            "approval_id": {"type": "integer"},
                            "connector": {"type": "string"},
                            "op": {"type": "string"},
                            "expires_at": {"type": "number"},
                            "approval_status": {"type": "string"},
                        }}}}}},
            },
        },
        "/api/v1/external/executions/{execution_id}/commit": {
            "post": {
                "operationId": "commitExecution",
                "summary": "Commit an approved execution by re-sending the "
                           "byte-identical request",
                "parameters": [{"name": "execution_id", "in": "path",
                                "required": True,
                                "schema": {"type": "string"}}],
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "required": ["connector", "op", "path"],
                        "properties": {
                            "connector": {"type": "string"},
                            "op": {"type": "string",
                                   "enum": ["get", "post", "put", "patch",
                                            "delete"]},
                            "path": {"type": "string"},
                            "body": {"type": "object"},
                        }}}}},
                "responses": {"200": {"description": "Verdict", "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "allowed": {"type": "boolean"},
                            "rule": {"type": "string"},
                            "reason": {"type": "string"},
                            "status": {"type": "string"},
                            "result": {"type": "string"},
                            "execution_id": {"type": "string"},
                        }}}}}},
            },
        },
    },
}


@router.get("/openapi.json")
async def external_openapi(request: Request) -> dict:
    """The importable gateway description (Agentforce External Services /
    Bedrock action groups consume this file directly). No bearer required —
    pure shape, no data, and the operator needs it before credentials exist —
    but it still 404s while the plane is off."""
    from maverick import external_agents as xa
    if not xa.enabled():
        raise HTTPException(status_code=404, detail="not found")
    client = request.client.host if request.client else "unknown"
    _rate_limit(client, "openapi")
    return _OPENAPI
