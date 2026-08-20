"""REST API for Maverick (mounted at /api/v1).

v0.1.6: BackgroundTask runner moved to maverick.runner.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from maverick.runner import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_DOLLARS,
    DEFAULT_MAX_WALL_SECONDS,
)
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from ._shared import (
    _get_sse_semaphore,
    _world,
    require_provider_or_400,
)
from ._shared import _world_cache as _world_cache
from .api_schemas import (
    AnswerIn,
    AttachmentOut,
    ChildIn,
    ComposeIn,
    ConflictPreflightIn,
    DeliverableEditIn,
    FeedbackIn,
    GoalEventOut,
    GoalEventsResponse,
    GoalIn,
    GoalOut,
    HaltIn,
    OutcomeIn,
    RetitleIn,
    SignoffIn,
)
from .auth import (
    assert_goal_access,
    assert_project_access,
    auth_genuinely_off,
    caller_principal,
    caller_suites,
    can_access_goal_principal,
    execution_user_id_from_request,
    list_accessible_goals,
    require_permission,
    require_qualified_attorney,
    require_suite,
    search_accessible_goals,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["v1"])

_DELIVERABLE_EDIT_EXACT_SIMILARITY_CHARS = 20_000

_DELIVERABLE_EDIT_SIMILARITY_SAMPLE_CHARS = 4_000

def _sample_deliverable_text(text: str) -> str:
    """Return a bounded head/tail sample for large deliverable similarity checks."""
    if len(text) <= _DELIVERABLE_EDIT_SIMILARITY_SAMPLE_CHARS:
        return text
    head = _DELIVERABLE_EDIT_SIMILARITY_SAMPLE_CHARS // 2
    tail = _DELIVERABLE_EDIT_SIMILARITY_SAMPLE_CHARS - head
    return text[:head] + text[-tail:]

def _deliverable_edit_similarity(original: str, revised: str) -> float:
    """Compute edit similarity without unbounded SequenceMatcher CPU cost."""
    if max(len(original), len(revised)) > _DELIVERABLE_EDIT_EXACT_SIMILARITY_CHARS:
        original = _sample_deliverable_text(original)
        revised = _sample_deliverable_text(revised)
    return difflib.SequenceMatcher(None, original, revised).ratio()

_IDEMPOTENCY_CHANNEL = "idempotency:api:goals"

def _require_halt_permission(request: Request) -> None:
    """The retained on-box halt is an operator action."""
    require_permission(request, "operate")

def _to_goal_out(g) -> GoalOut:
    return GoalOut(
        id=g.id, status=g.status, title=g.title,
        description=g.description, result=g.result,
        project_id=getattr(g, "project_id", None),
    )

def _validated_goal_filing(
    request: Request,
    world,
    principal: str | None,
    *,
    domain: str | None,
    project_id: int | None,
) -> tuple[str | None, int | None]:
    """Validate and return the immutable domain/matter admission context."""
    domain = (domain or "").strip() or None

    # Verified users never create loose work. A client matter and legal pack
    # are part of the authorization context, not mutable metadata to attach
    # after accepting the paid run. Global admins have no ethical-wall bypass:
    # the exact principal must have an active membership row too.
    if principal is not None:
        if project_id is None:
            raise HTTPException(
                status_code=400,
                detail="project_id is required for authenticated goal creation",
            )
        if world.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="no such project")
        assert_project_access(request, project_id, world=world)
        if domain is None:
            raise HTTPException(
                status_code=400,
                detail="a legal specialist domain is required for authenticated goal creation",
            )

    if domain is None:
        return domain, project_id

    from maverick.domain import enabled_domains, suite_for

    profile = enabled_domains().get(domain)
    if profile is None:
        raise HTTPException(status_code=400, detail=f"unknown specialist: {domain!r}")
    suite = suite_for(domain)
    require_suite(request, suite)
    if principal is not None:
        if suite != "legal":
            raise HTTPException(
                status_code=400,
                detail="authenticated law-firm goals require a legal specialist domain",
            )
        terminal_gate = profile.workflow[-1].gate if profile.workflow else None
        if terminal_gate not in {"review", "approval"}:
            raise HTTPException(
                status_code=400,
                detail="the legal specialist must end with a review or approval gate",
            )
    return domain, project_id

@router.post("/conflicts/preflight")
async def conflict_preflight(
    request: Request,
    payload: ConflictPreflightIn,
) -> dict:
    """Attorney-only exact-name check with an intentionally opaque result.

    This endpoint is an unavoidable firm-wide existence oracle, so even a
    static deployment bearer with a privileged RBAC assignment is refused.  A
    named verified attorney learns only clear versus potential conflict; never
    the matching client, matter, party, or count.
    """
    principal = require_qualified_attorney(request)
    names = []
    if (payload.client_name or "").strip():
        names.append((payload.client_name or "").strip())
    names.extend(str(name).strip() for name in payload.adverse_parties)
    from maverick.audit import EventKind, audit_event

    try:
        audited = audit_event(
            EventKind.MATTER_INTAKE,
            agent=principal,
            actor=principal,
            operation="preflight",
            candidate_count=len(names),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="conflict-check audit is temporarily unavailable",
        ) from exc
    if not audited:
        raise HTTPException(
            status_code=503,
            detail="conflict-check audit is temporarily unavailable",
        )
    from maverick.world_model import PotentialConflict

    try:
        _world().check_potential_conflicts(names)
    except PotentialConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="intake could not be cleared; conflicts-counsel review required",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"clear": True}

@router.post("/goals", response_model=GoalOut, status_code=201)
async def create_goal(
    request: Request,
    payload: GoalIn,
    bg: BackgroundTasks,
    response: Response,
) -> GoalOut:
    require_permission(request, "operate")
    require_provider_or_400()
    principal = caller_principal(request)
    w = _world()
    # Shared sliding-window cap across /chat/send + this route, so a
    # runaway loop can't spawn unbounded (paid) goals.
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request)
    title = payload.title
    description = payload.description
    if payload.template:
        from maverick.templates import load_template
        try:
            tpl = load_template(payload.template)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except FileNotFoundError as e:
            # Don't echo the raw error: its str() carries the absolute on-disk
            # template path. Reflect the caller's own template name instead; the
            # original (with path) stays chained for server-side logs.
            raise HTTPException(
                status_code=404, detail=f"template not found: {payload.template!r}"
            ) from e
        try:
            title, description = tpl.render(**(payload.params or {}))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    title = (title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    # Validate the complete legal filing context before accepting or replaying
    # the paid run.
    domain, project_id = _validated_goal_filing(
        request,
        w,
        principal,
        domain=payload.domain,
        project_id=payload.project_id,
    )
    # Idempotency-Key (optional, RFC-style): a client whose POST times out at the
    # LB and retries must not double-create / double-bill a paid run -- and on a
    # multi-replica deployment the retry can land on a different replica, so the
    # dedup must be in the shared store (reusing the backend-agnostic
    # mark/lookup_processed_message primitive). The key is scoped to the caller so
    # it can't collide across principals. A replay returns the ORIGINAL goal and
    # dispatches no second run.
    idem_key = (request.headers.get("Idempotency-Key") or "").strip()
    idem_ext = ""
    if idem_key:
        if len(idem_key) > 255:
            raise HTTPException(status_code=400, detail="Idempotency-Key too long (max 255)")
        idem_ext = f"{caller_principal(request) or ''}:{idem_key}"
        prior = w.lookup_processed_message(_IDEMPOTENCY_CHANNEL, idem_ext)
        if prior is not None:
            g0 = w.get_goal(prior)
            if g0 is not None:
                assert_goal_access(request, g0)
                response.headers["Location"] = f"/api/v1/goals/{g0.id}"
                response.headers["Idempotency-Replayed"] = "true"
                return _to_goal_out(g0)  # replay: original goal, no new run
    if principal is not None:
        # The WorldModel repeats the membership predicate in the INSERT itself;
        # the earlier request check gives a clean 404, while this closes a
        # revocation race between authorization and durable creation.
        goal_id = w.create_matter_goal(
            title[:200],
            description,
            principal=principal,
            domain=domain or "",
            project_id=project_id,
        )
        if goal_id is None:
            raise HTTPException(status_code=404, detail="no such project")
    else:
        goal_id = w.create_goal(
            title[:200],
            description,
            owner="",
            domain=domain or "",
            project_id=project_id,
        )
    if idem_key and not w.mark_message_processed(
        _IDEMPOTENCY_CHANNEL, idem_ext, goal_id=goal_id
    ):
        # Lost a concurrent race on the same key: return the winner's goal and do
        # NOT dispatch a run for this never-run goal row. Mark the loser
        # terminal so it cannot remain a permanently-pending ghost in the
        # operator worklist.
        prior = w.lookup_processed_message(_IDEMPOTENCY_CHANNEL, idem_ext)
        g0 = w.get_goal(prior) if prior is not None else None
        if g0 is not None:
            try:
                w.set_goal_status(
                    goal_id,
                    "cancelled",
                    result="superseded by a concurrent idempotent request",
                )
            except Exception:
                log.warning(
                    "could not mark idempotency-race loser goal #%s cancelled",
                    goal_id,
                    exc_info=True,
                )
            # Cleanup is independent of whether this request may still see the
            # winner (for example, membership could be revoked mid-request).
            # Never leave the race-loser runnable merely because the safe
            # winner response is now hidden by the matter wall.
            assert_goal_access(request, g0)
            response.headers["Location"] = f"/api/v1/goals/{g0.id}"
            response.headers["Idempotency-Replayed"] = "true"
            return _to_goal_out(g0)
        # A claim without its referenced goal is indeterminate shared-store
        # state. Never return the newly-created goal as accepted: it was not
        # dispatched, and doing so leaves the client polling forever.
        try:
            w.set_goal_status(
                goal_id,
                "blocked",
                result="idempotency claim is temporarily indeterminate",
            )
        except Exception:
            log.warning(
                "could not mark indeterminate idempotency goal #%s blocked",
                goal_id,
                exc_info=True,
            )
        raise HTTPException(
            status_code=503,
            detail="idempotency claim is temporarily indeterminate; retry shortly",
            headers={"Retry-After": "1"},
        )
    from maverick.runner import run_goal_in_background_async
    # Enforce server-side execution caps even when callers request larger values.
    max_dollars = min(payload.max_dollars, DEFAULT_MAX_DOLLARS)
    max_wall_seconds = min(payload.max_wall_seconds, DEFAULT_MAX_WALL_SECONDS)
    max_depth = min(payload.max_depth, DEFAULT_MAX_DEPTH)

    allowed_suites = caller_suites(request)
    user_id = execution_user_id_from_request(request)
    if user_id:
        bg.add_task(
            run_goal_in_background_async, goal_id,
            max_dollars, max_wall_seconds, max_depth,
            channel="api", user_id=user_id,
            concurrency_principal=principal,
            allowed_suites=allowed_suites,
        )
    else:
        bg.add_task(
            run_goal_in_background_async, goal_id,
            max_dollars, max_wall_seconds, max_depth,
            concurrency_principal=principal,
            allowed_suites=allowed_suites,
        )
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=500, detail="goal vanished after create")
    response.headers["Location"] = f"/api/v1/goals/{goal_id}"
    response.headers["Idempotency-Replayed"] = "false"
    return _to_goal_out(g)

@router.get("/goals", response_model=list[GoalOut])
async def list_goals(
    request: Request,
    response: Response,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[GoalOut]:
    """List goals (newest first), paginated.

    Council perf fix: previous version pulled every goal ever into
    Python via ``list_goals()``, then sliced. Now the LIMIT/OFFSET are
    pushed to SQL.

    Matter-scoped: authenticated callers see only rows reachable through an
    active matter membership (plus their own legacy unfiled rows). Global
    admins may see all unfiled legacy rows, but never bypass a matter ACL.
    """
    w = _world()
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    goals = list_accessible_goals(
        request, w, status=status,
        limit=limit + 1, offset=offset, order="desc",
    )
    has_more = len(goals) > limit
    goals = goals[:limit]
    response.headers["Pagination-Limit"] = str(limit)
    response.headers["Pagination-Offset"] = str(offset)
    response.headers["Pagination-Has-More"] = str(has_more).lower()
    if has_more:
        response.headers["Pagination-Next-Offset"] = str(offset + limit)
    return [_to_goal_out(g) for g in goals]

@router.get("/goals/search", response_model=list[GoalOut])
async def search_goals(request: Request, q: str, limit: int = 50) -> list[GoalOut]:
    """Search across runs (goals) by text in title / description / result.

    Matter-scoped exactly like the list route; even a global admin searches a
    matter row only with an active membership. Declared before
    ``/goals/{goal_id}`` so the literal ``search`` path wins over the int param.
    """
    query = (q or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 50), 200))
    w = _world()
    goals = search_accessible_goals(request, w, query, limit=limit)
    return [_to_goal_out(g) for g in goals]

@router.get("/goals/{goal_id}", response_model=GoalOut)
async def get_goal(request: Request, goal_id: int) -> GoalOut:
    g = _world().get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    return _to_goal_out(g)

@router.get("/goals/{goal_id}/events", response_model=GoalEventsResponse)
async def goal_events(
    request: Request, goal_id: int, since: int = 0, limit: int = 200,
) -> GoalEventsResponse:
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    events = w.goal_events(goal_id, since_id=since, limit=max(1, min(limit, 500)))
    return GoalEventsResponse(
        status=g.status,
        result=g.result,
        next_id=events[-1].id if events else since,
        events=[
            GoalEventOut(id=e.id, agent=e.agent, kind=e.kind,
                         content=e.content, ts=e.ts)
            for e in events
        ],
    )

def _sse_event(e) -> str:
    """Format one goal event as a Server-Sent Event frame."""
    data = json.dumps({"id": e.id, "agent": e.agent, "kind": e.kind,
                       "content": e.content, "ts": e.ts})
    kind = str(e.kind or "message").replace("\n", " ").replace("\r", " ")
    return f"id: {e.id}\nevent: {kind}\ndata: {data}\n\n"

_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "blocked", "error"})

_SSE_POLL_INTERVAL = 0.5

_SSE_MAX_POLL_INTERVAL = 5.0

_SSE_IDLE_HEARTBEAT_EVERY = 30.0

_SSE_MAX_STREAM_SECONDS = 300.0

_SSE_MAX_BATCH = 200


class _SSEAccessRevoked(Exception):
    """Internal control flow: terminate an admitted stream without a leak."""


async def _require_live_sse_access(check) -> None:
    if not await run_in_threadpool(check):
        raise _SSEAccessRevoked

@router.get("/goals/{goal_id}/events/stream")
async def goal_events_stream(
    request: Request, goal_id: int, since: int = 0, limit: int = 0,
    poll: float = 1.0,
) -> StreamingResponse:
    """Real-time **SSE** stream of a goal's events (`text/event-stream`).

    Tails the durable `goal_events` log (so it works across the worker/dashboard
    process split, unlike an in-process bus): emits each new event as it lands,
    ends when the goal reaches a terminal status with no more events, or on
    client disconnect. ``limit`` (>0) closes after N events � used by tests and
    bounded consumers. ``poll`` is accepted for compatibility but ignored; the
    server controls polling cadence and idle backoff.
    """
    del poll
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    principal = caller_principal(request)

    def _live_access() -> bool:
        # The stream can outlive the request that admitted it.  Re-resolve the
        # exact matter membership before every durable read and every emitted
        # frame so revocation closes an already-open stream immediately.
        current = w.get_goal(goal_id)
        return current is not None and can_access_goal_principal(
            principal, current, world=w,
        )

    sem = _get_sse_semaphore()
    if sem.locked():
        raise HTTPException(
            status_code=503,
            detail="too many concurrent event streams; retry shortly",
            headers={"Retry-After": "5"},
        )
    await sem.acquire()

    async def _gen():
        started = asyncio.get_running_loop().time()
        last = since
        sent = 0
        idle_for = 0.0
        poll_interval = _SSE_POLL_INTERVAL
        try:
            await _require_live_sse_access(_live_access)
            yield ": connected\n\n"   # open the stream immediately
            while True:
                if await request.is_disconnected():
                    break
                if (asyncio.get_running_loop().time() - started) >= _SSE_MAX_STREAM_SECONDS:
                    await _require_live_sse_access(_live_access)
                    yield "event: timeout\ndata: {\"detail\": \"stream lifetime exceeded\"}\n\n"
                    return
                await _require_live_sse_access(_live_access)
                events = await run_in_threadpool(
                    w.goal_events, goal_id, last, _SSE_MAX_BATCH)
                await _require_live_sse_access(_live_access)
                for e in events:
                    await _require_live_sse_access(_live_access)
                    yield _sse_event(e)
                    last = e.id
                    sent += 1
                    if limit and sent >= limit:
                        return
                await _require_live_sse_access(_live_access)
                cur = await run_in_threadpool(w.get_goal, goal_id)
                await _require_live_sse_access(_live_access)
                if events:
                    idle_for = 0.0
                    poll_interval = _SSE_POLL_INTERVAL
                else:
                    idle_for += poll_interval
                    if idle_for >= _SSE_IDLE_HEARTBEAT_EVERY:
                        await _require_live_sse_access(_live_access)
                        yield ": heartbeat\n\n"
                        idle_for = 0.0
                    poll_interval = min(_SSE_MAX_POLL_INTERVAL, poll_interval * 1.5)
                if cur is not None and cur.status in _TERMINAL_STATUSES:
                    if len(events) < _SSE_MAX_BATCH:
                        await _require_live_sse_access(_live_access)
                        yield f"event: end\ndata: {json.dumps({'status': cur.status})}\n\n"
                        return
                    # A full batch means more backlog may remain: keep draining
                    # (without sleeping) and end only once a read comes up short.
                    continue
                await asyncio.sleep(poll_interval)
        except (_SSEAccessRevoked, asyncio.CancelledError):
            return
        finally:
            sem.release()

    return StreamingResponse(_gen(), media_type="text/event-stream")

@router.post("/goals/{goal_id}/answer", status_code=204)
async def answer_question(request: Request, goal_id: int, payload: AnswerIn) -> None:
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    require_permission(request, "operate")
    answer = (payload.answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer is required")
    qs = w.open_questions(goal_id=goal_id)
    if not any(q.id == payload.question_id for q in qs):
        raise HTTPException(status_code=404, detail="no such open question for this goal")
    w.answer(payload.question_id, answer)

@router.post(
    "/goals/{goal_id}/attachments", response_model=AttachmentOut, status_code=201,
)
async def upload_attachment(
    request: Request, goal_id: int, bg: BackgroundTasks,
    file: UploadFile = File(...),
) -> AttachmentOut:
    """Upload a file of any supported kind and attach it to a goal.

    Text, images, audio, video, PDFs, and Office/OpenDocument files are
    accepted (executables and generic archives are always denied by
    magic-byte sniffing). Size and mime-type validation are enforced
    server-side; the agent's `list_attachments` tool exposes the uploaded
    set, images are auto-embedded as vision blocks and PDFs as document
    blocks on the first message.
    """
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)

    from maverick.attachments import (
        MAX_FILE_BYTES,
        AttachmentRejected,
        store,
    )

    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"file too large: {len(data)} bytes (limit {MAX_FILE_BYTES})"
            ),
        )

    mime = file.content_type or "application/octet-stream"
    filename = file.filename or "upload"

    existing = sum(a.size_bytes for a in w.list_attachments(goal_id))
    try:
        stored = store(
            goal_id,
            filename=filename,
            mime=mime,
            data=data,
            existing_total=existing,
        )
    except AttachmentRejected as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    aid = w.add_attachment(
        goal_id=goal_id,
        filename=stored.filename,
        mime=stored.mime,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        path=str(stored.path),
    )
    # Companion generation (transcripts / extracted text) off the request
    # path; best-effort, never raises.
    from maverick.attachments import generate_companions
    bg.add_task(generate_companions, w, goal_id)
    return AttachmentOut(
        id=aid,
        filename=stored.filename,
        mime=stored.mime,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )

def _csv_formula_safe(value: object) -> object:
    """Neutralize spreadsheet formulas while preserving ordinary CSV values."""
    from maverick.tools.spreadsheet import _neutralize_formula

    return _neutralize_formula(value)

@router.get("/goals/{goal_id}/attachments", response_model=list[AttachmentOut])
async def list_goal_attachments(request: Request, goal_id: int) -> list[AttachmentOut]:
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    return [
        AttachmentOut(
            id=a.id, filename=a.filename, mime=a.mime,
            size_bytes=a.size_bytes, sha256=a.sha256,
        )
        for a in w.list_attachments(goal_id)
    ]

@router.get("/goals/{goal_id}/attachments/{attachment_id}/download")
async def download_goal_attachment(
    request: Request, goal_id: int, attachment_id: int,
) -> Response:
    """Serve one stored attachment back to the browser (goal-page panel).

    Same access rules as the list; the bytes come only from the encrypted local
    store. Served as a download with
    the ORIGINAL filename � never inline, so a crafted HTML/SVG upload can't
    execute in the dashboard origin.
    """
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    match = next(
        (a for a in w.list_attachments(goal_id) if a.id == attachment_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="no such attachment")
    from pathlib import Path as _Path
    path = _Path(match.path)
    if not path.is_file():
        raise HTTPException(
            status_code=410, detail="attachment bytes not on this host")
    from maverick.attachments import AttachmentRejected, read_bytes
    try:
        content = await run_in_threadpool(read_bytes, path, match.sha256)
    except AttachmentRejected as exc:
        raise HTTPException(
            status_code=410,
            detail="attachment bytes failed integrity verification",
        ) from exc
    safe_name = match.filename.replace('"', "_")
    return Response(
        content=content,
        media_type=match.mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )

@router.post("/outcomes", status_code=204)
async def record_outcome(request: Request, payload: OutcomeIn) -> None:
    """Ingest a real downstream outcome for a past episode (the grounded reward).

    This direct, matter-ACL-gated endpoint lets a firm member ground local
    improvement in a verified result. ``value`` is clamped to [0, 1] by the
    store; there is no external correlation-key lookup.
    """
    require_permission(request, "operate")
    goal_id = int(payload.goal_id)
    episode_id = int(payload.episode_id)
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    if not w.episode_exists(goal_id, episode_id):
        raise HTTPException(status_code=404, detail="no such episode")
    from maverick.consequence import record_outcome as _rec
    _rec(goal_id, episode_id, float(payload.value), kind=(payload.kind or ""))

@router.get("/halt")
async def halt_status() -> dict:
    """Is the killswitch armed?

    Council round-2 capabilities-seat fix: round-1 only surfaced the
    file path. Now also returns the reason string (from the file body
    when the halt was set via POST) and the file's mtime as ``armed_at``
    so the UI can show "halted 3m ago for: <reason>".
    """
    from maverick.killswitch import _halt_file_path, is_active
    p = _halt_file_path()
    out: dict = {
        "active": is_active(),
        "file": str(p),
        "file_present": p.exists(),
        "reason": None,
        "armed_at": None,
    }
    if p.exists():
        try:
            body = p.read_text(errors="replace").strip()
            out["reason"] = body or None
        except OSError:
            pass
        try:
            out["armed_at"] = p.stat().st_mtime
        except OSError:
            pass
    return out

@router.post("/halt", status_code=204)
async def halt_set(request: Request, payload: HaltIn) -> None:
    """Arm the killswitch by touching ~/.maverick/HALT.

    Honoured by every agent at the next tool-call boundary. Use the
    DELETE endpoint or ``rm ~/.maverick/HALT`` to clear.
    """
    _require_halt_permission(request)
    from maverick.killswitch import _halt_file_path
    reason = payload.reason or "manual via dashboard"
    p = _halt_file_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(reason + "\n")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"cannot write halt file: {e}") from e
@router.delete("/halt", status_code=204)
async def halt_clear(request: Request) -> None:
    """Clear the killswitch (delete ~/.maverick/HALT)."""
    _require_halt_permission(request)
    from maverick.killswitch import _halt_file_path, clear
    p = _halt_file_path()
    if p.exists():
        try:
            p.unlink()
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"cannot remove halt file: {e}") from e
    clear()
@router.post("/goals/{goal_id}/cancel", status_code=204)
async def cancel_goal(request: Request, goal_id: int) -> None:
    """Mark a goal as cancelled.

    The agent loop checks status at each tool-call boundary; setting
    'cancelled' here causes the next check to short-circuit the run.
    Already-done goals are a no-op.
    """
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    if g.status in ("done", "cancelled", "failed"):
        return
    w.set_goal_status(goal_id, "cancelled", result="cancelled via dashboard")
    # A human killing an in-progress run is real negative ground truth (the run
    # wasn't worth finishing) -- feed it to the learning loop, best-effort.
    _ground_outcome(w, goal_id, 0.0, kind="cancel")

@router.get("/goals/{goal_id}/open_questions")
async def goal_open_questions(request: Request, goal_id: int) -> dict:
    """List unanswered questions an agent has parked for this goal."""
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    qs = w.open_questions(goal_id=goal_id)
    return {
        "open_questions": [
            {"id": q.id, "question": q.question, "asked_at": q.asked_at}
            for q in qs
        ],
    }

def _goal_gate(domain: str) -> str | None:
    """The persisted release gate for a goal's pack, or ``None``.

    A terminal playbook gate is promoted here even when an AI-generated pack
    omitted ``output.gate``. Intermediate prompt gates remain non-executable.
    """
    if not domain:
        return None
    try:
        from maverick.domain import available_domains, enforced_gate
        prof = available_domains().get(domain)
    except Exception as exc:  # pragma: no cover -- factory layer unavailable
        # A policy-read failure is not evidence that a deliverable is ungated.
        # Release callers all funnel through this helper, so fail closed.
        raise HTTPException(
            status_code=503,
            detail="deliverable release policy is temporarily unavailable",
        ) from exc
    if prof is None:
        raise HTTPException(
            status_code=409,
            detail="the goal's domain policy is no longer available",
        )
    return enforced_gate(prof)

def _goal_shape(domain: str) -> str:
    """The render shape the goal's pack declares, defaulting to 'prose'."""
    if not domain:
        return "prose"
    try:
        from maverick.domain import available_domains
        prof = available_domains().get(domain)
        return prof.output.shape if prof else "prose"
    except Exception:  # pragma: no cover -- factory layer unavailable
        return "prose"

def _authorize_goal_release_or_http(w, goal):
    """Apply the shared release contract and preserve its authenticated error."""
    from .release_policy import GoalReleaseDenied, authorize_goal_release

    try:
        return authorize_goal_release(w, goal)
    except GoalReleaseDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _deliver_release_audit_or_http(**kwargs) -> str:
    """Append an actual-release event and preserve its fail-closed status."""
    from .release_policy import GoalReleaseDenied, deliver_release_audit

    try:
        return deliver_release_audit(**kwargs)
    except GoalReleaseDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

_QUALIFIED_MATTER_ATTORNEY_ROLES = frozenset({"responsible_attorney", "attorney"})

def _require_qualified_matter_attorney(request: Request, world, goal) -> str | None:
    """Require an exact active attorney membership for a legal release action.

    Dashboard-wide RBAC answers whether a person may ever sign legal work; the
    matter roster answers whether they may act for this client. Auth-off local
    mode keeps its historical compatibility, but authenticated staff, viewers,
    and global admins receive no matter-role bypass.
    """
    principal = caller_principal(request)
    if principal is None:
        return None
    principal = require_qualified_attorney(request)
    project_id = getattr(goal, "project_id", None)
    if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0:
        raise HTTPException(status_code=404, detail="no such goal")
    try:
        role = world.project_member_role(project_id, principal)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="matter authorization is temporarily unavailable",
        ) from exc
    if role is None:
        raise HTTPException(status_code=404, detail="no such goal")
    if role not in _QUALIFIED_MATTER_ATTORNEY_ROLES:
        raise HTTPException(
            status_code=403,
            detail="an active matter attorney must authorize this release action",
        )
    return principal

@router.get("/goals/{goal_id}/signoff")
async def get_signoff(request: Request, goal_id: int) -> dict:
    """The current sign-off on a goal's deliverable, plus the gate its pack
    declares (so the UI knows whether a sign-off is even called for)."""
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    return {
        "gate": _goal_gate(g.domain),
        "deliverable_updated_at": g.updated_at,
        "signoff": w.signoff_for(goal_id),
    }

@router.post("/goals/{goal_id}/signoff")
async def post_signoff(request: Request, goal_id: int, payload: SignoffIn) -> dict:
    """Record a human's certify/reject decision on a finished deliverable -- the
    governed hand-off step (agents draft; humans certify). 400 if the pack
    declares no gate (there is nothing to sign off)."""
    # Certifying or rejecting governed work product is a legal release action,
    # distinct from ordinary operation.  Only an explicitly qualified attorney
    # (or admin, which carries the same permission) may make this decision.
    # Auth-off loopback mode remains the legacy single-user exception in the
    # shared permission helper.
    require_permission(request, "legal_signoff")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    qualified_principal = _require_qualified_matter_attorney(request, w, g)
    if _goal_gate(g.domain) is None:
        raise HTTPException(status_code=400, detail="this deliverable has no sign-off gate")
    if g.status != "done":
        raise HTTPException(
            status_code=409,
            detail="the deliverable must be finished before it can be signed off",
        )
    who = qualified_principal or _supervisor(request)
    try:
        changed = w.record_signoff(
            goal_id,
            payload.decision,
            decided_by=who,
            note=payload.note,
            expected_updated_at=payload.expected_updated_at,
        )
    except ValueError as exc:
        # Optimistic version binding: a concurrent edit/rerun means the human
        # reviewed stale bytes. Reload and require a fresh decision.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    current_goal = w.get_goal(goal_id)
    current_signoff = w.signoff_for(goal_id)
    if current_goal is None or current_signoff is None:
        raise HTTPException(
            status_code=503,
            detail="legal sign-off could not be reconciled",
        )
    from .release_policy import GoalReleaseDenied, deliver_current_signoff_audit

    try:
        deliver_current_signoff_audit(w, current_goal, current_signoff)
    except GoalReleaseDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    # Feed the human's certify/reject verdict back into the learning loop as a
    # GROUNDED outcome: a person judging the actual deliverable is the cleanest
    # ground-truth signal in the system, and it lands as exactly the
    # (goal_id, episode_id, value) tuple the Consequence Engine / data-engine
    # already consume. Both verdicts are signal -- an approval is a 1.0 reward,
    # a rejection a 0.0. Best-effort and gated: no-op unless [consequence] is on.
    if changed:
        _record_signoff_outcome(w, goal_id, payload.decision)
    return {
        "gate": _goal_gate(g.domain),
        "deliverable_updated_at": g.updated_at,
        "changed": changed,
        "signoff": w.signoff_for(goal_id),
    }

def _ground_outcome(w, goal_id: int, value: float, *, kind: str) -> None:
    """Record a real human/world outcome for the goal's latest episode as
    grounded reward the learning flywheel prefers over its proxy. Delegates to
    the shared core helper (no-op unless ``[consequence]`` is enabled; never
    raises -- grounding is best-effort and must not fail the request)."""
    from maverick import consequence
    consequence.record_self_outcome(w, goal_id, value, kind=kind)

def _record_signoff_outcome(w, goal_id: int, decision: str) -> None:
    """A deliverable sign-off -> grounded outcome (approved 1.0 / rejected 0.0)."""
    _ground_outcome(w, goal_id, 1.0 if decision == "approved" else 0.0, kind="signoff")

@router.get("/goals/{goal_id}/feedback")
async def get_feedback(request: Request, goal_id: int) -> dict:
    """The current human thumbs-up/down on a goal's result (or ``null``)."""
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    principal = caller_principal(request)
    if principal is None:
        raise HTTPException(status_code=403, detail="named matter membership required")
    try:
        feedback = w.matter_feedback_for_goal(goal_id, principal=principal)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="feedback store is temporarily unavailable"
        ) from exc
    return {"feedback": feedback}

@router.post("/goals/{goal_id}/feedback")
async def post_feedback(request: Request, goal_id: int, payload: FeedbackIn) -> dict:
    """Record a human thumbs-up/down on a goal's result.

    The always-available verdict: unlike the sign-off gate (which only exists when
    a pack declares one), any finished result can be rated here, and the rating is
    fed back into the learning loop as a GROUNDED outcome -- an up is a 1.0 reward,
    a down a 0.0 -- exactly the ``(goal_id, episode_id, value)`` tuple the
    local consequence loop consumes. Persistence is fail-closed; optional
    grounding remains gated on ``[consequence]``."""
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    value = 1.0 if payload.rating == "up" else 0.0
    who = caller_principal(request)
    if who is None:
        raise HTTPException(status_code=403, detail="named matter membership required")
    try:
        row = w.record_matter_feedback(
            goal_id,
            principal=who,
            rating=payload.rating,
            value=value,
            note=payload.note or "",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="feedback could not be persisted"
        ) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="no such goal")
    _ground_outcome(w, goal_id, value, kind="feedback")
    return {"feedback": row}

@router.post("/goals/{goal_id}/deliverable/edit")
async def edit_deliverable(request: Request, goal_id: int,
                           payload: DeliverableEditIn) -> dict:
    """Save a human's revised deliverable and ground the edit as a learning signal.

    How much the human changed the agent's draft is graded ground truth: a
    verbatim keep is a strong positive (~1.0), a heavy rewrite a graded negative.
    The similarity becomes the grounded outcome (kind ``edit``), and the revised
    text replaces the goal's result (the human's version is now canonical).
    Best-effort grounding, gated on ``[consequence]``."""
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    original = g.result or ""
    revised = payload.text or ""
    w.set_goal_status(goal_id, g.status, result=revised)   # the edit is the deliverable now
    if not original:
        # Nothing to grade against: the agent produced no draft to keep or
        # rewrite. Grounding this as 0.0 would punish it for text it never wrote,
        # so we save the human's text but ground nothing.
        return {"similarity": None}
    similarity = _deliverable_edit_similarity(original, revised)
    _ground_outcome(w, goal_id, similarity, kind="edit")
    return {"similarity": round(similarity, 3)}

@router.get("/goals/{goal_id}/deliverable.csv")
async def export_deliverable_csv(request: Request, goal_id: int) -> Response:
    """Export a goal's deliverable as CSV -- the mechanical hand-off so an
    approved forecast/table can be loaded into a downstream system instead of
    re-keyed. 404 when the result carries no tabular deliverable."""
    import csv
    import io as _io

    from maverick.deliverable import render_deliverable
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    # Download is an explicit release action, not a read-only workspace view.
    # Require both a currently qualified caller and the durable matter/domain/
    # terminal-gate/current-approval contract.
    require_permission(request, "legal_signoff")
    actor = _require_qualified_matter_attorney(request, w, g)
    if actor is None:
        if not auth_genuinely_off():
            raise HTTPException(status_code=403, detail="named release actor required")
        actor = "local"
    release = _authorize_goal_release_or_http(w, g)
    rendered = render_deliverable(_goal_shape(release.domain), release.deliverable)
    if rendered.table is None:
        raise HTTPException(status_code=404, detail="no tabular deliverable to export")
    buf = _io.StringIO()
    writer = csv.writer(buf)
    # Neutralize spreadsheet formulas so an opened CSV can't execute injected
    # formula cells (=, +, -, @) in Excel/Sheets.
    writer.writerow([_csv_formula_safe(cell) for cell in rendered.table.headers])
    writer.writerows(
        [_csv_formula_safe(cell) for cell in row] for row in rendered.table.rows
    )
    _deliver_release_audit_or_http(
        actor=actor,
        release=release,
        goal_id=goal_id,
        action="csv_export",
        destination_class="authenticated_download",
    )
    # The audit records intent before bytes leave.  Re-prove the exact approved
    # version after that append so a concurrent edit never receives the stale
    # in-memory response (the intent remains a truthful attempted release).
    fresh = w.get_goal(goal_id)
    if fresh is None or fresh.updated_at != release.deliverable_updated_at:
        raise HTTPException(
            status_code=409, detail="the deliverable changed before release"
        )
    fresh_release = _authorize_goal_release_or_http(w, fresh)
    if fresh_release.deliverable_sha256 != release.deliverable_sha256:
        raise HTTPException(
            status_code=409, detail="the deliverable changed before release"
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="deliverable-{goal_id}.csv"'},
    )

@router.get("/goals/{goal_id}/artifacts")
async def list_goal_artifacts(request: Request, goal_id: int) -> dict:
    """The goal's latest artifacts -- the newest version of each titled output
    (markdown / code / table / text) it produced, with a per-title version
    count. Goal-access gated; content is decrypted for the owner."""
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    return {"artifacts": w.latest_artifacts(goal_id)}

@router.get("/goals/{goal_id}/artifacts/history")
async def goal_artifact_history(request: Request, goal_id: int, title: str) -> dict:
    """Every version of one titled artifact, oldest -> newest, each with a unified
    diff against the previous version. Powers the version/diff viewer."""
    import difflib
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    versions = [a for a in w.artifacts_for_goal(goal_id) if a["title"] == title]
    out: list[dict] = []
    prev: str | None = None
    for a in versions:  # ascending by version
        diff = ""
        if prev is not None:
            diff = "\n".join(difflib.unified_diff(
                prev.splitlines(), (a["content"] or "").splitlines(),
                fromfile=f"v{a['version'] - 1}", tofile=f"v{a['version']}", lineterm=""))
        out.append({"version": a["version"], "created_at": a["created_at"],
                    "content": a["content"], "diff": diff})
        prev = a["content"] or ""
    return {"title": title, "versions": out}

_SHARE_TTL_SECONDS = 7 * 24 * 3600

@router.post("/goals/{goal_id}/share", status_code=201)
async def create_goal_share(request: Request, goal_id: int) -> dict:
    """Mint a read-only share link to a goal's deliverable (default 7-day
    expiry). Qualified legal-signoff permission + goal access + the shared
    release contract are required. The clear token is returned ONCE -- only its
    hash is stored, so it can't be re-fetched later, only revoked."""
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    # Minting a public bearer URL is an explicit legal release, not an ordinary
    # operator action. The release contract also refuses matterless, unknown,
    # domainless, ungated, unfinished, rejected, or stale deliverables.
    require_permission(request, "legal_signoff")
    actor = _require_qualified_matter_attorney(request, w, g)
    if actor is None:
        if not auth_genuinely_off():
            raise HTTPException(status_code=403, detail="named release actor required")
        actor = "local"
    release = _authorize_goal_release_or_http(w, g)
    try:
        from .public_origin import canonical_url

        public_base = canonical_url("").rstrip("/")
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503, detail="firm public URL policy is unavailable"
        ) from exc
    try:
        link_id, token, event = w.create_bound_share_link(
            goal_id,
            project_id=release.project_id,
            actor=actor,
            deliverable_updated_at=release.deliverable_updated_at,
            deliverable_sha256=release.deliverable_sha256,
            ttl_seconds=_SHARE_TTL_SECONDS,
            allow_local_auth_off=auth_genuinely_off(),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="release authority changed before share creation"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="share link could not be created"
        ) from exc
    _deliver_release_audit_or_http(
        actor=actor,
        release=release,
        goal_id=goal_id,
        action=event["action"],
        destination_class=event["destination_class"],
        event_id=event["event_id"],
        share_link_id=event["share_link_id"],
        expires_at=event["expires_at"],
        world=w,
    )
    if w.resolve_share_link(token) != goal_id:
        raise HTTPException(
            status_code=409, detail="the deliverable changed before release"
        )
    return {"id": link_id, "url": public_base + "/share/" + token}

@router.post("/goals/{goal_id}/share/{link_id}/revoke")
async def revoke_goal_share(request: Request, goal_id: int, link_id: int) -> dict:
    """Revoke a share link. Operator role + goal access; the revoke is scoped to
    this goal so a caller can't revoke another goal's link by id."""
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    require_permission(request, "operate")
    return {"ok": w.revoke_share_link(link_id, goal_id=goal_id)}

@router.get("/audit/tail")
async def audit_tail(request: Request, n: int = 100, day: str | None = None) -> dict:
    """Tail the audit log (NDJSON at ~/.maverick/audit/YYYY-MM-DD.ndjson).

    Audit-gated: the audit trail is the who-did-what-when record (it can name
    principals, tool inputs, costs), so reading it requires the "audit"
    permission -- held by the admin role and the dedicated read-only "auditor"
    role (separation of duties), never by operator/viewer. It is not an
    unauthenticated/operator surface."""
    require_permission(request, "audit")
    from maverick.audit import default_audit_log

    from maverick_dashboard.app import safe_audit_day
    n = max(1, min(int(n or 100), 1000))
    return {"events": default_audit_log().tail(n, day=safe_audit_day(day))}

@router.get("/audit/grep")
async def audit_grep(request: Request, pattern: str, day: str | None = None) -> dict:
    """Search recent audit events for the given literal pattern.

    Audit-gated (see :func:`audit_tail`). Intentionally uses bounded, literal
    (case-insensitive) matching rather than a user-supplied regex: a regex over
    the HTTP surface invites catastrophic-backtracking ReDoS that blocks the
    dashboard event loop. Bounds the scan to the most recent 1000 events and
    caps results at 200.
    """
    require_permission(request, "audit")
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required")
    if len(pattern) > 200:
        raise HTTPException(status_code=400, detail="pattern too long")
    from maverick.audit import default_audit_log

    from maverick_dashboard.app import safe_audit_day
    events = default_audit_log().tail(1000, day=safe_audit_day(day))
    needle = pattern.lower()
    matches = [
        e for e in events
        if needle in json.dumps(e, ensure_ascii=False).lower()
    ]
    return {"events": matches[:200]}

def _supervisor(request: Request) -> str:
    """The acting supervisor's identity for claims/attribution.

    The authenticated principal when auth is on; the shared "operator"
    identity in single-user/no-auth deployments (claims still prevent
    double-handling across that operator's browser tabs)."""
    from .auth import caller_principal
    return caller_principal(request) or "operator"

@router.post("/goals/{goal_id}/retitle", status_code=204)
async def retitle_goal(request: Request, goal_id: int, payload: RetitleIn) -> None:
    """Rename a matter goal through the WorldModel mutation boundary."""
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    require_permission(request, "operate")
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    w.set_goal_title(goal_id, title[:200])

@router.post("/goals/{goal_id}/children", response_model=GoalOut, status_code=201)
async def create_child_goal(request: Request, goal_id: int, payload: ChildIn) -> GoalOut:
    """Create a child work item under ``goal_id``.

    Structural only: the child is created ``pending`` and is NOT queued to
    run; start it later via chat or POST /api/v1/goals. It inherits the
    parent's matter/domain; authenticated provenance names the exact member
    who created the child, while the matter ACL keeps the subtree shared.
    """
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request)
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    principal = caller_principal(request)
    if principal is not None:
        domain, project_id = _validated_goal_filing(
            request,
            w,
            principal,
            domain=g.domain,
            project_id=g.project_id,
        )
        child_id = w.create_matter_goal(
            title[:200],
            (payload.description or "")[:8000],
            parent_id=goal_id,
            principal=principal,
            domain=domain or "",
            project_id=project_id,
        )
        if child_id is None:
            raise HTTPException(status_code=404, detail="no such goal")
    else:
        # Preserve the parent's filing context even in local compatibility
        # mode; a structural child must not silently fall out of its matter.
        child_id = w.create_goal(
            title[:200],
            (payload.description or "")[:8000],
            parent_id=goal_id,
            owner=g.owner,
            domain=g.domain,
            project_id=g.project_id,
        )
    c = w.get_goal(child_id)
    if c is None:
        raise HTTPException(status_code=500, detail="goal vanished after create")
    return _to_goal_out(c)

_COMPOSE_PRIORITIES = ("low", "normal", "high")

@router.post("/goals/compose", response_model=GoalOut, status_code=201)
async def compose_goal(request: Request, payload: ComposeIn, bg: BackgroundTasks) -> GoalOut:
    """Goal-builder submit: blocks -> structured brief -> create + run.

    The goals table has no metadata columns (see ``WorldModel.create_goal``),
    so the budget/channel/priority blocks are folded into the description the
    agent reads; the budget block additionally becomes the run's real
    ``max_dollars`` cap. Steps become a markdown checklist.
    """
    require_permission(request, "operate")
    require_provider_or_400()
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request)
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    steps = [s.strip()[:500] for s in payload.steps if s and s.strip()]
    if len(steps) > 50:
        raise HTTPException(status_code=400, detail="too many steps (max 50)")
    priority = (payload.priority or "").strip().lower() or None
    if priority is not None and priority not in _COMPOSE_PRIORITIES:
        raise HTTPException(
            status_code=400,
            detail=f"priority must be one of: {', '.join(_COMPOSE_PRIORITIES)}",
        )
    channel = (payload.channel or "").strip() or None

    parts: list[str] = []
    if steps:
        parts.append("## Steps\n" + "\n".join(f"- [ ] {s}" for s in steps))
    meta: list[str] = []
    if payload.budget_dollars is not None:
        meta.append(
            f"Budget cap: ${payload.budget_dollars:.2f} "
            "(also enforced as the run's max_dollars)"
        )
    if channel:
        meta.append(f"Announce progress on: {channel}")
    if priority:
        meta.append(f"Priority: {priority}")
    if meta:
        parts.append("\n".join(meta))
    description = "\n\n".join(parts) if parts else title

    w = _world()
    principal = caller_principal(request)
    domain, project_id = _validated_goal_filing(
        request,
        w,
        principal,
        domain=payload.domain,
        project_id=payload.project_id,
    )
    if principal is not None:
        goal_id = w.create_matter_goal(
            title[:200],
            description[:8000],
            principal=principal,
            domain=domain or "",
            project_id=project_id,
        )
        if goal_id is None:
            raise HTTPException(status_code=404, detail="no such project")
    else:
        goal_id = w.create_goal(
            title[:200],
            description[:8000],
            owner="",
            domain=domain or "",
            project_id=project_id,
        )
    from maverick.runner import run_goal_in_background_async
    max_dollars = (
        min(payload.budget_dollars, DEFAULT_MAX_DOLLARS)
        if payload.budget_dollars is not None else DEFAULT_MAX_DOLLARS
    )
    allowed_suites = caller_suites(request)
    user_id = execution_user_id_from_request(request)
    if user_id:
        bg.add_task(
            run_goal_in_background_async, goal_id, max_dollars,
            channel="api", user_id=user_id,
            concurrency_principal=principal,
            allowed_suites=allowed_suites,
        )
    else:
        bg.add_task(
            run_goal_in_background_async,
            goal_id,
            max_dollars,
            concurrency_principal=principal,
            allowed_suites=allowed_suites,
        )
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=500, detail="goal vanished after create")
    return _to_goal_out(g)

@router.post("/goals/{goal_id}/resume", status_code=204)
async def resume_goal(goal_id: int, request: Request, bg: BackgroundTasks) -> None:
    """Resume a blocked / cancelled goal.

    Capabilities-seat finding: the old CLI had ``maverick resume`` but the
    dashboard's only way to flip a cancelled goal back was to start a
    brand-new one. This route (now the only resume surface) flips status
    back to 'pending' and
    re-queues the runner, so the next goal-event poll picks it back up.
    """
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    principal = caller_principal(request)
    if principal is not None:
        # Legacy unfiled rows may remain readable to an administrator for
        # cleanup, but verified users must never dispatch them. Revalidate the
        # exact current matter membership and gated legal profile immediately
        # before changing status and queuing work.
        _validated_goal_filing(
            request,
            w,
            principal,
            domain=g.domain,
            project_id=g.project_id,
        )
    # Block resuming things that have no parked work.
    if g.status not in ("blocked", "cancelled", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"goal is {g.status!r}; only blocked/cancelled/failed goals can resume",
        )
    w.set_goal_status(goal_id, "pending", result=None)
    from maverick.runner import run_goal_in_background_async
    allowed_suites = caller_suites(request)
    user_id = execution_user_id_from_request(request)
    if user_id:
        bg.add_task(
            run_goal_in_background_async, goal_id, channel="api", user_id=user_id,
            concurrency_principal=principal,
            allowed_suites=allowed_suites,
        )
    else:
        bg.add_task(
            run_goal_in_background_async,
            goal_id,
            concurrency_principal=principal,
            allowed_suites=allowed_suites,
        )
