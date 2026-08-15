"""REST API for Lightwork (mounted at /api/v1).

v0.1.6: BackgroundTask runner moved to maverick.runner.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import difflib
import hashlib
import hmac
import json
import logging
import os
import threading
import time

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
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
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)

from ._shared import (
    _get_sse_semaphore,
    _world,
    require_provider_or_400,
)
from ._shared import _world_cache as _world_cache  # re-export: tests clear api._world_cache
from .api_schemas import (
    AgentOverrideIn,
    AiSystemIn,
    AnswerIn,
    AssessmentAssignIn,
    AssessmentDecideIn,
    AssessmentEvidenceIn,
    AssessmentRefreshIn,
    AssessmentReviewIn,
    AttachmentOut,
    BulkAssessmentImportIn,
    CachePurgeIn,
    CatalogInstallIn,
    ChildIn,
    ClausePlaybookIn,
    ComposeIn,
    ConnectionIn,
    CopilotIn,
    DeliverableEditIn,
    DgmToggleIn,
    DocDiscoverIn,
    DpaFromDocumentIn,
    DpaReviewIn,
    DsarFromMessageIn,
    DsarIn,
    EventTriggerIn,
    EventTriggerOut,
    ExternalAgentIn,
    ExternalCredentialIn,
    FactIn,
    FeatureSwitchIn,
    FeedbackIn,
    FleetAgentAddIn,
    FleetCreateIn,
    FleetRunIn,
    FlowApplyIn,
    FlowAutonomyIn,
    FlowChatIn,
    FlowDraftIn,
    FlowDryRunIn,
    FlowPublishIn,
    FlowResumeIn,
    FlowRollbackIn,
    FlowRunIn,
    FlowSaveIn,
    FollowupsIn,
    GoalEventOut,
    GoalEventsResponse,
    GoalIn,
    GoalOut,
    HaltIn,
    ImportRunIn,
    ImportRunOut,
    IncidentIn,
    IncidentNotifyIn,
    JDDraftIn,
    JDMatchIn,
    LearningToggleIn,
    LicenseIn,
    LicenseNoteIn,
    ModelCostTierIn,
    OAuthAuthorizeIn,
    OAuthExchangeIn,
    OneTrustImportIn,
    OutcomeByKeyIn,
    OutcomeIn,
    OutcomeLinkIn,
    PartnerTenantIn,
    RedactIn,
    ReparentIn,
    RetitleIn,
    ReviewTriggerIn,
    RiskAcceptIn,
    RoleOverrideIn,
    RopaIn,
    ScheduleIn,
    ScheduleOut,
    SignoffIn,
    SkillCreateIn,
    SkillInstallIn,
    SkillOut,
    SourceAttachIn,
    SpeakIn,
    TemplateIn,
    TenantCreateIn,
    TenantOut,
    TenantPlanIn,
    TenantQuotaIn,
    TenantRoleIn,
    TriggerIn,
    TriggerOut,
    TrustAgentIn,
    TrustRevokeIn,
    UserSuitesIn,
    ValueAssumptionsIn,
    WorkflowDraftIn,
    WorkflowRefineIn,
    WorkflowSaveIn,
)
from .auth import (
    assert_goal_access,
    auth_genuinely_off,
    caller_principal,
    caller_suites,
    can_access_goal,
    durable_automation_owner,
    execution_user_id_from_request,
    goal_owner_filter,
    has_permission,
    is_dashboard_admin,
    require_global_permission,
    require_permission,
    require_suite,
    stored_automation_identity,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["v1"])

# Keep exact edit grading for normal-sized deliverables, but never feed the
# full 200k-character request limit into difflib.SequenceMatcher. Certain
# repetitive inputs make SequenceMatcher consume quadratic-like CPU, and this
# module's endpoints run on the dashboard API path.
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

# Shared-store channel for Idempotency-Key dedup on goal creation (see
# create_goal). Distinct from the channel/webhook idempotency namespaces.
_IDEMPOTENCY_CHANNEL = "idempotency:api:goals"


def _shared_halt_backend() -> bool:
    """Whether the cluster-wide (shared-store) halt is in play. Only on a shared
    backend (Postgres); on single-host SQLite the local HALT file is the whole
    mechanism and the killswitch never consults the shared row, so the dashboard
    leaves it untouched (keeps single-host behavior unchanged)."""
    try:
        from maverick.world_model_backends import is_postgres_configured
        return bool(is_postgres_configured())
    except Exception:
        return False


def _require_halt_permission(request: Request) -> None:
    """Gate dashboard halt toggles at the correct blast radius.

    In local/SQLite mode the halt is process-local and remains an operator action.
    In shared Postgres mode the halt row is fleet-wide and untenanted, so toggling
    it is a global control-plane action reserved for dashboard admins.
    """
    require_permission(request, "admin" if _shared_halt_backend() else "operate")


_PERF_SLA_CACHE_TTL_SECONDS = 60.0
_PERF_SLA_LOCK = asyncio.Lock()
_PERF_SLA_CACHE: tuple[float, list[dict], str | None] | None = None
_PERF_HISTORY_MAX_FILES = 128
_COMPLIANCE_PACKET_CACHE_TTL_SECONDS = 60.0
_COMPLIANCE_PACKET_LOCK = asyncio.Lock()
_COMPLIANCE_PACKET_CACHE: tuple[float, str] | None = None


















def _to_goal_out(g) -> GoalOut:
    return GoalOut(
        id=g.id, status=g.status, title=g.title,
        description=g.description, result=g.result,
    )


# --- Tenant provisioning (admin only) ----------------------------------------
# A control-plane surface so operators can spin tenants up/down without shelling
# into the box for `maverick tenant ...`. All endpoints require the "admin"
# permission; auth-off (single-operator) deployments treat the local caller as
# admin, matching the rest of the API.

def _to_tenant_out(rec) -> TenantOut:
    from maverick.workspace import Workspace
    # API paths use a stable slash representation on every server OS; clients
    # should not need to understand Windows path separators.
    config_path = (Workspace(rec.id).root / "config.toml").as_posix()
    return TenantOut(
        id=rec.id, status=rec.status, plan=rec.plan,
        display_name=rec.display_name, max_daily_dollars=rec.max_daily_dollars,
        created_at=rec.created_at, updated_at=rec.updated_at,
        config_path=config_path,
    )


def _get_tenant_or_404(tenant_id: str):
    from maverick.tenant import registry as tenant_registry
    rec = tenant_registry.get_tenant(tenant_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="no such tenant")
    return rec


@router.get("/admin/tenants", response_model=list[TenantOut])
async def list_tenants(request: Request) -> list[TenantOut]:
    require_global_permission(request, "admin")
    from maverick.tenant import registry as tenant_registry
    return [_to_tenant_out(r) for r in tenant_registry.list_tenants()]


@router.get("/workforce/posture")
async def workforce_posture(request: Request) -> dict:
    """The workforce autonomy dial: fleet posture + graduation candidates.

    Read-only surface for the dashboard -- whether per-agent autonomy levels are
    on, the distribution of baseline authority rungs across the roster, how many
    hires are still onboarding, and which have earned graduation from a clean
    approval record (advisory; the client lifts onboarding to act on it).
    """
    require_permission(request, "view")
    from maverick.agent_autonomy import graduation_candidates, levels_enabled
    from maverick.domain_audit import audit_roster, summarize
    s = summarize(audit_roster())
    out: dict = {
        "levels_enabled": levels_enabled(),
        "autonomy_posture": s.get("autonomy_posture", {}),
        "packs_onboarding": s.get("packs_onboarding", 0),
        "graduation_candidates": [],
    }
    try:
        from maverick.domain import available_domains
        wm = _world()
        cands = graduation_candidates(wm.list_approvals(limit=5000), sorted(available_domains()))
        out["graduation_candidates"] = [
            {"name": c.name, "sample": c.sample, "approve_rate": c.approve_rate,
             "confidence": c.confidence, "reason": c.reason}
            for c in cands
        ]
    except Exception:  # pragma: no cover -- no DB / empty install -> empty list
        pass
    return out


# ---- executive boards (Overview / Spend / Workforce) ------------------------
# One JSON payload per board, windowed by ?days and owner-scoped exactly like
# the pages that consume them. Timestamp columns (created/updated/started/
# ended, cost, tokens) are plaintext and safe to aggregate in SQL; episode
# ``outcome`` is encrypted at rest, so the outcome mix is counted in Python
# over the decrypted recent slice.

_BOARD_DAYS = (30, 90, 180, 365)


def _board_days(days: int | None) -> int:
    return days if days in _BOARD_DAYS else 90


def _board_day_list(days: int):
    from datetime import date, timedelta
    today = date.today()
    day_list = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    since = time.mktime(day_list[0].timetuple())
    return day_list, since, since - days * 86400


def _owner_sql(owner, column: str = "owner"):
    """(extra WHERE clause, params) for goal-owner scoping."""
    return ("", ()) if owner is None else (f" AND {column} = ?", (owner,))


def _board_overview(w, owner, days: int) -> dict:
    day_list, since, prev_since = _board_day_list(days)
    own, params = _owner_sql(owner)
    by_status = {r[0]: int(r[1]) for r in w.conn.execute(
        "SELECT status, COUNT(*) FROM goals WHERE 1=1" + own + " GROUP BY status",
        params).fetchall()}
    daily = {d.isoformat(): {"d": d.isoformat(), "started": 0, "delivered": 0}
             for d in day_list}
    for col, key, extra in (("created_at", "started", ""),
                            ("updated_at", "delivered", " AND status = 'done'")):
        rows = w.conn.execute(
            f"SELECT date({col}, 'unixepoch') AS day, COUNT(*) FROM goals "
            f"WHERE {col} >= ?" + extra + own + " GROUP BY day",
            (since, *params)).fetchall()
        for day, n in rows:
            if day in daily:
                daily[day][key] += int(n)

    def _count(col: str, lo: float, hi: float, extra: str = "") -> int:
        return int(w.conn.execute(
            f"SELECT COUNT(*) FROM goals WHERE {col} >= ? AND {col} < ?"
            + extra + own, (lo, hi, *params)).fetchone()[0])

    now = time.time()
    window = {
        "started": _count("created_at", since, now),
        "delivered": _count("updated_at", since, now, " AND status = 'done'"),
        "started_prev": _count("created_at", prev_since, since),
        "delivered_prev": _count("updated_at", prev_since, since,
                                 " AND status = 'done'"),
    }
    from maverick.departments import department_title
    from maverick.domain import suite_for
    domains: dict[str, dict] = {}
    for dom, total, done in w.conn.execute(
            "SELECT domain, COUNT(*), SUM(status = 'done') FROM goals "
            "WHERE 1=1" + own + " GROUP BY domain", params).fetchall():
        suite = suite_for(dom or "") if dom else None
        label = department_title(suite) if suite else "General"
        row = domains.setdefault(label, {"label": label, "total": 0, "done": 0})
        row["total"] += int(total)
        row["done"] += int(done or 0)
    recent = [{"id": g.id, "title": g.title, "status": g.status,
               "updated_at": g.updated_at}
              for g in w.list_goals(owner=owner, limit=12, order="desc")]
    return {
        "totals": {
            "total": sum(by_status.values()),
            "active": by_status.get("active", 0),
            "done": by_status.get("done", 0),
            "blocked": by_status.get("blocked", 0),
        },
        "window": window,
        "daily": list(daily.values()),
        "domains": sorted(domains.values(),
                          key=lambda r: r["total"], reverse=True)[:8],
        "recent": recent,
    }


def _board_spend(w, owner, days: int) -> dict:
    day_list, since, prev_since = _board_day_list(days)
    join = ("FROM episodes e JOIN goals g ON e.goal_id = g.id"
            if owner is not None else "FROM episodes e")
    own, params = ("", ()) if owner is None else (" AND g.owner = ?", (owner,))
    ended = " WHERE e.ended_at IS NOT NULL"

    daily = {d.isoformat(): {"d": d.isoformat(), "cost": 0.0, "runs": 0}
             for d in day_list}
    for day, cost, runs in w.conn.execute(
            "SELECT date(e.started_at, 'unixepoch') AS day, "
            "SUM(COALESCE(e.cost_dollars, 0)), COUNT(*) " + join + ended +
            " AND e.started_at >= ?" + own + " GROUP BY day",
            (since, *params)).fetchall():
        if day in daily:
            daily[day] = {"d": day, "cost": float(cost or 0), "runs": int(runs)}

    def _tot(lo: float) -> dict:
        r = w.conn.execute(
            "SELECT COALESCE(SUM(e.cost_dollars), 0), COUNT(*), "
            "COALESCE(SUM(e.input_tokens), 0), COALESCE(SUM(e.output_tokens), 0), "
            "COALESCE(SUM(e.tool_calls), 0) " + join + ended +
            " AND e.started_at >= ? AND e.started_at < ?" + own,
            (lo, lo + days * 86400 + 1, *params)).fetchone()
        return {"dollars": float(r[0]), "runs": int(r[1]),
                "input_tokens": int(r[2]), "output_tokens": int(r[3]),
                "tool_calls": int(r[4])}

    totals, prev = _tot(since), _tot(prev_since)
    episodes = w.list_episodes(limit=200, owner=owner)
    outcomes = {"success": 0, "failed": 0, "running": 0}
    for e in episodes:
        if e.started_at < since:
            continue
        if not e.outcome:
            outcomes["running"] += 1
        elif e.outcome == "success":
            outcomes["success"] += 1
        else:
            outcomes["failed"] += 1
    per_run = [{"id": e.id, "goal_id": e.goal_id,
                "cost": float(e.cost_dollars or 0)}
               for e in episodes[:60]]
    top_goals = []
    for gid, cost, runs in w.conn.execute(
            "SELECT e.goal_id, SUM(COALESCE(e.cost_dollars, 0)) AS c, COUNT(*) "
            + join + ended + " AND e.started_at >= ?" + own +
            " GROUP BY e.goal_id ORDER BY c DESC LIMIT 8",
            (since, *params)).fetchall():
        g = w.get_goal(int(gid)) if gid is not None else None
        top_goals.append({"goal_id": gid, "title": g.title if g else f"#{gid}",
                          "cost": float(cost or 0), "runs": int(runs)})
    return {"totals": totals, "prev": prev, "daily": list(daily.values()),
            "outcomes": outcomes, "per_run": per_run, "top_goals": top_goals,
            "by_platform": _external_platform_spend(w, owner, since)}


def _external_platform_spend(w, owner, since: float) -> list[dict]:
    """Spend by external-agent platform (bring-your-own-agent runs).

    Forces the episodes⋈goals join unconditionally — the admin/auth-off view
    passes ``owner is None``, and that is precisely the view where
    ``agent:*`` rows exist. Individual humans keep owner scoping and simply
    see an empty segment."""
    if owner is not None:
        return []
    from maverick.external_agents import PLATFORMS, roster
    platform_of = {f"agent:{r['id']}": r["platform"] for r in roster()}
    if not platform_of:
        return []
    agg: dict[str, dict] = {}
    for own_id, cost, runs in w.conn.execute(
            "SELECT g.owner, SUM(COALESCE(e.cost_dollars, 0)), COUNT(*) "
            "FROM episodes e JOIN goals g ON e.goal_id = g.id "
            "WHERE e.ended_at IS NOT NULL AND e.started_at >= ? "
            "AND g.owner LIKE 'agent:%' GROUP BY g.owner",
            (since,)).fetchall():
        platform = platform_of.get(str(own_id))
        if platform is None:
            continue
        row = agg.setdefault(platform, {
            "label": PLATFORMS.get(platform, platform),
            "cost": 0.0, "runs": 0})
        row["cost"] += float(cost or 0)
        row["runs"] += int(runs)
    return sorted(agg.values(), key=lambda r: r["cost"], reverse=True)[:8]


def _board_workforce(w, owner, days: int) -> dict:
    from maverick.departments import list_departments
    from maverick.marketplace.storefront import connector_marketplace
    from maverick.operating_record import assemble
    from maverick.outcomes import firm_totals, worker_cards
    _, since, prev_since = _board_day_list(days)
    own, params = _owner_sql(owner)
    firm = firm_totals(assemble(w, owner=owner)).to_dict()
    cards = worker_cards(w, owner=owner)
    depts: dict[str, dict] = {}
    for c in cards:
        row = depts.setdefault(c.suite_title or "General",
                               {"label": c.suite_title or "General",
                                "completed": 0, "total": 0, "spend": 0.0})
        row["completed"] += c.goals_completed
        row["total"] += c.goals_total
        row["spend"] += c.spend_dollars

    def _delivered(lo: float, hi: float) -> int:
        return int(w.conn.execute(
            "SELECT COUNT(*) FROM goals WHERE status = 'done' "
            "AND updated_at >= ? AND updated_at < ?" + own,
            (lo, hi, *params)).fetchone()[0])

    return {
        "kpis": {
            "departments": len(depts),
            "specialists": sum(d.headcount for d in list_departments()),
            "connectors": connector_marketplace()["total"],
            "goals_completed": firm["goals_completed"],
            "goals_total": firm["goals_total"],
            "spend_dollars": firm["spend_dollars"],
            "delivered_window": _delivered(since, time.time()),
            "delivered_prev": _delivered(prev_since, since),
        },
        "depts": sorted(depts.values(),
                        key=lambda r: (r["completed"], r["total"]),
                        reverse=True)[:8],
        "leaders": [c.to_dict() for c in cards[:6]],
        "platforms": _external_platform_workforce(w, owner),
    }


def _external_platform_workforce(w, owner) -> list[dict]:
    """Goals by external-agent platform for the workforce board. Same
    admin-view-only rule as the spend segment: individual humans keep owner
    scoping and see an empty segment."""
    if owner is not None:
        return []
    from maverick.external_agents import PLATFORMS, roster
    platform_of = {f"agent:{r['id']}": r["platform"] for r in roster()}
    if not platform_of:
        return []
    agg: dict[str, dict] = {}
    for own_id, completed, total, spend in w.conn.execute(
            "SELECT g.owner, "
            "SUM(CASE WHEN g.status = 'done' THEN 1 ELSE 0 END), COUNT(*), "
            "(SELECT COALESCE(SUM(e.cost_dollars), 0) FROM episodes e "
            " JOIN goals g2 ON e.goal_id = g2.id WHERE g2.owner = g.owner) "
            "FROM goals g WHERE g.owner LIKE 'agent:%' GROUP BY g.owner"
            ).fetchall():
        platform = platform_of.get(str(own_id))
        if platform is None:
            continue
        row = agg.setdefault(platform, {
            "label": PLATFORMS.get(platform, platform),
            "completed": 0, "total": 0, "spend": 0.0})
        row["completed"] += int(completed or 0)
        row["total"] += int(total or 0)
        row["spend"] += float(spend or 0)
    return sorted(agg.values(), key=lambda r: (r["completed"], r["total"]),
                  reverse=True)[:8]


def _board_workspace(department: str) -> dict:
    """Compact department board (privacy / finance / security): the same
    assessment records the workspace pages list, rolled into KPIs, a residual-
    risk mix, a 12-month opened-vs-decided series, and per-framework volume.
    The deep executive view stays /privacy/board; this is the at-a-glance."""
    from datetime import datetime, timezone

    from maverick.assessment import custom_template_records, list_saved

    from .app import (
        FINANCE_ASSESSMENT_TYPES,
        PRIVACY_ASSESSMENT_TYPES,
        SECURITY_ASSESSMENT_TYPES,
    )
    base = {"privacy": PRIVACY_ASSESSMENT_TYPES,
            "finance": FINANCE_ASSESSMENT_TYPES,
            "security": SECURITY_ASSESSMENT_TYPES}[department]
    types = set(base) | {
        t for t, rec in custom_template_records().items()
        if rec.get("department", "privacy") == department}
    sessions = [x for x in list_saved() if x.get("type") in types]

    kpis = {"total": len(sessions), "open": 0, "needs_more": 0,
            "approved": 0, "rejected": 0, "due_review": 0}
    for x in sessions:
        st = x.get("status")
        if st == "pending_review":
            kpis["open"] += 1
        elif st in ("needs_more", "needs_answers"):
            kpis["needs_more"] += 1
        elif st in kpis:
            kpis[st] += 1
        if x.get("review_due"):
            kpis["due_review"] += 1

    risk = {"high": 0, "medium": 0, "low": 0, "minimal": 0, "unrated": 0}
    for x in sessions:
        risk[x.get("residual_risk") if x.get("residual_risk") in risk
             else "unrated"] += 1

    def _month(ts: float) -> str:
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        return f"{d.year}-{d.month:02d}"

    now = datetime.now(tz=timezone.utc)
    months: list[str] = []
    y, m = now.year, now.month
    for _ in range(12):
        months.insert(0, f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    monthly = {k: {"d": k, "opened": 0, "decided": 0} for k in months}
    for x in sessions:
        for field, key in (("created_at", "opened"), ("decided_at", "decided")):
            ts = x.get(field)
            if ts and _month(float(ts)) in monthly:
                monthly[_month(float(ts))][key] += 1

    by_type: dict[str, dict] = {}
    for x in sessions:
        row = by_type.setdefault(x.get("type") or "?", {
            "label": (x.get("type") or "?").replace("_", " "),
            "total": 0, "approved": 0})
        row["total"] += 1
        if x.get("status") == "approved":
            row["approved"] += 1
    return {"kpis": kpis, "risk": risk, "monthly": list(monthly.values()),
            "types": sorted(by_type.values(), key=lambda r: r["total"],
                            reverse=True)[:6]}


def _board_savings(w, owner, days: int) -> dict:
    """The value board: the same honest math as /savings (client-set
    assumptions x completed work - agent spend), plus a daily delivered
    series so the page can show work over time."""
    from maverick import savings as savings_mod
    from maverick.config import get_value
    day_list, since, _prev = _board_day_list(days)
    own, params = _owner_sql(owner)
    daily = {d.isoformat(): {"d": d.isoformat(), "n": 0} for d in day_list}
    for day, n in w.conn.execute(
            "SELECT date(updated_at, 'unixepoch') AS day, COUNT(*) FROM goals "
            "WHERE status = 'done' AND updated_at >= ?" + own + " GROUP BY day",
            (since, *params)).fetchall():
        if day in daily:
            daily[day]["n"] = int(n)
    report = savings_mod.to_dict(
        savings_mod.compute(w, window_days=days, cfg=get_value()))
    return {"report": report, "daily_done": list(daily.values())}


def _board_oversight(w, owner, days: int) -> dict:
    """Mission-control rollup: guardrail interventions from the audit log
    (fail-soft: unreadable log yields an empty board, never a 500), the
    pending human queue, and the halt state."""
    from datetime import date, timedelta
    day_list, since, _prev = _board_day_list(days)
    daily = {d.isoformat(): {"d": d.isoformat(), "n": 0} for d in day_list}
    by_kind: dict[str, int] = {}
    total = 0
    try:
        from maverick.audit.export import iter_audit_events
        since_day = (date.today() - timedelta(days=days)).isoformat()
        scanned = 0
        for e in iter_audit_events(since=since_day):
            scanned += 1
            if scanned > 100_000:
                break
            total += 1
            kind = str(e.get("kind") or "event")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            ts = e.get("ts")
            if ts:
                day = date.fromtimestamp(float(ts)).isoformat()
                if day in daily:
                    daily[day]["n"] += 1
    except Exception:  # pragma: no cover -- fail-soft like the page
        pass
    try:
        pending = sum(1 for a in w.list_approvals(limit=500)
                      if getattr(a, "status", "") == "pending")
    except Exception:  # pragma: no cover
        pending = 0
    try:
        halted = bool(w.active_halt())
    except Exception:  # pragma: no cover
        halted = False
    kinds = sorted(({"kind": k.replace("_", " "), "n": n}
                    for k, n in by_kind.items()),
                   key=lambda r: r["n"], reverse=True)[:8]
    return {"interventions": total, "by_kind": kinds,
            "daily": list(daily.values()),
            "pending_approvals": pending, "halted": halted}


@router.get("/dashboards/{board}")
async def dashboard_board(request: Request, board: str,
                          days: int | None = None) -> dict:
    """Executive-board payload for the Overview / Spend / Workforce pages."""
    require_permission(request, "view")
    w = _world()
    owner = goal_owner_filter(request)
    d = _board_days(days)
    if board in ("privacy", "finance", "security"):
        payload = _board_workspace(board)
    elif board in ("overview", "spend", "workforce", "savings", "oversight"):
        build = {"overview": _board_overview, "spend": _board_spend,
                 "workforce": _board_workforce, "savings": _board_savings,
                 "oversight": _board_oversight}[board]
        payload = build(w, owner, d)
    else:
        raise HTTPException(status_code=404, detail="unknown board")
    payload.update({"board": board, "days": d, "generated_at": time.time()})
    return payload


@router.post("/admin/tenants", response_model=TenantOut, status_code=201)
async def create_tenant(request: Request, body: TenantCreateIn) -> TenantOut:
    require_global_permission(request, "admin")
    from maverick.tenant import registry as tenant_registry
    try:
        rec = tenant_registry.create_tenant(
            body.id, plan=body.plan, display_name=body.display_name,
            max_daily_dollars=body.max_daily_dollars,
        )
    except ValueError as e:
        # Already exists, or an invalid/over-long id.
        raise HTTPException(status_code=409, detail=str(e)) from e
    return _to_tenant_out(rec)


@router.get("/admin/tenants/{tenant_id}", response_model=TenantOut)
async def get_tenant(request: Request, tenant_id: str) -> TenantOut:
    require_global_permission(request, "admin")
    return _to_tenant_out(_get_tenant_or_404(tenant_id))


@router.post("/admin/tenants/{tenant_id}/suspend", response_model=TenantOut)
async def suspend_tenant(request: Request, tenant_id: str) -> TenantOut:
    require_global_permission(request, "admin")
    from maverick.tenant import registry as tenant_registry
    _get_tenant_or_404(tenant_id)
    return _to_tenant_out(tenant_registry.suspend_tenant(tenant_id))


@router.post("/admin/tenants/{tenant_id}/resume", response_model=TenantOut)
async def resume_tenant(request: Request, tenant_id: str) -> TenantOut:
    require_global_permission(request, "admin")
    from maverick.tenant import registry as tenant_registry
    _get_tenant_or_404(tenant_id)
    return _to_tenant_out(tenant_registry.resume_tenant(tenant_id))


@router.post("/admin/tenants/{tenant_id}/plan", response_model=TenantOut)
async def set_tenant_plan(
    request: Request, tenant_id: str, body: TenantPlanIn,
) -> TenantOut:
    require_global_permission(request, "admin")
    from maverick.tenant import registry as tenant_registry
    _get_tenant_or_404(tenant_id)
    return _to_tenant_out(tenant_registry.set_plan(tenant_id, body.plan))


@router.post("/admin/tenants/{tenant_id}/quota", response_model=TenantOut)
async def set_tenant_quota(
    request: Request, tenant_id: str, body: TenantQuotaIn,
) -> TenantOut:
    require_global_permission(request, "admin")
    from maverick.tenant import registry as tenant_registry
    _get_tenant_or_404(tenant_id)
    return _to_tenant_out(
        tenant_registry.set_quota(tenant_id, body.max_daily_dollars)
    )


@router.delete("/admin/tenants/{tenant_id}", status_code=204)
async def delete_tenant(
    request: Request, tenant_id: str, purge: bool = False,
) -> Response:
    require_global_permission(request, "admin")
    from maverick.tenant import registry as tenant_registry
    _get_tenant_or_404(tenant_id)
    tenant_registry.delete_tenant(tenant_id, purge=purge)
    return Response(status_code=204)


# Per-tenant RBAC: a principal can hold a different role in each tenant. These
# memberships override the global role for that tenant only (bootstrap admins
# stay globally admin). Managed admin-only.

def _reject_tenant_role_assignment_under_per_user_tenancy() -> None:
    """Per-tenant RBAC keys on the request's active tenant, but per-user tenancy
    (``MAVERICK_TENANT_BY_USER``) force-pins every request to the caller's own
    isolated tenant (``api:<principal>``) -- so a role assigned to any named
    tenant can never be the active tenant and would be stored but DEAD. Reject
    the mutation so the silent no-op becomes an explicit error instead of a
    footgun (an admin thinking they scoped a user when they did not). Use the
    global per-user role assignment (``POST /users/set``) in that mode.

    Deletions remain allowed: stale roles for generated ``api:<principal>``
    tenants can still be active under the read path and must be revocable.
    """
    from maverick.paths import tenant_by_user_enabled
    if tenant_by_user_enabled():
        raise HTTPException(
            status_code=409,
            detail="Tenant-level roles aren't available on this deployment — "
                   "per-user tenancy is enabled, so each user already works in "
                   "their own isolated tenant (MAVERICK_TENANT_BY_USER). Assign "
                   "the user a global role instead (POST /users/set).",
        )


@router.get("/admin/tenants/{tenant_id}/roles", response_model=dict[str, str])
async def list_tenant_roles(request: Request, tenant_id: str) -> dict[str, str]:
    require_global_permission(request, "admin")
    from maverick_dashboard import rbac
    _get_tenant_or_404(tenant_id)
    return rbac.list_tenant_roles(tenant_id)


@router.put("/admin/tenants/{tenant_id}/roles/{principal}", status_code=204)
async def set_tenant_role(
    request: Request, tenant_id: str, principal: str, body: TenantRoleIn,
) -> Response:
    require_global_permission(request, "admin")
    _reject_tenant_role_assignment_under_per_user_tenancy()
    from maverick_dashboard import rbac
    _get_tenant_or_404(tenant_id)
    rbac.set_tenant_role(tenant_id, principal, body.role,
                         actor=caller_principal(request) or "local")
    return Response(status_code=204)


@router.delete("/admin/tenants/{tenant_id}/roles/{principal}", status_code=204)
async def remove_tenant_role(
    request: Request, tenant_id: str, principal: str,
) -> Response:
    require_global_permission(request, "admin")
    from maverick_dashboard import rbac
    _get_tenant_or_404(tenant_id)
    rbac.remove_tenant_role(tenant_id, principal,
                            actor=caller_principal(request) or "local")
    return Response(status_code=204)


# Per-user department access (suite grants): job-function scoping of the
# workforce — a finance analyst uses Finance specialists, never Legal's.
# Global control-plane data like the RBAC roster, so managed under the GLOBAL
# admin role. Enforcement lives in auth.caller_suites/require_suite and the
# departments/fleets routes.

@router.get("/users/suites")
async def list_user_suites(request: Request) -> dict:
    """Every explicit department grant, plus the grantable suite catalog."""
    require_global_permission(request, "admin")
    from maverick_dashboard import suite_grants
    default = suite_grants.default_suites()
    return {
        "grants": suite_grants.list_grants(),
        "suites": sorted(suite_grants.known_suites()),
        "default_suites": sorted(default) if default is not None else None,
    }


@router.put("/users/{principal}/suites", status_code=204)
async def set_user_suites(
    request: Request, principal: str, body: UserSuitesIn,
) -> Response:
    """Scope a user to exactly these departments (empty list = none)."""
    require_global_permission(request, "admin")
    from maverick_dashboard import suite_grants
    try:
        suite_grants.set_suites(principal, body.suites,
                                actor=caller_principal(request) or "local")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(status_code=204)


@router.delete("/users/{principal}/suites", status_code=204)
async def remove_user_suites(request: Request, principal: str) -> Response:
    """Lift a user's department scoping (back to unrestricted/default)."""
    require_global_permission(request, "admin")
    from maverick_dashboard import suite_grants
    suite_grants.remove_grant(principal,
                              actor=caller_principal(request) or "local")
    return Response(status_code=204)


@router.post(
    "/goals",
    response_model=GoalOut,
    status_code=201,
    responses={
        201: {
            "description": "Goal accepted, or the original goal replayed",
            "headers": {
                "Location": {"schema": {"type": "string"}},
                "Idempotency-Replayed": {"schema": {"type": "boolean"}},
            },
        },
        503: {
            "description": "Idempotency state is temporarily indeterminate",
            "headers": {
                "Retry-After": {"schema": {"type": "integer"}},
            },
        },
    },
)
async def create_goal(
    request: Request,
    payload: GoalIn,
    bg: BackgroundTasks,
    response: Response,
) -> GoalOut:
    require_permission(request, "operate")
    require_provider_or_400()
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
    # Run AS a specialist pack (JD hires, department deploys): the orchestrator
    # inherits a domain stamped on the goal row, binding that pack's persona,
    # capability envelope and compartment. Validate against the enabled roster
    # and the caller's department grant before accepting it.
    domain = (payload.domain or "").strip() or None
    if domain:
        from maverick.domain import enabled_domains, suite_for
        if domain not in enabled_domains():
            raise HTTPException(status_code=400, detail=f"unknown specialist: {domain!r}")
        require_suite(request, suite_for(domain))
    w = _world()
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
                response.headers["Location"] = f"/api/v1/goals/{g0.id}"
                response.headers["Idempotency-Replayed"] = "true"
                return _to_goal_out(g0)  # replay: original goal, no new run
    goal_id = w.create_goal(title[:200], description, owner=caller_principal(request) or "")
    if domain:
        w.set_goal_domain(goal_id, domain)
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
            channel="api", user_id=user_id, allowed_suites=allowed_suites,
        )
    else:
        bg.add_task(
            run_goal_in_background_async, goal_id,
            max_dollars, max_wall_seconds, max_depth,
            allowed_suites=allowed_suites,
        )
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=500, detail="goal vanished after create")
    response.headers["Location"] = f"/api/v1/goals/{goal_id}"
    response.headers["Idempotency-Replayed"] = "false"
    return _to_goal_out(g)


@router.get(
    "/goals",
    response_model=list[GoalOut],
    responses={
        200: {
            "description": "A page of goals, newest first",
            "headers": {
                "Pagination-Limit": {"schema": {"type": "integer"}},
                "Pagination-Offset": {"schema": {"type": "integer"}},
                "Pagination-Has-More": {"schema": {"type": "boolean"}},
                "Pagination-Next-Offset": {"schema": {"type": "integer"}},
            },
        },
    },
)
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

    Owner-scoped: a non-admin authenticated caller sees only their own goals;
    auth-off and admin callers see all (``goal_owner_filter`` returns None).
    """
    w = _world()
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    goals = w.list_goals(
        status=status, owner=goal_owner_filter(request),
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

    Owner-scoped: a non-admin authenticated caller searches only their own
    goals; auth-off and admin callers search all. Declared before
    ``/goals/{goal_id}`` so the literal ``search`` path wins over the int param.
    """
    query = (q or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 50), 200))
    goals = _world().search_goals(query, owner=goal_owner_filter(request), limit=limit)
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

# ----- v1 SSE stream resource limits -----
# Match the legacy dashboard stream hardening: open SSE streams hold an async
# task and repeatedly poll SQLite, so cap concurrency, enforce a finite stream
# lifetime, and use a server-controlled polling cadence with idle backoff. The
# semaphore lives in _shared so app and api share ONE process-wide cap (was a
# duplicate definition here -> two independent caps). _get_sse_semaphore is
# imported above.


_SSE_POLL_INTERVAL = 0.5
_SSE_MAX_POLL_INTERVAL = 5.0
_SSE_IDLE_HEARTBEAT_EVERY = 30.0
_SSE_MAX_STREAM_SECONDS = 300.0
_SSE_MAX_BATCH = 200

@router.get("/goals/{goal_id}/events/stream")
async def goal_events_stream(
    request: Request, goal_id: int, since: int = 0, limit: int = 0,
    poll: float = 1.0,
) -> StreamingResponse:
    """Real-time **SSE** stream of a goal's events (`text/event-stream`).

    Tails the durable `goal_events` log (so it works across the worker/dashboard
    process split, unlike an in-process bus): emits each new event as it lands,
    ends when the goal reaches a terminal status with no more events, or on
    client disconnect. ``limit`` (>0) closes after N events — used by tests and
    bounded consumers. ``poll`` is accepted for compatibility but ignored; the
    server controls polling cadence and idle backoff.
    """
    del poll
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)

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
            yield ": connected\n\n"   # open the stream immediately
            while True:
                if await request.is_disconnected():
                    break
                if (asyncio.get_running_loop().time() - started) >= _SSE_MAX_STREAM_SECONDS:
                    yield "event: timeout\ndata: {\"detail\": \"stream lifetime exceeded\"}\n\n"
                    return
                events = await run_in_threadpool(
                    w.goal_events, goal_id, last, _SSE_MAX_BATCH)
                for e in events:
                    yield _sse_event(e)
                    last = e.id
                    sent += 1
                    if limit and sent >= limit:
                        return
                cur = await run_in_threadpool(w.get_goal, goal_id)
                if events:
                    idle_for = 0.0
                    poll_interval = _SSE_POLL_INTERVAL
                else:
                    idle_for += poll_interval
                    if idle_for >= _SSE_IDLE_HEARTBEAT_EVERY:
                        yield ": heartbeat\n\n"
                        idle_for = 0.0
                    poll_interval = min(_SSE_MAX_POLL_INTERVAL, poll_interval * 1.5)
                if cur is not None and cur.status in _TERMINAL_STATUSES:
                    if len(events) < _SSE_MAX_BATCH:
                        yield f"event: end\ndata: {json.dumps({'status': cur.status})}\n\n"
                        return
                    # A full batch means more backlog may remain: keep draining
                    # (without sleeping) and end only once a read comes up short.
                    continue
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
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
    "/goals/{goal_id}/attachments",
    response_model=AttachmentOut,
    status_code=201,
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


def _privacy_auth_genuinely_off() -> bool:
    """Strict auth-off decision for privacy credential and mutation boundaries."""
    if os.environ.get("MAVERICK_DASHBOARD_TOKEN"):
        return False
    try:
        from maverick.config import config_source_errors, load_config

        config = load_config() or {}
        if config_source_errors():
            return False
    except Exception:
        return False
    if not isinstance(config, dict):
        return False

    def _env_override(name: str) -> bool | None:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return None
        value = raw.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        # Invalid auth policy is uncertainty, never permission to use ambient
        # credentials or mint an unattributed privacy mutation.
        return True

    dashboard = config.get("dashboard") or {}
    auth = config.get("auth") or {}
    if not isinstance(dashboard, dict) or not isinstance(auth, dict):
        return False

    configured = []
    for env_name, section_value in (
        ("MAVERICK_DASHBOARD_REQUIRE_AUTH", dashboard.get("require_auth", False)),
        ("MAVERICK_DASHBOARD_INVITES", dashboard.get("invites", False)),
    ):
        override = _env_override(env_name)
        if override is None:
            if not isinstance(section_value, bool):
                return False
            configured.append(section_value)
        else:
            configured.append(override)

    oidc = auth.get("oidc") or {}
    proxy = auth.get("proxy") or {}
    saml = auth.get("saml") or {}
    if not all(isinstance(section, dict) for section in (oidc, proxy, saml)):
        return False
    for env_name, section in (
        ("MAVERICK_OIDC_ENABLED", oidc),
        ("MAVERICK_PROXY_AUTH", proxy),
    ):
        override = _env_override(env_name)
        if override is None:
            enabled = section.get("enabled", False)
            if not isinstance(enabled, bool):
                return False
            configured.append(enabled)
        else:
            configured.append(override)
    # SAML has no enable flag: any configured surface is an auth mechanism;
    # a partial section is still uncertainty and therefore denies ambient use.
    configured.append(bool(saml))
    return not any(configured)


def _request_identity(request: Request) -> str | None:
    """Verified identity; ``None`` only in genuine local auth-off mode."""
    principal = str(caller_principal(request) or "")
    if principal:
        return principal
    expected = os.environ.get("MAVERICK_DASHBOARD_TOKEN", "")
    if expected:
        authorization = request.headers.get("authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        if supplied and hmac.compare_digest(supplied, expected):
            return "auth:dashboard-token"
        raise HTTPException(
            status_code=401,
            detail="authenticated caller identity required",
        )
    if _privacy_auth_genuinely_off():
        return None
    raise HTTPException(
        status_code=401,
        detail="authenticated caller identity required",
    )


def _request_actor(request: Request) -> str:
    """Stable attributable actor for dashboard-originated durable writes."""
    identity = _request_identity(request) or "local:dashboard"
    if len(identity) <= 256:
        return identity
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{identity[:183]}#sha256:{digest}"


def _document_source_principal(request: Request) -> str | None:
    """Credential-use principal; local auth-off keeps operator semantics."""
    principal = _request_identity(request)
    if principal is not None and len(principal) > 256:
        raise HTTPException(
            status_code=403,
            detail="authenticated principal is too long for connection grants",
        )
    return principal


@router.post("/docs/discover")
async def discover_documents(request: Request, body: DocDiscoverIn) -> dict:
    """Search the CONNECTED document sources (Microsoft Graph, Slack, Google
    Drive) for documents related to an assessment subject -- the SOW,
    contract, DPA a respondent would otherwise go hunt for.

    Read-only against the sources; returns ranked candidates with an opaque
    ``ref`` the attach endpoint round-trips. Sources without credentials are
    skipped; with nothing configured this returns an empty list and says so.
    """
    require_permission(request, "operate")
    from maverick import doc_discovery
    if not doc_discovery.enabled():
        raise HTTPException(status_code=403,
                            detail="doc discovery is disabled ([assessments] "
                                   "doc_discovery)")
    principal = _document_source_principal(request)
    allow_ambient = principal is None
    configured = await run_in_threadpool(
        doc_discovery.configured_sources,
        principal=principal,
        allow_ambient_credentials=allow_ambient,
    )
    hits = await run_in_threadpool(
        doc_discovery.discover, body.subject,
        sources=(body.sources if body.sources is not None
                 else configured or None),
        limit=body.limit,
        principal=principal,
        allow_ambient_credentials=allow_ambient,
    )
    return {
        "configured_sources": configured,
        "hits": [h.to_dict() for h in hits],
    }


@router.post(
    "/goals/{goal_id}/attachments/from-source",
    response_model=AttachmentOut,
    status_code=201,
)
async def attach_from_source(
    request: Request, goal_id: int, body: SourceAttachIn, bg: BackgroundTasks,
) -> AttachmentOut:
    """Fetch one discovered document from its source and attach it to a goal
    as evidence -- the one-click 'found it, attach it for me'.

    The download goes through the same validation as a direct upload (size
    cap, mime allowlist, executable/archive magic-byte deny), so discovery
    can't smuggle in what an upload couldn't.
    """
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)

    from maverick import doc_discovery
    from maverick.attachments import AttachmentRejected, store
    principal = _document_source_principal(request)
    try:
        data, mime = await run_in_threadpool(
            doc_discovery.fetch,
            body.source,
            body.doc_id,
            body.ref,
            principal=principal,
            allow_ambient_credentials=principal is None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 -- a source outage is a 502, not a 500
        raise HTTPException(status_code=502,
                            detail=f"fetch from {body.source} failed") from e

    filename = body.name.strip() or f"{body.source}-{body.doc_id[:24]}"
    mime = doc_discovery.resolve_mime(filename, mime)
    existing = sum(a.size_bytes for a in w.list_attachments(goal_id))
    try:
        stored = store(goal_id, filename=filename, mime=mime, data=data,
                       existing_total=existing)
    except AttachmentRejected as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    aid = w.add_attachment(
        goal_id=goal_id, filename=stored.filename, mime=stored.mime,
        size_bytes=stored.size_bytes, sha256=stored.sha256,
        path=str(stored.path),
    )
    from maverick.attachments import generate_companions
    bg.add_task(generate_companions, w, goal_id)
    return AttachmentOut(id=aid, filename=stored.filename, mime=stored.mime,
                         size_bytes=stored.size_bytes, sha256=stored.sha256)




# --------------------------------------------------------------------------- #
# Privacy ops record types (the workspace's data plane). All operate-gated:
# these records carry vendor terms, system inventories, and subject requests.
# --------------------------------------------------------------------------- #

def _entity_graph_or_403():
    from maverick import entity_graph
    if not entity_graph.enabled():
        raise HTTPException(status_code=403,
                            detail="the entity graph is disabled "
                                   "([entity_graph] enable)")
    return entity_graph


def _parse_as_of(value: str) -> float | None:
    """Accept an epoch float or a YYYY-MM-DD date (end of that day UTC), so
    'what did we know when we signed on 2026-03-01' needs no epoch math."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        day = _dt.datetime.strptime(text, "%Y-%m-%d").replace(
            tzinfo=_dt.timezone.utc)
        return (day + _dt.timedelta(days=1)).timestamp()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="as_of must be an epoch timestamp or YYYY-MM-DD") from None


@router.get("/graph/dossier")
async def graph_dossier(request: Request, vendor: str,
                        as_of: str = "") -> dict:
    """Everything the graph knows about one vendor -- reviews in force,
    decisions, open clause gaps, documents, people -- each item citing the
    governed record it derives from. ``as_of`` reconstructs a past date."""
    require_permission(request, "operate")
    graph = _entity_graph_or_403()
    return await run_in_threadpool(
        graph.dossier, vendor[:200], as_of=_parse_as_of(as_of))


@router.get("/graph/why")
async def graph_why(request: Request, vendor: str) -> dict:
    """Walk the latest decision on a vendor back to its evidence: decision ->
    reviewer -> reviews in force at decision time -> clause findings ->
    documents. The regulator's first question, answered with record ids."""
    require_permission(request, "operate")
    graph = _entity_graph_or_403()
    return await run_in_threadpool(graph.why, vendor[:200])


@router.get("/graph/blast-radius")
async def graph_blast_radius(request: Request, kind: str,
                             name: str) -> dict:
    """Reverse reachability: this clause/document/person turned out to be a
    problem -- which records, vendors, and decisions relied on it?"""
    require_permission(request, "operate")
    if kind not in ("clause", "document", "person", "vendor", "system",
                    "goal", "skill"):
        raise HTTPException(status_code=400, detail="unsupported entity kind")
    graph = _entity_graph_or_403()
    return await run_in_threadpool(graph.blast_radius, kind, name[:300])


@router.get("/graph/entity")
async def graph_entity(request: Request, kind: str, name: str,
                       depth: int = 2, as_of: str = "") -> dict:
    require_permission(request, "operate")
    graph = _entity_graph_or_403()
    if kind not in graph.CONCEPT_KINDS + graph.RECORD_KINDS:
        raise HTTPException(status_code=400, detail="unsupported entity kind")
    return await run_in_threadpool(
        graph.neighborhood, kind, name[:300],
        depth=max(1, min(int(depth), 4)), as_of=_parse_as_of(as_of))


@router.post("/graph/rebuild")
async def graph_rebuild(request: Request) -> dict:
    """Regenerate the index from the governed stores. Always safe: the graph
    is derived, so a rebuild can only ever say what the records say."""
    require_permission(request, "operate")
    graph = _entity_graph_or_403()
    return await run_in_threadpool(graph.rebuild)


def _privacy_ops_or_403():
    from maverick import privacy_ops
    if not privacy_ops.enabled():
        raise HTTPException(status_code=403,
                            detail="privacy ops are disabled ([privacy_ops] "
                                   "enable)")
    return privacy_ops


@router.get("/privacy/dpa-reviews")
async def list_dpa_reviews(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    return {"reviews": await run_in_threadpool(ops.list_dpa_reviews)}


@router.post("/privacy/dpa-reviews", status_code=201)
async def create_dpa_review(request: Request, body: DpaReviewIn) -> dict:
    """Clause-by-clause Art. 28 review of a pasted/extracted DPA. Every
    verdict carries its matched excerpt -- explainable, reviewer-verifiable."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    return await run_in_threadpool(
        ops.review_dpa, body.vendor, body.text,
        document_name=body.document_name,
        reviewed_by=_request_actor(request),
    )


@router.get("/privacy/dpa-reviews/{review_id}")
async def get_dpa_review(request: Request, review_id: str) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    record = await run_in_threadpool(ops.get_dpa_review, review_id[:64])
    if record is None:
        raise HTTPException(status_code=404, detail="no such review")
    return record


_PAPER_DOCX_MIME = ("application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document")
_PAPER_MAX_BYTES = 16 * 1024 * 1024


def _safe_filename(name: str) -> str:
    """A Content-Disposition-safe name: no quotes, no path separators, no
    control characters that could forge a second header."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_. ") else "-"
                      for ch in (name or ""))
    return cleaned.strip(" .-")[:120] or "document"


@router.get("/privacy/paper-reviews")
async def list_paper_reviews(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    return {"reviews": await run_in_threadpool(ops.list_paper_reviews)}


@router.post("/privacy/paper-reviews", status_code=201)
async def create_paper_review(
    request: Request,
    file: UploadFile = File(...),
    vendor: str = Form(""),
    instrument: str = Form(""),
    assessment_id: str = Form(""),
) -> dict:
    """Review a vendor's OWN paper against our clause playbook.

    Classifies the instrument (DPA vs privacy addendum), finds every departure
    from our standard positions deterministically, and produces the vendor's
    document marked up with real Word tracked changes. The upload itself is
    never persisted -- only the review record and the redline we generated."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    from maverick import docx_redline, paper_review

    filename = (file.filename or "vendor-paper")[:300]
    mime = file.content_type or "application/octet-stream"
    if not (mime in (_PAPER_DOCX_MIME, "application/pdf")
            or filename.lower().endswith((".docx", ".pdf"))):
        raise HTTPException(
            status_code=400,
            detail="only Word (.docx) and PDF documents can be reviewed")
    data = await file.read(_PAPER_MAX_BYTES + 1)
    if len(data) > _PAPER_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"document too large (limit {_PAPER_MAX_BYTES} bytes)")

    extraction: dict = {}
    text = await run_in_threadpool(
        ops._document_text, data, mime, _metadata=extraction)
    if not (text or "").strip():
        raise HTTPException(
            status_code=422,
            detail="no text could be read from that file — a scanned PDF "
                   "needs OCR before it can be reviewed")

    who = _request_actor(request)
    review = await run_in_threadpool(
        paper_review.review_paper, text, vendor=vendor.strip()[:200],
        instrument=instrument.strip().lower(), document_name=filename)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _redline():
        edits = review.edits()
        if mime == _PAPER_DOCX_MIME or filename.lower().endswith(".docx"):
            try:
                return docx_redline.redline_docx(data, edits, date=stamp)
            except docx_redline.RedlineError:
                pass   # fall back to a reconstruction rather than failing
        return docx_redline.build_redlined_docx(
            review.source_paragraphs, edits, date=stamp,
            title=f"{review.vendor or 'Vendor'} — "
                  f"{review.to_dict()['instrument_label']} (Lightwork redline)")

    result = await run_in_threadpool(_redline)
    stem = _safe_filename(review.vendor or "vendor")[:60] or "vendor"
    record = await run_in_threadpool(
        ops.save_paper_review, review, redline=result.content,
        redline_result=result, reviewed_by=who,
        assessment_id=assessment_id.strip()[:128],
        redline_filename=f"{stem}-{review.instrument}-redline.docx")
    return {**record, "extraction": extraction,
            "redline": result.to_dict()}


@router.get("/privacy/paper-reviews/{review_id}")
async def get_paper_review(request: Request, review_id: str) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    record = await run_in_threadpool(ops.get_paper_review, review_id[:64])
    if record is None:
        raise HTTPException(status_code=404, detail="no such review")
    return record


@router.get("/privacy/paper-reviews/{review_id}/redline")
async def download_paper_redline(request: Request, review_id: str) -> Response:
    """The exact tracked-changes package that was reviewed and filed."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    rid = review_id[:64]
    record = await run_in_threadpool(ops.get_paper_review, rid)
    if record is None:
        raise HTTPException(status_code=404, detail="no such review")
    body = await run_in_threadpool(ops.paper_redline_bytes, rid)
    if not body:
        raise HTTPException(status_code=404,
                            detail="this review has no redline document")
    name = record.get("redline_filename") or f"{rid}-redline.docx"
    return Response(
        content=body, media_type=_PAPER_DOCX_MIME,
        headers={"Content-Disposition":
                 f'attachment; filename="{_safe_filename(name)}"'})


@router.get("/privacy/paper-reviews/{review_id}/memo")
async def get_paper_memo(request: Request, review_id: str) -> Response:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    record = await run_in_threadpool(ops.get_paper_review, review_id[:64])
    if record is None:
        raise HTTPException(status_code=404, detail="no such review")
    return PlainTextResponse(record.get("memo") or "")


@router.get("/privacy/our-paper")
async def draft_our_paper_docx(request: Request, vendor: str = "",
                               instrument: str = "dpa") -> Response:
    """OUR template instrument for a vendor, as a Word document with every
    vendor-specific value filled in red for counsel to verify.

    Deterministic: the clause language is the operator's playbook (shipped
    positions underneath) -- no model is involved. Nothing is stored; the
    draft is regenerable from the playbook at any time."""
    require_permission(request, "operate")
    _privacy_ops_or_403()
    from maverick import paper_review
    inst = (instrument or "dpa").strip().lower()
    try:
        draft = await run_in_threadpool(
            paper_review.draft_our_paper, vendor.strip()[:200],
            instrument=inst)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    stem = _safe_filename(vendor.strip() or "vendor")[:60] or "vendor"
    return Response(
        content=draft.content, media_type=_PAPER_DOCX_MIME,
        headers={"Content-Disposition":
                 f'attachment; filename="{stem}-{inst}-our-paper.docx"'})


@router.get("/privacy/clause-playbook")
async def get_clause_playbook(request: Request) -> dict:
    """Our standard clause positions -- what a vendor's paper is redlined to.

    ``customised`` names the clauses this deployment has overridden, so an
    operator can see at a glance whether they are running our language or
    their own."""
    require_permission(request, "operate")
    from maverick import paper_review
    shipped = paper_review.default_playbook()
    active = await run_in_threadpool(paper_review.load_playbook)
    return {
        "path": str(paper_review.playbook_path()),
        "positions": active,
        "shipped": shipped,
        "customised": sorted(k for k, v in active.items()
                             if shipped.get(k) != v),
    }


@router.post("/privacy/clause-playbook")
async def set_clause_playbook(request: Request,
                              body: ClausePlaybookIn) -> dict:
    """Write this deployment's own clause positions.

    A customer's standard positions are theirs, not ours -- this is the setup
    step that makes the redline speak in their language. Omitted clauses keep
    the shipped position."""
    require_permission(request, "admin")
    from maverick import paper_review
    known = set(paper_review.default_playbook())
    unknown = sorted(set(body.positions) - known)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown clause id(s): {', '.join(unknown[:8])}")
    merged = paper_review.default_playbook()
    merged.update({k: v.strip() for k, v in body.positions.items()
                   if v and v.strip()})
    path = await run_in_threadpool(paper_review.write_playbook, merged)
    active = await run_in_threadpool(paper_review.load_playbook)
    shipped = paper_review.default_playbook()
    return {"ok": True, "path": str(path),
            "customised": sorted(k for k, v in active.items()
                                 if shipped.get(k) != v)}


@router.get("/privacy/dpa-documents")
async def search_dpa_documents(request: Request, vendor: str = "") -> dict:
    """Find a vendor's DPA/contract where it already lives (MS Graph /
    Slack / Google Drive) so the review starts from the real document."""
    require_permission(request, "operate")
    _privacy_ops_or_403()
    from maverick import doc_discovery
    # "enabled" means it can actually search: feature on AND >=1 source
    # holds credentials — otherwise the UI says "connect a source", not
    # "no matching documents".
    principal = _document_source_principal(request)
    allow_ambient = principal is None
    configured = await run_in_threadpool(
        doc_discovery.configured_sources,
        principal=principal,
        allow_ambient_credentials=allow_ambient,
    )
    if not doc_discovery.enabled() or not configured:
        return {"enabled": False, "hits": []}
    hits = await run_in_threadpool(
        doc_discovery.discover, vendor[:200],
        keywords=("dpa", "data processing", "agreement", "contract"),
        sources=configured,
        principal=principal,
        allow_ambient_credentials=allow_ambient,
    )
    return {"enabled": True, "hits": [h.to_dict() for h in hits]}


@router.post("/privacy/dpa-reviews/from-document", status_code=201)
async def create_dpa_review_from_document(request: Request,
                                          body: DpaFromDocumentIn) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    principal = _document_source_principal(request)
    try:
        return await run_in_threadpool(
            ops.review_dpa_from_document, body.vendor, body.source,
            body.doc_id,
            ref=body.ref,
            document_name=body.document_name,
            reviewed_by=_request_actor(request),
            principal=principal,
            allow_ambient_credentials=principal is None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 - source failures are status-only
        raise HTTPException(
            status_code=502,
            detail="document source request failed",
        ) from e


@router.get("/privacy/ai-systems")
async def list_ai_systems(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    return {"systems": await run_in_threadpool(ops.list_ai_systems)}


@router.post("/privacy/ai-systems", status_code=201)
async def register_ai_system(request: Request, body: AiSystemIn) -> dict:
    """Add an AI system to the registry, classified against the EU AI Act's
    tiers on the way in (screening aid, not legal advice)."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    return await run_in_threadpool(
        ops.register_ai_system, body.name, body.purpose,
        provider=body.provider, owner=body.owner,
        assessment_id=body.assessment_id,
        registered_by=_request_actor(request),
    )


@router.post("/privacy/ai-systems/from-assessment/{assessment_id}",
             status_code=201)
async def register_ai_system_from_assessment(request: Request,
                                             assessment_id: str) -> dict:
    """A completed AIRA feeds the registry the way a PIA feeds the RoPA;
    the assessment's attestations only ever ratchet the tier upward."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    record = await run_in_threadpool(
        ops.register_ai_system_from_assessment,
        assessment_id[:64],
        registered_by=_request_actor(request),
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no such assessment")
    return record


@router.get("/privacy/ropa")
async def list_ropa(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    return {"activities": await run_in_threadpool(ops.list_ropa)}


@router.post("/privacy/ropa", status_code=201)
async def upsert_ropa(request: Request, body: RopaIn) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    payload = body.model_dump(exclude_none=True)
    ropa_id = payload.pop("ropa_id", "")
    revision = payload.pop("revision", None)
    try:
        record = await run_in_threadpool(
            ops.upsert_ropa,
            payload,
            ropa_id=ropa_id,
            updated_by=_request_actor(request),
            expected_revision=revision,
        )
    except ops.RecordConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="no such activity")
    return record


@router.post("/privacy/ropa/from-assessment/{assessment_id}", status_code=201)
async def ropa_from_assessment(request: Request, assessment_id: str) -> dict:
    """Auto-draft an Art. 30 entry from a completed assessment -- the moment
    assessments start FEEDING the inventory instead of dying in a folder."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    record = await run_in_threadpool(
        ops.draft_ropa_from_assessment,
        assessment_id[:64],
        created_by=_request_actor(request),
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no such assessment")
    return record


@router.post("/privacy/ropa/import-onetrust", status_code=201)
async def import_onetrust(request: Request, body: OneTrustImportIn) -> dict:
    """Import OneTrust's RoPA/Data-Mapping CSV export -- their register
    comes in with source provenance and stays editable here."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    imported = await run_in_threadpool(
        ops.import_onetrust_ropa,
        body.csv_text,
        imported_by=_request_actor(request),
    )
    return {"imported": len(imported),
            "activities": [e["activity"] for e in imported]}


def _csv_formula_safe(value: object) -> object:
    """Neutralize spreadsheet formulas while preserving ordinary CSV values."""
    from maverick.tools.spreadsheet import _neutralize_formula

    return _neutralize_formula(value)


@router.get("/privacy/ropa/export.csv")
async def export_ropa_csv(request: Request) -> Response:
    """The Art. 30 register as CSV -- what a supervisory authority asks for."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    rows = await run_in_threadpool(ops.export_ropa_art30)
    import csv
    import io
    buf = io.StringIO()
    fields = ["id", *ops.ROPA_FIELDS, "assessment_id", "source"]
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for r in rows:
        w.writerow({field: _csv_formula_safe(r.get(field, "")) for field in fields})
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="ropa-art30.csv"'})


@router.get("/privacy/dsar")
async def list_dsar_requests(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    return {"requests": await run_in_threadpool(ops.list_dsars)}


def _dsar_aging_payload(rows: list[dict]) -> dict:
    """SLA aging bands over the open DSAR clocks. Shared by the aging
    endpoint and the command-center board so both always agree."""
    bands = {"overdue": 0, "d0_7": 0, "d8_14": 0, "d15_30": 0, "d30_plus": 0}
    by_kind: dict[str, dict] = {}
    open_rows = [r for r in rows if r.get("status") == "open"]
    for r in open_rows:
        dl = int(r.get("days_left", 0))
        band = ("overdue" if dl < 0 else "d0_7" if dl <= 7
                else "d8_14" if dl <= 14 else "d15_30" if dl <= 30
                else "d30_plus")
        bands[band] += 1
        k = str(r.get("kind", "other"))
        kb = by_kind.setdefault(k, {"overdue": 0, "d0_7": 0, "d8_14": 0,
                                    "d15_30": 0, "d30_plus": 0, "total": 0})
        kb[band] += 1
        kb["total"] += 1
    return {"open": len(open_rows), "bands": bands, "by_kind": by_kind,
            "overdue": bands["overdue"],
            "closing_soon": bands["overdue"] + bands["d0_7"]}


@router.get("/privacy/dsar/aging")
async def dsar_aging(request: Request) -> dict:
    """SLA aging over the open DSAR clocks: how many requests sit in each
    band to the statutory deadline, and the same split by request kind -- the
    at-a-glance heatmap of where the queue is about to breach."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    rows = await run_in_threadpool(ops.list_dsars)
    return _dsar_aging_payload(rows)


# Chapter V safeguard markers recognised in a RoPA transfer description.
_SAFEGUARD_MARKERS = ("scc", "standard contractual", "adequacy", "bcr",
                      "binding corporate", "chapter v", "art. 46",
                      "article 46")


def _transfer_flow_rows(ropa: list[dict], saved: list[dict]) -> list[dict]:
    """Cross-border flows from the Art. 30 register plus TIA records. Shared
    by the transfer-map endpoint and the command-center board."""
    flows: list[dict] = []
    for r in ropa:
        transfers = str(r.get("transfers", "")).strip()
        if not transfers or transfers.lower() in ("none", "n/a", "no"):
            continue
        low = transfers.lower()
        flows.append({
            "source": "RoPA",
            "activity": r.get("activity", "") or r.get("controller", ""),
            "destination": transfers[:300],
            "recipients": str(r.get("recipients", ""))[:300],
            "safeguarded": any(s in low for s in _SAFEGUARD_MARKERS),
            "ref": r.get("id", ""),
        })
    # TIAs are, by definition, cross-border transfer assessments.
    for s in saved:
        if s.get("type") != "tia":
            continue
        flows.append({
            "source": "TIA",
            "activity": s.get("subject", ""),
            "destination": s.get("subject", ""),
            "recipients": "",
            "safeguarded": s.get("residual_risk") in ("minimal", "low"),
            "residual_risk": s.get("residual_risk", "?"),
            "ref": s.get("id", ""),
        })
    return flows


@router.get("/privacy/transfer-map")
async def transfer_map(request: Request) -> dict:
    """Where personal data flows out: cross-border transfers drawn from the
    Art. 30 register and any Transfer Impact Assessments -- destination,
    recipients, and whether a Chapter V safeguard is recorded. The map behind
    the paperwork."""
    # Operate-gated to match /privacy/ropa: this surfaces the register's
    # recipient and destination content, not the viewer-tier summary rows.
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    from maverick.assessment import list_saved
    ropa = await run_in_threadpool(ops.list_ropa)
    saved = await run_in_threadpool(list_saved)
    flows = _transfer_flow_rows(ropa, saved)
    unsafe = sum(1 for f in flows if not f["safeguarded"])
    return {"flows": flows, "total": len(flows), "unsafeguarded": unsafe}


# ---- privacy command center (the board behind /privacy/board) --------------

_RISK_LEVELS = ("minimal", "low", "medium", "high")
_OPEN_ASSESSMENT_STATUSES = ("pending_review", "needs_more")
# Client-side cross-filtering works off summary rows; the cap keeps the
# payload bounded at program volume without silently changing the KPIs
# (those are computed over everything server-side).
_BOARD_SESSION_CAP = 1500


def _privacy_session_rows() -> list[dict]:
    """Saved assessments filtered to the privacy department's types
    (built-ins plus custom templates authored for privacy) -- the board
    never counts the finance chassis."""
    from maverick.assessment import custom_template_records, list_saved

    from .app import PRIVACY_ASSESSMENT_TYPES
    types = set(PRIVACY_ASSESSMENT_TYPES) | {
        t for t, rec in custom_template_records().items()
        if rec.get("department", "privacy") == "privacy"}
    return [s for s in list_saved() if s.get("type") in types]


def _board_month_series(sessions: list[dict], now: float,
                        months: int = 12) -> list[dict]:
    """Opened/decided counts per calendar month (UTC), oldest first."""
    t = time.gmtime(now)
    y, m = t.tm_year, t.tm_mon
    keys: list[str] = []
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    opened = dict.fromkeys(keys, 0)
    decided = dict.fromkeys(keys, 0)
    for s in sessions:
        ck = time.strftime("%Y-%m", time.gmtime(float(s.get("created_at")
                                                      or 0)))
        if ck in opened:
            opened[ck] += 1
        if s.get("decided_at"):
            dk = time.strftime("%Y-%m",
                               time.gmtime(float(s["decided_at"])))
            if dk in decided:
                decided[dk] += 1
    return [{"month": k, "opened": opened[k], "decided": decided[k]}
            for k in keys]


def _board_deltas(sessions: list[dict], now: float,
                  days: int = 30) -> dict:
    """This window vs the prior window -- the KPI delta chips."""
    cur_lo, prev_lo = now - days * 86400, now - 2 * days * 86400
    d = {"window_days": days,
         "opened": {"cur": 0, "prev": 0},
         "decided": {"cur": 0, "prev": 0},
         "high_residual": {"cur": 0, "prev": 0}}
    for s in sessions:
        c = float(s.get("created_at") or 0)
        bucket = "cur" if c >= cur_lo else "prev" if c >= prev_lo else ""
        if bucket:
            d["opened"][bucket] += 1
            if s.get("residual_risk") == "high":
                d["high_residual"][bucket] += 1
        dec = float(s.get("decided_at") or 0)
        if dec >= cur_lo:
            d["decided"]["cur"] += 1
        elif dec >= prev_lo:
            d["decided"]["prev"] += 1
    return d


def _board_assessment_plane(sessions: list[dict], now: float) -> dict:
    """KPIs, the inherent->residual risk mix, per-type volumes, and the
    12-month throughput series over the privacy assessment records."""
    mix = {"inherent": dict.fromkeys(_RISK_LEVELS, 0),
           "residual": dict.fromkeys(_RISK_LEVELS, 0)}
    by_type: dict[str, dict] = {}
    for s in sessions:
        for side in ("inherent", "residual"):
            lvl = s.get(f"{side}_risk")
            if lvl in mix[side]:
                mix[side][lvl] += 1
        row = by_type.setdefault(
            str(s.get("type", "?")),
            {"type": str(s.get("type", "?")), "total": 0, "open": 0,
             "high": 0})
        row["total"] += 1
        if s.get("status") in _OPEN_ASSESSMENT_STATUSES:
            row["open"] += 1
        if s.get("residual_risk") == "high":
            row["high"] += 1
    kpis = {
        "assessments": len(sessions),
        "open_work": sum(1 for s in sessions
                         if s.get("status") in _OPEN_ASSESSMENT_STATUSES),
        "high_residual": mix["residual"]["high"],
        "review_due": sum(1 for s in sessions if s.get("review_due")),
        "risk_accepted": sum(1 for s in sessions if s.get("risk_accepted")),
    }
    return {"kpis": kpis, "risk_mix": mix,
            "by_type": sorted(by_type.values(),
                              key=lambda r: -r["total"]),
            "throughput": _board_month_series(sessions, now),
            "deltas": _board_deltas(sessions, now)}


def _board_session_rows(sessions: list[dict]) -> list[dict]:
    """The bounded summary rows the board cross-filters client-side."""
    return [{"id": s.get("id"), "type": s.get("type"),
             "subject": s.get("subject"), "status": s.get("status"),
             "inherent": s.get("inherent_risk"),
             "residual": s.get("residual_risk"),
             "created_at": s.get("created_at"),
             "decided_at": s.get("decided_at"),
             "review_due": bool(s.get("review_due")),
             "review_due_reason": s.get("review_due_reason", ""),
             "risk_accepted": bool(s.get("risk_accepted")),
             "open_followups": s.get("open_followups", 0)}
            for s in sessions[:_BOARD_SESSION_CAP]]


def _board_posture(sessions: list[dict], dsar_rows: list[dict],
                   incidents: list[dict], dpa: list[dict],
                   flows: list[dict]) -> list[dict]:
    """The posture gauges: each one a real ratio out of the registers, never
    a synthetic score. ``pct`` is None when a register is empty."""
    def item(key: str, label: str, ok: int, total: int, note: str) -> dict:
        return {"key": key, "label": label, "ok": ok, "total": total,
                "pct": round(100.0 * ok / total, 1) if total else None,
                "note": note}

    open_dsar = [r for r in dsar_rows if r.get("status") == "open"]
    approved = [s for s in sessions if s.get("status") == "approved"]
    inc_ok = sum(1 for r in incidents
                 if r.get("notification") or not r.get("clock_breached"))
    clauses_total = sum(int(r.get("clauses_total") or 0) for r in dpa)
    clauses_present = sum(int(r.get("clauses_present") or 0) for r in dpa)
    return [
        item("dsar_sla", "DSAR SLA health",
             sum(1 for r in open_dsar if not r.get("overdue")),
             len(open_dsar),
             "open requests still inside the statutory clock"),
        item("review_cadence", "Review cadence",
             sum(1 for s in approved if not s.get("review_due")),
             len(approved),
             "approved assessments not overdue for re-review"),
        item("transfer_safeguards", "Transfer safeguards",
             sum(1 for f in flows if f.get("safeguarded")), len(flows),
             "cross-border flows with a Chapter V safeguard on file"),
        item("incident_clock", "Incident 72h clock",
             inc_ok, len(incidents),
             "incidents decided or still inside the Art. 33 window"),
        item("dpa_coverage", "DPA clause coverage",
             clauses_present, clauses_total,
             "Art. 28(3) clauses found across reviewed DPAs"),
    ]


@router.get("/privacy/board")
async def privacy_board(request: Request) -> dict:
    """Everything the privacy command center renders, in one round trip:
    program KPIs with 30-day deltas, the inherent->residual risk mix,
    per-framework volumes, 12 months of throughput, DSAR SLA aging, the
    transfer map, AI Act tiers, and the posture gauges. Operate-gated like
    the workspace it summarizes; works (with empty registers) when
    [privacy_ops] is disabled."""
    require_permission(request, "operate")
    from maverick import privacy_ops
    now = time.time()
    sessions = await run_in_threadpool(_privacy_session_rows)
    payload = _board_assessment_plane(sessions, now)
    payload["sessions"] = _board_session_rows(sessions)

    dsar_rows: list[dict] = []
    incidents: list[dict] = []
    dpa: list[dict] = []
    ai_rows: list[dict] = []
    ropa: list[dict] = []
    flows: list[dict] = []
    enabled = privacy_ops.enabled()
    if enabled:
        dsar_rows = await run_in_threadpool(privacy_ops.list_dsars)
        incidents = await run_in_threadpool(privacy_ops.list_incidents)
        dpa = await run_in_threadpool(privacy_ops.list_dpa_reviews)
        ai_rows = await run_in_threadpool(privacy_ops.list_ai_systems)
        ropa = await run_in_threadpool(privacy_ops.list_ropa)
        flows = _transfer_flow_rows(ropa, sessions)

    payload["records_enabled"] = enabled
    payload["dsar"] = _dsar_aging_payload(dsar_rows)
    payload["transfers"] = {
        "flows": flows[:40], "total": len(flows),
        "unsafeguarded": sum(1 for f in flows
                             if not f.get("safeguarded"))}
    payload["incidents"] = {
        "total": len(incidents),
        "on_clock": sum(1 for r in incidents
                        if not r.get("notification")
                        and r.get("status") in ("open", "notify")),
        "breached": sum(1 for r in incidents if r.get("clock_breached"))}
    tiers = {"prohibited": 0, "high": 0, "limited": 0, "minimal": 0}
    for r in ai_rows:
        t = str(r.get("tier", ""))
        if t in tiers:
            tiers[t] += 1
    payload["ai_tiers"] = tiers
    payload["registers"] = {"ropa": len(ropa), "dpa": len(dpa),
                            "ai": len(ai_rows), "dsar": len(dsar_rows)}
    payload["posture"] = _board_posture(sessions, dsar_rows, incidents,
                                        dpa, flows)
    from maverick.assessment import acceptance_metrics

    from .app import PRIVACY_ASSESSMENT_TYPES
    payload["learning"] = await run_in_threadpool(
        acceptance_metrics, types=set(PRIVACY_ASSESSMENT_TYPES))
    payload["generated_at"] = now
    return payload


def _finance_session_rows() -> list[dict]:
    """Saved assessments filtered to the finance department's types --
    the same chassis the privacy board runs on, different department."""
    from maverick.assessment import custom_template_records, list_saved

    from .app import FINANCE_ASSESSMENT_TYPES
    types = set(FINANCE_ASSESSMENT_TYPES) | {
        t for t, rec in custom_template_records().items()
        if rec.get("department") == "finance"}
    return [s for s in list_saved() if s.get("type") in types]


def _decision_rate(sessions: list[dict], kinds: tuple[str, ...]) -> dict:
    """Approved-vs-rejected pass rate over decided records of the given
    types -- control tests and close readiness read straight off the
    register, never estimated."""
    rows = [s for s in sessions if s.get("type") in kinds
            and s.get("status") in ("approved", "rejected")]
    ok = sum(1 for s in rows if s.get("status") == "approved")
    return {"passed": ok, "total": len(rows),
            "pct": round(100.0 * ok / len(rows), 1) if rows else None}


def _finance_sod() -> dict:
    """Structural SoD lint over the shipped finance/banking packs -- the
    record/authorize/custody/reconcile separation, checked statically."""
    try:
        from maverick.domain import builtin_dir, load_domains
        from maverick.finance.sod_linter import lint_roster
        packs = {n: d for n, d in load_domains(builtin_dir()).items()
                 if n.startswith(("finance", "bank"))}
        conflicts = lint_roster(packs)
        return {"packs": len(packs), "conflicts": len(conflicts),
                "detail": [str(c) for c in conflicts[:5]]}
    except Exception as exc:  # pragma: no cover -- packs dir varies by install
        return {"packs": 0, "conflicts": None, "error": str(exc)[:200]}


def _finance_posture() -> dict:
    """The finance control-coverage report. Control coverage, not an audit
    opinion -- the disclaimer ships with the numbers."""
    from dataclasses import asdict

    from maverick.finance.status import FINANCE_DISCLAIMER, finance_status
    checks = [asdict(c) for c in finance_status()]
    return {"controls": checks,
            "active": sum(1 for c in checks if c.get("status") == "active"),
            "total": len(checks),
            "disclaimer": FINANCE_DISCLAIMER}


@router.get("/finance/board")
async def finance_board(request: Request) -> dict:
    """The finance command center in one round trip -- the board chassis
    parameterized to the finance department: assessment KPIs and learning
    over the finance types, control-test and close-readiness pass rates
    from decided records, the SoD lint, the finance control-coverage
    posture (with its not-an-audit-opinion disclaimer), and the regulatory
    license renewal runway."""
    require_permission(request, "operate")
    now = time.time()
    sessions = await run_in_threadpool(_finance_session_rows)
    payload = _board_assessment_plane(sessions, now)
    payload["sessions"] = _board_session_rows(sessions)
    payload["control_tests"] = _decision_rate(sessions,
                                              ("sox_control", "itgc"))
    payload["close_readiness"] = _decision_rate(sessions,
                                                ("close_readiness",))
    payload["fraud_open"] = sum(
        1 for s in sessions if s.get("type") == "fraud_risk"
        and s.get("status") in ("pending_review", "needs_more"))
    payload["sod"] = await run_in_threadpool(_finance_sod)
    payload["finance_posture"] = await run_in_threadpool(_finance_posture)
    from maverick.license_registry import runway
    payload["licenses"] = await run_in_threadpool(runway)
    from maverick.assessment import acceptance_metrics

    from .app import FINANCE_ASSESSMENT_TYPES
    payload["learning"] = await run_in_threadpool(
        acceptance_metrics, types=set(FINANCE_ASSESSMENT_TYPES))
    payload["generated_at"] = now
    return payload


# ---- regulatory license register (state-by-state renewals) -----------------

@router.get("/licenses")
async def list_regulatory_licenses(request: Request) -> dict:
    """The license inventory with computed renewal runway, soonest first."""
    require_permission(request, "operate")
    from maverick.license_registry import list_licenses, runway
    return {"licenses": await run_in_threadpool(list_licenses),
            "runway": await run_in_threadpool(runway)}


@router.post("/licenses", status_code=201)
async def add_regulatory_license(request: Request, body: LicenseIn) -> dict:
    require_permission(request, "operate")
    from maverick.license_registry import add_license
    return await run_in_threadpool(
        lambda: add_license(
            name=body.name, jurisdiction=body.jurisdiction,
            authority=body.authority, license_number=body.license_number,
            holder=body.holder or "", status=body.status,
            renewal_at=body.renewal_at, cadence_days=body.cadence_days,
            added_by=_request_actor(request)))


@router.post("/licenses/{license_id}/renew")
async def renew_regulatory_license(request: Request, license_id: str,
                                   body: LicenseNoteIn) -> dict:
    """Record a completed renewal (clock advances FROM the deadline).
    Humans file with the authority; the register only records that."""
    require_permission(request, "operate")
    from maverick.license_registry import renew
    rec = await run_in_threadpool(
        lambda: renew(license_id[:40], note=body.note or "",
                      by=_request_actor(request)))
    if rec is None:
        raise HTTPException(status_code=404, detail="no such license")
    return rec


@router.post("/licenses/{license_id}/evidence")
async def license_evidence(request: Request, license_id: str,
                           body: LicenseNoteIn) -> dict:
    require_permission(request, "operate")
    from maverick.license_registry import attach_evidence
    rec = await run_in_threadpool(
        lambda: attach_evidence(license_id[:40], note=body.note or "",
                                filename=body.filename or "",
                                by=_request_actor(request)))
    if rec is None:
        raise HTTPException(status_code=404, detail="no such license")
    return rec


# ---- partner fleet (multi-tenant console) ----------------------------------

def _probe_tenant(t: dict) -> dict:
    """One synchronous probe of a client deployment: /health for liveness
    and latency, /value.json (best-effort) for the agent's own counted value
    ledger. Never raises -- the fleet view shows the error instead."""
    import httpx
    base = t["base_url"].rstrip("/")
    headers = ({"Authorization": f"Bearer {t['token']}"}
               if t.get("token") else {})
    out: dict = {"checked_at": time.time(), "ok": False, "latency_ms": None,
                 "agent": "", "version": "", "value": None, "error": ""}
    t0 = time.monotonic()
    try:
        r = httpx.get(f"{base}/health", headers=headers, timeout=4)
        out["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        out["ok"] = r.status_code == 200
        if not out["ok"]:
            out["error"] = f"health HTTP {r.status_code}"
            return out
    except Exception as exc:
        out["error"] = str(exc)[:200]
        return out
    try:
        v = httpx.get(f"{base}/value.json", headers=headers, timeout=4)
        if v.status_code == 200:
            d = v.json()
            out["agent"] = str(d.get("agent", ""))[:60]
            out["version"] = str(d.get("version", ""))[:20]
            out["value"] = {"cases": int(d.get("cases") or 0),
                            "hours": round(float(d.get("hours") or 0), 1),
                            "dollars": round(float(d.get("dollars") or 0), 2)}
    except Exception:  # value ledger is optional on older agents
        pass
    try:
        x = httpx.get(f"{base}/api/v1/external-agents", headers=headers,
                      timeout=4)
        if x.status_code == 200:
            st = (x.json() or {}).get("status") or {}
            out["external"] = {
                "enrolled": int(st.get("enrolled") or 0),
                "active": int(st.get("active") or 0),
                "over_budget": int(st.get("over_budget") or 0),
                "contained": int(st.get("contained") or 0),
            }
    except Exception:  # BYOA console is optional on older tenants
        pass
    return out


@router.get("/partner/tenants")
async def partner_list_tenants(request: Request) -> dict:
    """The partner fleet registry (tokens never echoed)."""
    require_permission(request, "operate")
    from . import partner_store
    rows = await run_in_threadpool(partner_store.list_tenants)
    return {"tenants": [partner_store.public_row(r) for r in rows]}


@router.post("/partner/tenants", status_code=201)
async def partner_add_tenant(request: Request,
                             body: PartnerTenantIn) -> dict:
    """Register a client deployment. Admin-gated: the row can carry a
    bearer token and the console probes the URL server-side."""
    require_permission(request, "admin")
    from . import partner_store
    row = await run_in_threadpool(
        lambda: partner_store.upsert_tenant(
            name=body.name, base_url=body.base_url, token=body.token,
            theme=body.theme, notes=body.notes))
    return partner_store.public_row(row)


@router.delete("/partner/tenants/{tenant_id}")
async def partner_delete_tenant(request: Request, tenant_id: str) -> dict:
    require_permission(request, "admin")
    from . import partner_store
    ok = await run_in_threadpool(partner_store.delete_tenant,
                                 tenant_id[:20])
    if not ok:
        raise HTTPException(status_code=404, detail="no such tenant")
    return {"deleted": tenant_id[:20]}


@router.post("/partner/tenants/{tenant_id}/check")
async def partner_check_tenant(request: Request, tenant_id: str) -> dict:
    require_permission(request, "operate")
    from . import partner_store
    row = next((r for r in await run_in_threadpool(
        partner_store.list_tenants) if r["id"] == tenant_id[:20]), None)
    if row is None:
        raise HTTPException(status_code=404, detail="no such tenant")
    result = await run_in_threadpool(_probe_tenant, row)
    await run_in_threadpool(partner_store.record_check, row["id"], result)
    return {"tenant": partner_store.public_row({**row,
                                                "last_check": result})}


@router.get("/partner/fleet")
async def partner_fleet(request: Request, check: int = 0) -> dict:
    """The rollup the partner console renders: every tenant with its last
    (or, with ?check=1, a fresh) probe, plus fleet totals -- healthy count
    and the summed value ledgers of every reachable agent."""
    require_permission(request, "operate")
    from . import partner_store
    rows = await run_in_threadpool(partner_store.list_tenants)
    if check:
        for row in rows:
            result = await run_in_threadpool(_probe_tenant, row)
            await run_in_threadpool(partner_store.record_check,
                                    row["id"], result)
            row["last_check"] = result
    tenants = [partner_store.public_row(r) for r in rows]
    checks = [t.get("last_check") or {} for t in tenants]
    values = [c.get("value") for c in checks if c.get("value")]
    externals = [c.get("external") for c in checks if c.get("external")]
    return {"tenants": tenants,
            "fleet": {
                "total": len(tenants),
                "healthy": sum(1 for c in checks if c.get("ok")),
                "cases": sum(v["cases"] for v in values),
                "hours": round(sum(v["hours"] for v in values), 1),
                "dollars": round(sum(v["dollars"] for v in values), 2),
                "external_agents": sum(x["enrolled"] for x in externals),
                "external_attention": sum(
                    x["over_budget"] + x["contained"] for x in externals),
            }}


# ---- audit binder (the regulator-grade evidence pack) ----------------------

def _binder_finance_section() -> dict:
    """Finance evidence for the binder: posture (with its disclaimer), the
    structural SoD lint, and the license renewal runway."""
    from maverick.license_registry import runway
    return {"posture": _finance_posture(), "sod": _finance_sod(),
            "licenses": runway()}


def _binder_payload(days: int = 90) -> dict:
    """Everything an auditor asks for, assembled from the records themselves:
    per-day chain verification over the signed audit log, the event summary,
    approvals with identity + quorum, the assessment register with revisions
    and decisions, the privacy registers, and the acceptance-learning KPIs.
    Nothing here is narrative -- every number has a record behind it."""
    from maverick.assessment import acceptance_metrics, list_saved
    from maverick.audit import default_audit_log
    from maverick.audit.signing import verify_chain

    now = time.time()
    cutoff = now - days * 86400
    log = default_audit_log()
    chain: list[dict] = []
    kinds: dict[str, int] = {}
    first_ts: float | None = None
    last_ts: float | None = None
    events_seen = 0
    for path in sorted(log.audit_dir.glob("*.ndjson")):
        try:
            day_ts = _dt.datetime.strptime(
                path.stem, "%Y-%m-%d").replace(
                tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            continue
        if day_ts < cutoff - 86400:
            continue
        breaks = [{"line": b.line_no, "reason": b.reason,
                   "detail": str(getattr(b, "detail", ""))[:200]}
                  for b in verify_chain(path)]
        rows = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rows += 1
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                events_seen += 1
                kinds[str(e.get("kind", "?"))] = \
                    kinds.get(str(e.get("kind", "?")), 0) + 1
                ts = float(e.get("ts") or 0)
                if ts:
                    first_ts = ts if first_ts is None else min(first_ts, ts)
                    last_ts = ts if last_ts is None else max(last_ts, ts)
        except OSError:
            pass
        chain.append({"day": path.stem, "events": rows,
                      "intact": not breaks, "breaks": breaks[:20]})

    w = _world()
    approvals = [{
        "id": a.id, "action": a.action, "risk": a.risk, "status": a.status,
        "requested_by": a.requested_by, "requested_at": a.requested_at,
        "decided_by": a.decided_by, "decided_at": a.decided_at,
        "approvals_required": a.approvals_required,
    } for a in w.list_approvals(limit=300)
        if float(a.requested_at or 0) >= cutoff]

    assessments = [{
        "id": s["id"], "type": s["type"], "subject": s["subject"],
        "inherent": s["inherent_risk"], "residual": s["residual_risk"],
        "status": s["status"], "revision": s["revision"],
        "template_digest": (s.get("template_digest") or "")[:16],
        "created_at": s["created_at"], "decided_at": s.get("decided_at"),
        "review_due": s.get("review_due"),
    } for s in list_saved()[:300]]

    from maverick import privacy_ops
    registers: dict = {"enabled": privacy_ops.enabled()}
    if registers["enabled"]:
        dsar_rows = privacy_ops.list_dsars()
        registers.update({
            "ropa": len(privacy_ops.list_ropa()),
            "dpa": len(privacy_ops.list_dpa_reviews()),
            "ai": len(privacy_ops.list_ai_systems()),
            "dsar": len(dsar_rows),
            "incidents": len(privacy_ops.list_incidents()),
            "dsar_aging": _dsar_aging_payload(dsar_rows),
        })

    return {
        "window_days": days,
        "generated_at": now,
        "chain": {"files": chain,
                  "intact": all(c["intact"] for c in chain) if chain
                  else None,
                  "events": events_seen,
                  "first_ts": first_ts, "last_ts": last_ts,
                  "kinds": dict(sorted(kinds.items(),
                                       key=lambda kv: -kv[1]))},
        "approvals": approvals,
        "assessments": assessments,
        "registers": registers,
        "learning": acceptance_metrics(),
        # Finance evidence: control coverage, not an audit opinion — the
        # disclaimer travels with the numbers. Plus the SoD lint and the
        # regulatory-license renewal runway.
        "finance": _binder_finance_section(),
    }


@router.get("/audit/binder")
async def audit_binder(request: Request, days: int = 90) -> dict:
    """The evidence pack as JSON -- the machine-readable twin of the
    /audit/binder page. Audit-gated like the log it summarizes."""
    require_permission(request, "audit")
    return await run_in_threadpool(_binder_payload,
                                   max(1, min(days, 730)))


@router.get("/assess/question-roi")
async def assessment_question_roi(request: Request, type: str) -> dict:
    """Which questions earn their place: per-question fire-rate and
    rating-impact evidence across every saved assessment of a type -- the
    data behind pruning a questionnaire without losing signal."""
    require_permission(request, "operate")
    from maverick.assessment import question_roi
    return await run_in_threadpool(question_roi, type[:40])


@router.post("/privacy/dsar", status_code=201)
async def open_dsar_request(request: Request, body: DsarIn) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    return await run_in_threadpool(
        ops.open_dsar,
        body.subject_id,
        body.kind,
        channel=body.channel,
        opened_by=_request_actor(request),
    )


@router.post("/privacy/dsar/from-message", status_code=201)
async def open_dsar_from_message(request: Request,
                                 body: DsarFromMessageIn) -> dict:
    """Triage an inbound message into a tracked request. The record keeps
    the matched phrases and an excerpt as provenance; a message that does
    not read as a data-subject request (or names no subject) is a 422, not
    a silently-opened case."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    record = await run_in_threadpool(
        ops.open_dsar_from_message, body.text, channel=body.channel,
        sender=body.sender, opened_by=_request_actor(request))
    if record is None:
        raise HTTPException(
            status_code=422,
            detail="no data-subject request detected (or no subject "
                   "address found) — open it manually if you disagree")
    return record


@router.post("/privacy/dsar/{request_id}/fulfill")
async def fulfill_dsar_request(request: Request, request_id: str) -> dict:
    """Access/portability run the real subject-data export. Erasure returns a
    structured argv handoff for a deliberate authenticated operator workflow;
    the API never constructs or executes a shell command."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    record = await run_in_threadpool(
        ops.fulfill_dsar,
        request_id[:64],
        fulfilled_by=_request_actor(request),
    )
    if record is None:
        raise HTTPException(status_code=404,
                            detail="no such open request")
    return record


@router.post("/privacy/dsar/{request_id}/close")
async def close_dsar_request(request: Request, request_id: str) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    record = await run_in_threadpool(
        ops.close_dsar,
        request_id[:64],
        closed_by=_request_actor(request),
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no such request")
    return record


@router.get("/privacy/report")
async def privacy_program_report(request: Request) -> dict:
    """The board-pack aggregate over every register -- computed from the
    records themselves, nothing estimated."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    return await run_in_threadpool(ops.program_report)


@router.get("/privacy/incidents")
async def list_privacy_incidents(request: Request) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    return {"incidents": await run_in_threadpool(ops.list_incidents)}


@router.post("/privacy/incidents", status_code=201)
async def open_privacy_incident(request: Request, body: IncidentIn) -> dict:
    """Open an incident: the Art. 33 clock starts now; the register tracks
    it and never notifies anyone itself."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    record = await run_in_threadpool(
        ops.open_incident, body.title, severity=body.severity,
        description=body.description, categories=body.categories,
        affected_estimate=body.affected_estimate,
        reported_by=_request_actor(request))
    return record


@router.post("/privacy/incidents/{incident_id}/notification")
async def decide_privacy_incident(request: Request, incident_id: str,
                                  body: IncidentNotifyIn) -> dict:
    """Record the human Art. 33/34 call -- notifiable or documented-why-not.
    Audited either way."""
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    try:
        record = await run_in_threadpool(
            ops.decide_incident_notification,
            incident_id[:64],
            body.notifiable,
            rationale=body.rationale,
            decided_by=_request_actor(request),
        )
    except ops.RecordConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="no such incident")
    return record


@router.post("/privacy/incidents/{incident_id}/close")
async def close_privacy_incident(request: Request,
                                 incident_id: str) -> dict:
    require_permission(request, "operate")
    ops = _privacy_ops_or_403()
    record = await run_in_threadpool(
        ops.close_incident,
        incident_id[:64],
        closed_by=_request_actor(request),
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no such incident")
    return record


@router.get("/assess/templates")
async def list_assessment_templates(request: Request) -> dict:
    """The assessment catalog: every framework the engine can run (PIA, AIRA,
    vendor risk, HIPAA, SOC 2, PCI DSS, ...) with its question count."""
    require_permission(request, "view")
    from maverick.assessment import list_templates
    return {"templates": [
        {"type": t.type, "title": t.title, "framework": t.framework,
         "questions": len(t.questions), "revision": t.revision,
         "digest": t.digest, "custom": t.custom}
        for t in list_templates()
    ]}


@router.get("/assess/template-state/{assessment_type}")
async def get_assessment_template_state(request: Request,
                                        assessment_type: str) -> dict:
    """CAS identity, including a deleted custom-only questionnaire tombstone."""
    require_permission(request, "view")
    from maverick.assessment import template_state
    try:
        return await run_in_threadpool(template_state, assessment_type[:64])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/assess/templates/{assessment_type}")
async def get_assessment_template(request: Request,
                                  assessment_type: str) -> dict:
    """One template in full (questions included) for the catalog editor."""
    require_permission(request, "view")
    from maverick.assessment import get_template, template_department
    tpl = get_template(assessment_type[:64])
    if tpl is None:
        raise HTTPException(status_code=404, detail="no such template")
    return {
        "type": tpl.type, "title": tpl.title, "framework": tpl.framework,
        "description": tpl.description,
        "department": template_department(tpl.type),
        "custom": tpl.custom,
        "revision": tpl.revision,
        "digest": tpl.digest,
        "questions": [{"id": q.id, "section": q.section, "text": q.text,
                       "risk_answer": q.risk_answer, "severity": q.severity,
                       "guidance": q.guidance} for q in tpl.questions],
    }


@router.put("/assess/templates/{assessment_type}")
async def put_assessment_template(request: Request, assessment_type: str,
                                  body: TemplateIn) -> dict:
    """Admin-publish one immutable questionnaire release with pointer CAS."""
    require_permission(request, "admin")
    from maverick.assessment import (
        AssessmentConflict,
        AssessmentStateError,
        save_custom_template,
    )
    values = body.model_dump()
    expected_revision = values.pop("expected_revision")
    expected_digest = values.pop("expected_digest")
    try:
        tpl = await run_in_threadpool(
            save_custom_template,
            {"type": assessment_type[:64], **values},
            expected_revision=expected_revision,
            expected_digest=expected_digest,
            actor=_request_actor(request),
        )
    except AssessmentConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AssessmentStateError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"type": tpl.type, "title": tpl.title,
            "questions": len(tpl.questions), "custom": True,
            "revision": tpl.revision, "digest": tpl.digest}


@router.delete("/assess/templates/{assessment_type}")
async def delete_assessment_template(request: Request,
                                     assessment_type: str,
                                     expected_revision: int,
                                     expected_digest: str) -> dict:
    """Admin-unpublish a custom release; immutable history is retained."""
    require_permission(request, "admin")
    from maverick.assessment import (
        TEMPLATES,
        AssessmentConflict,
        AssessmentStateError,
        delete_custom_template,
        template_state,
    )
    try:
        removed = await run_in_threadpool(
            delete_custom_template, assessment_type[:64],
            expected_revision=expected_revision,
            expected_digest=expected_digest,
            actor=_request_actor(request),
        )
    except AssessmentConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AssessmentStateError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if not removed:
        raise HTTPException(status_code=404, detail="no custom template")
    state = await run_in_threadpool(template_state, assessment_type[:64])
    return {"deleted": assessment_type[:64],
            "builtin_restored": assessment_type[:64] in TEMPLATES,
            "revision": state["revision"], "digest": state["digest"]}


@router.get("/assess/sessions")
async def list_assessment_sessions(request: Request) -> dict:
    """Summaries of every saved third-party assessment session (PIA / AIRA /
    vendor risk...), newest first -- the reviewer's worklist. ``status`` is
    ``pending_review`` or ``needs_more`` (open follow-ups outstanding)."""
    require_permission(request, "operate")
    from maverick.assessment import list_saved
    return {"sessions": await run_in_threadpool(list_saved)}


@router.get("/assess/sessions/{assessment_id}")
async def get_assessment_session(request: Request, assessment_id: str) -> dict:
    """One full assessment record: subject, every answer with the
    respondent's notes, scored findings, rating, and the follow-up thread.
    This is what the review pop-out renders."""
    require_permission(request, "operate")
    from maverick.assessment import load_saved, template_for_saved_record
    record = await run_in_threadpool(load_saved, assessment_id[:64])
    if record is None:
        raise HTTPException(status_code=404, detail="no such assessment")
    # Enrich with the template's question text + section so the pop-out
    # shows real questions, never raw ids like "pia_necessity".
    tpl = template_for_saved_record(record)
    if tpl is not None:
        record["question_text"] = {
            q.id: {"text": q.text, "section": q.section} for q in tpl.questions
        }
    return record


@router.post("/assess/sessions/{assessment_id}/followups")
async def add_assessment_followups(
    request: Request, assessment_id: str, body: FollowupsIn,
) -> dict:
    """Send follow-up questions back to the respondent. The record flips to
    ``needs_more`` until every follow-up is answered; the respondent-facing
    harness (intake portal) picks them up and re-interviews. Audited."""
    require_permission(request, "operate")
    from maverick.assessment import (
        AssessmentConflict,
        AssessmentStateError,
        add_followups,
    )
    who = _request_actor(request)
    try:
        record = await run_in_threadpool(
            add_followups, assessment_id[:64], body.questions, who,
            expected_revision=body.expected_revision,
        )
    except AssessmentConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AssessmentStateError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if record is None:
        raise HTTPException(status_code=404,
                            detail="no such assessment (or empty questions)")
    return record


@router.post("/assess/sessions/{assessment_id}/decide")
async def decide_assessment_session(
    request: Request, assessment_id: str, body: AssessmentDecideIn,
) -> dict:
    """Record the reviewer's decision. Approval with a cadence schedules the
    next review -- the workspace surfaces the record again when it comes due,
    and the follow-up cycle re-opens it. Audited."""
    require_permission(request, "operate")
    from maverick.assessment import (
        AssessmentConflict,
        AssessmentStateError,
        AssessmentTransitionError,
        decide_assessment,
    )
    who = _request_actor(request)
    try:
        record = await run_in_threadpool(
            decide_assessment, assessment_id[:64], body.decision,
            decided_by=who, note=body.note,
            cadence_days=body.cadence_days,
            expected_revision=body.expected_revision,
        )
    except (AssessmentConflict, AssessmentTransitionError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AssessmentStateError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if record is None:
        raise HTTPException(status_code=404, detail="no such assessment")
    return record


@router.post("/assess/sessions/{assessment_id}/assign")
async def assign_assessment_session(
    request: Request, assessment_id: str, body: AssessmentAssignIn,
) -> dict:
    """Put an assessment on a named reviewer's desk (empty name unassigns).

    Everything else on the record is post-hoc attribution -- who decided, who
    answered. This is the only field that says whose job it is NEXT, which is
    what lets a reviewer ask "what is mine?". Routing only: it does not gate
    who may decide. Audited."""
    require_permission(request, "operate")
    from maverick.assessment import (
        AssessmentConflict,
        AssessmentStateError,
        assign_assessment,
    )
    who = _request_actor(request)
    try:
        record = await run_in_threadpool(
            assign_assessment, assessment_id[:64], body.assignee,
            assigned_by=who, expected_revision=body.expected_revision,
        )
    except AssessmentConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AssessmentStateError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if record is None:
        raise HTTPException(status_code=404, detail="no such assessment")
    return record


@router.post("/assess/sessions/{assessment_id}/accept-risk")
async def accept_assessment_risk(
    request: Request, assessment_id: str, body: RiskAcceptIn,
) -> dict:
    """Formally accept the residual risk with a named owner, rationale, and
    expiry. A dated decision, not a permanent waiver -- it comes due again
    when it expires. Audited."""
    require_permission(request, "operate")
    from maverick.assessment import (
        AssessmentConflict,
        AssessmentStateError,
        AssessmentTransitionError,
        accept_risk,
    )
    who = _request_actor(request)
    try:
        record = await run_in_threadpool(
            accept_risk, assessment_id[:64], accepted_by=who,
            rationale=body.rationale, expires_days=body.expires_days,
            expected_revision=body.expected_revision,
        )
    except (AssessmentConflict, AssessmentTransitionError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except AssessmentStateError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if record is None:
        raise HTTPException(status_code=404, detail="no such assessment")
    return record


@router.post("/assess/sessions/{assessment_id}/trigger-review")
async def trigger_assessment_review(
    request: Request, assessment_id: str, body: ReviewTriggerIn,
) -> dict:
    """Force a re-review now (or set a renewal date that flips it due). The
    external-event hook: a contract renewal, a new sub-processor, a material
    change. Audited."""
    require_permission(request, "operate")
    from maverick.assessment import (
        AssessmentConflict,
        AssessmentStateError,
        set_renewal,
        trigger_review,
    )
    who = _request_actor(request)
    try:
        if body.renewal_at is not None:
            record = await run_in_threadpool(
                set_renewal, assessment_id[:64], renewal_at=body.renewal_at,
                by=who, expected_revision=body.expected_revision,
            )
        else:
            record = await run_in_threadpool(
                trigger_review, assessment_id[:64], reason=body.reason,
                by=who, expected_revision=body.expected_revision,
            )
    except AssessmentConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AssessmentStateError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    if record is None:
        raise HTTPException(status_code=404, detail="no such assessment")
    return record


@router.get("/assess/trend")
async def assessment_risk_trend(
    request: Request, subject: str, type: str = "",
) -> dict:
    """A subject's residual-risk history across successive assessments -- the
    re-review delta (risk moving up or down over time), oldest first."""
    require_permission(request, "view")
    from maverick.assessment import risk_trend
    rows = await run_in_threadpool(risk_trend, subject[:300], type[:40])
    direction = ""
    if len(rows) >= 2:
        rank = {"minimal": 0, "low": 1, "medium": 2, "high": 3}
        a = rank.get(rows[-2]["residual_risk"], 0)
        b = rank.get(rows[-1]["residual_risk"], 0)
        direction = "up" if b > a else "down" if b < a else "flat"
    return {"subject": subject, "type": type, "trend": rows,
            "direction": direction}


_BULK_IMPORT_MAX_ROWS = 500


@router.post("/assess/bulk-import")
async def bulk_import_assessments(
    request: Request, body: BulkAssessmentImportIn,
) -> dict:
    """Queue a batch of assessments from a pasted CSV (subject,type[,note]).
    Onboards a whole vendor list without one-by-one intake: each row becomes a
    pending assessment stub ready to be interviewed. Bounded to
    ``_BULK_IMPORT_MAX_ROWS`` per request; emits one summary audit event."""
    require_permission(request, "operate")
    import csv as _csv
    import io

    from maverick.assessment import (
        TEMPLATES,
        AssessmentSession,
        save_session,
    )
    who = _request_actor(request)
    rows = list(_csv.reader(io.StringIO(body.csv)))
    created: list[dict] = []
    errors: list[dict] = []
    skipped_over_cap = 0
    for i, row in enumerate(rows):
        cells = [c.strip() for c in row if c is not None]
        if not cells or not any(cells):
            continue
        subject = cells[0]
        atype = (cells[1].lower() if len(cells) > 1 and cells[1] else "vendor_risk")
        # Tolerate a single header row -- only ever the first line, so a real
        # vendor literally named "Vendor" on a later row is not dropped.
        if i == 0 and subject.lower() in ("subject", "vendor", "name"):
            continue
        # Bound the synchronous per-row work (each save_session is a disk
        # write + memory-plane record); report the overflow rather than
        # silently truncating.
        if len(created) >= _BULK_IMPORT_MAX_ROWS:
            skipped_over_cap += 1
            continue
        if atype not in TEMPLATES:
            errors.append({"row": i + 1, "subject": subject,
                           "error": f"unknown template type '{atype}'"})
            continue
        try:
            session = AssessmentSession(type=atype, subject=subject[:300])
            await run_in_threadpool(save_session, session)
            created.append({"id": session.id, "subject": subject,
                            "type": atype})
        except Exception as e:  # noqa: BLE001 -- report the row, keep going
            errors.append({"row": i + 1, "subject": subject, "error": str(e)})
    try:  # best-effort: the import already committed each session atomically
        from maverick.audit import record as _audit_record
        _audit_record("ASSESSMENTS_BULK_IMPORTED", agent=who,
                      created=len(created), errors=len(errors))
    except Exception:  # pragma: no cover -- audit write never blocks the import
        pass
    return {"created": created, "errors": errors,
            "created_count": len(created), "error_count": len(errors),
            "skipped_over_cap": skipped_over_cap}


@router.get("/assessment-memory/similar")
async def assessment_memory_similar(
    request: Request, subject: str, type: str = "", k: int = 3,
) -> dict:
    """What the org already knows about subjects like this one: similar past
    assessments (with ratings), advisory per-question answer suggestions from
    the same template's history, and semantic lessons when the knowledge
    plane is on. Advisory only -- nothing here answers an assessment.
    Operate-gated: suggestions carry prior ANSWER content and reviewer notes,
    a tier above the summary rows the assessments register shows viewers."""
    require_permission(request, "operate")
    from maverick import assessment_memory
    subject = subject.strip()[:300]
    k = max(1, min(k, 10))
    if not subject:
        raise HTTPException(status_code=422, detail="subject is required")
    sim = await run_in_threadpool(
        assessment_memory.similar, subject,
        assessment_type=(type or None), k=k)
    suggestions = {}
    if type:
        suggestions = await run_in_threadpool(
            assessment_memory.suggest_answers, type, subject, k=k)
    lessons = await run_in_threadpool(assessment_memory.lessons, subject, k=k)
    return {"enabled": assessment_memory.enabled(), "similar": sim,
            "suggestions": suggestions, "lessons": lessons}


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

    Same access rules as the list; the bytes come off the local store (with
    an S3 pull-back for multi-host deployments). Served as a download with
    the ORIGINAL filename — never inline, so a crafted HTML/SVG upload can't
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
        # Uploaded on another worker host: try the S3 mirror.
        from maverick.attachments import s3_fetch
        fetched = await run_in_threadpool(s3_fetch, goal_id, path.name)
        if fetched is None:
            raise HTTPException(
                status_code=410, detail="attachment bytes not on this host")
        path = fetched
    safe_name = match.filename.replace('"', "_")
    return FileResponse(
        path,
        media_type=match.mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/facts", response_model=dict[str, str])
async def list_facts() -> dict[str, str]:
    return _world().get_facts()


@router.post("/facts", status_code=204)
async def set_fact(request: Request, payload: FactIn) -> None:
    require_permission(request, "operate")
    key = (payload.key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="fact key is required")
    _world().upsert_fact(key, payload.value)


@router.post("/outcomes", status_code=204)
async def record_outcome(request: Request, payload: OutcomeIn) -> None:
    """Ingest a real downstream outcome for a past episode (the grounded reward).

    The HTTP entrypoint a system-of-record connector (CRM / ERP / ticketing
    webhook) calls once reality reports back -- invoice paid (1.0), ticket
    reopened (0.0), or a graded result. The Cognitive Data Engine flywheel then
    prefers this over the verifier proxy on its next turn, so learning is grounded
    in what actually happened. ``value`` is clamped to [0, 1] by the store.
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


@router.post("/outcomes/link", status_code=204)
async def link_outcome(request: Request, payload: OutcomeLinkIn) -> None:
    """Link an external business key to the episode that acted on it.

    The registration half of the system-of-record loop: when a run creates an
    invoice / opens a ticket, this records ``invoice:INV-42 -> (goal, episode)``
    so a later outcome reported against that key (see ``/outcomes/by-key``) can be
    grounded without the reporter knowing any episode id. Always harmless to store.
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
    from maverick.consequence import link_outcome_key
    link_outcome_key(payload.key, goal_id, episode_id)


@router.post("/outcomes/by-key")
async def record_outcome_by_key(request: Request, payload: OutcomeByKeyIn) -> dict:
    """Ingest a real downstream outcome using only the business key.

    The endpoint a system-of-record webhook (Stripe "invoice.paid", Zendesk
    "ticket.reopened") calls: it knows its own key, not a Lightwork episode. We
    resolve the key to the episode that acted (registered via ``/outcomes/link``)
    and ground the reward there. ``matched`` is ``false`` when no run has linked
    that key -- the connector reported an outcome for work Lightwork didn't do.
    """
    require_permission(request, "operate")
    from maverick import consequence
    w = _world()

    def _authorize(goal_id: int, _episode_id: int) -> bool:
        # Enforce the same access boundary as the goal/episode ingest so an
        # operate caller can't ground outcomes on a goal they can't see (the
        # correlation store is already tenant-scoped by path).
        g = w.get_goal(goal_id)
        return g is not None and can_access_goal(request, g)

    matched = consequence.record_outcome_for_key(
        payload.key, float(payload.value), kind=(payload.kind or ""),
        authorize=_authorize)
    return {"matched": bool(matched)}


# ---- turnkey "connect this account" OAuth flow --------------------------------
#
# Connect a SaaS account by NAME using a provider preset, so an OAuth'd event
# source (oauth_http_json / github_issues with provider=<name>) can poll it.
# Authorizing a token is the same trust as sending it, so all mutating steps are
# ADMIN-gated (matching the provider-trigger admin gate). The client secret is
# read from the operator's env, never posted here or persisted.


def _require_oauth_vault() -> None:
    from maverick import oauth_vault
    if not oauth_vault.enabled():
        raise HTTPException(
            status_code=403,
            detail=("Account connections aren't enabled yet — an administrator "
                    "must turn on the OAuth vault first (enable [oauth] vault "
                    "in the server configuration, or set MAVERICK_OAUTH_VAULT=1)."),
        )


@router.get("/oauth/providers")
async def oauth_providers_endpoint(request: Request) -> dict:
    """The connectable provider presets + which are already connected (view)."""
    require_permission(request, "view")
    from maverick import oauth_providers, oauth_vault
    connected: list[str] = []
    status: dict[str, dict] = {}
    if oauth_vault.enabled():
        try:
            vault = oauth_vault.get_vault()
            connected = vault.providers()
            # Token-free per-provider health so the UI can flag an expired
            # connection without a live network call.
            for name in connected:
                st = vault.status(name)
                if st is not None:
                    status[name] = st
        except Exception:  # pragma: no cover -- a vault read never errors the list
            connected = []
            status = {}
    return {
        "providers": oauth_providers.provider_catalog(),
        "connected": connected,
        "status": status,
        "vault_enabled": oauth_vault.enabled(),
    }


@router.post("/oauth/{provider}/authorize-url")
async def oauth_authorize_url(request: Request, provider: str,
                              payload: OAuthAuthorizeIn) -> dict:
    """Build the consent URL for a preset provider (admin). Returns the URL to
    open and the PKCE verifier to keep -- pass the verifier back to /exchange.
    Stateless: the verifier round-trips through the (trusted) operator."""
    require_permission(request, "admin")
    _require_oauth_vault()
    from maverick import oauth_providers
    built = oauth_providers.build_authorize_url(
        provider, redirect_uri=payload.redirect_uri,
        client_id=payload.client_id or "", scopes=payload.scopes)
    if built is None:
        prov = oauth_providers.get_provider(provider)
        if prov is None:
            raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
        raise HTTPException(
            status_code=400,
            detail=f"set {prov.default_client_id_env()} (the OAuth app client id) first")
    url, verifier = built
    return {"authorize_url": url, "verifier": verifier}


@router.post("/oauth/{provider}/exchange")
async def oauth_exchange(request: Request, provider: str,
                         payload: OAuthExchangeIn) -> dict:
    """Swap the authorization code for tokens and seal them in the per-tenant
    vault under the provider name (admin). The provider preset supplies the token
    endpoint; the client secret is read from the operator's env."""
    require_permission(request, "admin")
    _require_oauth_vault()
    from maverick import oauth_providers
    if oauth_providers.get_provider(provider) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    try:
        summary = oauth_providers.exchange_code(
            provider, code=payload.code, redirect_uri=payload.redirect_uri,
            verifier=payload.verifier or "", client_id=payload.client_id or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # a provider/network failure -> 502, not a 500
        raise HTTPException(status_code=502, detail=f"token exchange failed: {e}") from e
    return {"connected": True, **summary}


@router.post("/oauth/{provider}/test")
async def oauth_test(request: Request, provider: str) -> dict:
    """Validate a connected account (admin): obtain a live access token from the
    sealed vault, refreshing it through the preset's token endpoint if it's
    expired. Reports health only -- never the token itself. ``ok`` is true when
    a usable token could be produced, false (with a redacted ``error``) when the
    refresh failed, so an operator sees a stale connection before a flow does."""
    require_permission(request, "admin")
    _require_oauth_vault()
    from maverick import oauth_providers, oauth_vault
    if oauth_providers.get_provider(provider) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    vault = oauth_vault.get_vault()
    if vault.status(provider) is None:
        raise HTTPException(status_code=404, detail=f"{provider!r} is not connected")
    refresher = oauth_providers.make_refresher(provider)
    try:
        token = vault.access_token(provider, refresher=refresher)
    except Exception as e:  # a provider/network failure -> reported, not a 500
        return {"ok": False, "error": str(e), "status": vault.status(provider)}
    return {"ok": bool(token), "status": vault.status(provider)}


@router.delete("/oauth/{provider}", status_code=204)
async def oauth_disconnect(request: Request, provider: str) -> None:
    """Remove a connected account's sealed token from the vault (admin)."""
    require_permission(request, "admin")
    _require_oauth_vault()
    from maverick import oauth_vault
    oauth_vault.get_vault().delete(provider)


# ---- connections: named, sealed SaaS credentials -----------------------------


def _require_connections() -> None:
    from maverick import connections
    if not connections.enabled():
        raise HTTPException(
            status_code=403,
            detail=("Connections aren't enabled on this server yet — an "
                    "administrator can switch them on (enable [connections] in "
                    "the server configuration, or set MAVERICK_CONNECTIONS=1)."),
        )


@router.get("/connections")
async def list_connections_endpoint(request: Request) -> dict:
    """Named connections the caller may see (tokens never returned -- only
    ``has_token``). Owner-scoped when auth is on."""
    require_permission(request, "operate")
    _require_connections()
    from maverick import connections
    return {"connections": connections.list_connections(owner=_request_identity(request))}


@router.post("/connections", status_code=201)
async def create_connection_endpoint(request: Request, payload: ConnectionIn) -> dict:
    """Create or replace a named connection (base URL + token, sealed at rest).
    A connector can then be wired without env vars -- the connection is the
    fallback when ``<NAME>_TOKEN`` is unset. Admin-gated: a saved connection
    holds a live credential, so only administrators may create/rotate one."""
    require_permission(request, "admin")
    _require_connections()
    from maverick import connections
    try:
        # Owner-scope like triggers: a non-admin can't overwrite another
        # tenant's connection by reusing its name. Enforced INSIDE the store
        # lock (expected_owner) so the check and the write are atomic.
        owner = _request_identity(request)
        rec = connections.set_connection(
            payload.name, connector=payload.connector, base_url=payload.base_url,
            token=payload.token, owner=owner or "",
            expected_owner=owner, access=payload.access,
            allowed_principals=payload.allowed_principals)
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return rec


@router.post("/connections/{name}/test")
async def test_connection_endpoint(request: Request, name: str) -> dict:
    """Best-effort reachability check: GET the connection's base URL with its
    token and report whether it authenticates. Never returns the token.
    Admin-gated: the probe sends the sealed token, so it's part of managing the
    credential, not an operator action."""
    require_permission(request, "admin")
    _require_connections()
    from maverick import connections
    rec = connections.get_connection(name)
    # Owner-scope like list/delete/create: a non-admin can only test their own
    # connection (else a name-guess is a cross-owner reachability oracle AND
    # sends another owner's token). A 404 (not 403) so it can't confirm the name.
    owner_filter = _request_identity(request)
    if rec is None or (owner_filter is not None and rec.get("owner", "") != owner_filter):
        raise HTTPException(status_code=404, detail="no such connection")
    probe_revision = str(rec.get("revision") or "")

    def _persist_probe(result: dict) -> None:
        try:
            connections.record_test_result(
                name,
                reachable=bool(result.get("reachable")),
                authenticated=bool(result.get("authenticated")),
                status=result.get("status"),
                expected_owner=owner_filter,
                expected_revision=probe_revision,
            )
        except connections.ConnectionVersionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="connection changed while its readiness probe was running",
            ) from exc

    base = str(rec.get("base_url") or "").rstrip("/")
    if not base:
        result = {"ok": False, "reachable": False, "authenticated": False,
                  "detail": "no base URL set"}
        _persist_probe(result)
        return result

    def _probe() -> dict:
        import secrets

        from maverick.tools import _ssrf

        try:
            from maverick.tools.enterprise_connectors import auth_headers_for
            # Speak the connector's saved-token auth dialect (basic / custom
            # scheme), but do not include operator environment-backed auxiliary
            # headers: saved connection tests use a caller-controlled base_url.
            headers = auth_headers_for(
                str(rec.get("connector") or ""), str(rec.get("token") or ""),
                include_extra_headers_env=False,
            )
            # This request carries the sealed token, so it MUST go through the
            # governed SSRF-safe path: safe_get pins the host to a validated
            # PUBLIC ip (blocks 169.254/127.0.0.1/private/link-local) and forces
            # follow_redirects=False, so a base_url or a 3xx can't turn this into
            # an internal-network probe or forward the token to another host.
            r = _ssrf.safe_get(base, headers=headers, timeout=10.0)
            # Any HTTP status proves network reachability. It does NOT prove
            # authentication/readiness: 401/403 are explicit auth failures and
            # a generic 404/4xx/5xx cannot substantiate a successful credential
            # test. Keep these concepts separate for authoring preflight.
            # A redirect commonly points to an interactive login page; it proves
            # reachability, not that this credential authenticated. A 2xx alone
            # is also insufficient because many API roots are public. Challenge
            # the same endpoint with a fresh invalid credential and require an
            # explicit 401/403 control response before asserting that the saved
            # token made the difference. No response body or header is retained.
            actual_ok = 200 <= r.status_code < 300
            ready = False
            if actual_ok and rec.get("token"):
                invalid_headers = auth_headers_for(
                    str(rec.get("connector") or ""),
                    "invalid-readiness-probe-" + secrets.token_urlsafe(32),
                    include_extra_headers_env=False,
                )
                try:
                    control = _ssrf.safe_get(
                        base, headers=invalid_headers, timeout=10.0,
                    )
                except Exception as exc:  # token worked; control evidence did not
                    return {
                        "ok": False,
                        "reachable": True,
                        "authenticated": False,
                        "status": r.status_code,
                        "detail": (
                            "reachable, but the invalid-credential control could "
                            f"not be completed ({type(exc).__name__})"
                        ),
                    }
                ready = control.status_code in (401, 403)
            if r.status_code in (401, 403):
                detail = f"authentication refused ({r.status_code})"
            elif ready:
                detail = "authenticated (invalid-credential control was refused)"
            elif actual_ok:
                detail = (
                    "reachable, but the endpoint did not prove that the saved "
                    "credential was required"
                )
            else:
                detail = f"reachable but readiness probe returned {r.status_code}"
            return {"ok": ready, "reachable": True, "authenticated": ready,
                    "status": r.status_code, "detail": detail}
        except _ssrf.BlockedHost:
            return {"ok": False, "reachable": False, "authenticated": False,
                    "detail": "base URL host is not permitted"}
        except Exception as e:  # pragma: no cover -- network varies
            return {"ok": False, "reachable": False, "authenticated": False,
                    "detail": f"could not reach host: {type(e).__name__}"}
    result = await run_in_threadpool(_probe)
    _persist_probe(result)
    return result


@router.delete("/connections/{name}")
async def delete_connection_endpoint(request: Request, name: str) -> dict:
    require_permission(request, "admin")  # managing credentials is admin-only
    _require_connections()
    from maverick import connections
    if not connections.delete_connection(name, owner=_request_identity(request)):
        raise HTTPException(status_code=404, detail="no connection with that name")
    return {"deleted": connections.normalize_name(name)}


# ---- governance assessments: privacy / security / AI-risk register ------------
# Reading the register is audit-level; drafting and reviewing (which record a
# governance decision) require admin. Auto-draft heuristics live in
# maverick.assessments; this is thin plumbing over that engine.

def _assess_kind(kind: str) -> str:
    if kind not in ("agent", "flow"):
        raise HTTPException(status_code=400, detail="kind must be 'agent' or 'flow'")
    return kind


@router.get("/assessments")
async def list_assessments_endpoint(request: Request) -> dict:
    require_permission(request, "audit")
    from maverick import assessments
    return {"assessments": assessments.list_assessments(),
            "due": assessments.due_assessments(),
            "templates": assessments.list_templates()}


@router.get("/assessments/export")
async def export_assessments_endpoint(request: Request, format: str = "json") -> Response:
    """Audit export of every finding. ``?format=csv`` for a spreadsheet."""
    require_permission(request, "audit")
    from maverick import assessments
    rows = assessments.export_rows()
    if format == "csv":
        import csv
        import io
        cols = ["kind", "subject", "assessment_status", "lens", "lens_status",
                "control", "severity", "finding_status", "note", "mitigation",
                "evidence", "reviewer", "reviewed_at"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({col: _csv_formula_safe(r.get(col, "")) for col in cols})
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=assessments.csv"})
    return JSONResponse({"rows": rows})


@router.post("/assessments/sweep")
async def sweep_assessments_endpoint(request: Request) -> dict:
    """Re-assess every subject now (catch drift) and return what needs attention.
    The same sweep the automation scheduler runs daily, on demand."""
    require_permission(request, "admin")
    from maverick import assessments
    return assessments.sweep_due()


@router.get("/assessments/{kind}/{name}")
async def get_assessment_endpoint(request: Request, kind: str, name: str) -> dict:
    require_permission(request, "audit")
    from maverick import assessments
    a = assessments.get_assessment(_assess_kind(kind), name)
    if a is None:
        raise HTTPException(status_code=404, detail="no assessment for that subject")
    return a


@router.post("/assessments/{kind}/{name}/refresh")
async def refresh_assessment_endpoint(
    request: Request, kind: str, name: str, payload: AssessmentRefreshIn
) -> dict:
    """Auto-draft (or re-draft, detecting drift) the assessment for a subject."""
    require_permission(request, "admin")
    from maverick import assessments
    a = assessments.refresh(_assess_kind(kind), name, template=payload.template)
    if a is None:
        raise HTTPException(status_code=404, detail=f"unknown {kind}: {name!r}")
    return a


@router.post("/assessments/{kind}/{name}/review")
async def review_assessment_endpoint(
    request: Request, kind: str, name: str, payload: AssessmentReviewIn
) -> dict:
    """Record a reviewer's accept / needs-work decision on one lens."""
    require_permission(request, "admin")
    from maverick import assessments
    try:
        a = assessments.record_review(
            _assess_kind(kind), name, payload.lens, payload.decision,
            reviewer=caller_principal(request) or "", note=payload.note or "",
            cadence_days=payload.cadence_days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if a is None:
        raise HTTPException(status_code=404, detail="no assessment for that subject")
    return a


@router.post("/assessments/{kind}/{name}/evidence")
async def add_assessment_evidence_endpoint(
    request: Request, kind: str, name: str, payload: AssessmentEvidenceIn
) -> dict:
    require_permission(request, "admin")
    from maverick import assessments
    a = assessments.add_evidence(_assess_kind(kind), name, payload.lens,
                                 payload.control, payload.evidence)
    if a is None:
        raise HTTPException(status_code=404, detail="no such finding")
    return a


@router.delete("/assessments/{kind}/{name}", status_code=204)
async def delete_assessment_endpoint(request: Request, kind: str, name: str) -> Response:
    require_permission(request, "admin")
    from maverick import assessments
    assessments.delete_assessment(_assess_kind(kind), name)
    return Response(status_code=204)


# ---- flows: a deterministic skeleton over agentic + tool steps ---------------


def _require_flows() -> None:
    from maverick import flow
    if not flow.enabled():
        raise HTTPException(
            status_code=403,
            detail=("Flows aren't enabled on this server yet — an administrator "
                    "can switch on the flow engine (enable [flows] in the "
                    "server configuration, or set MAVERICK_FLOWS=1)."),
        )


def _can_access_flow(request: Request, flow) -> bool:
    """Whether the caller may read or mutate a saved flow definition.

    Auth-off and dashboard admins keep the historical unscoped behavior.
    Authenticated non-admins may only access flows they own; legacy ownerless
    flows remain reachable only from the auth-off/admin paths.
    """
    principal = caller_principal(request)
    if principal is None or is_dashboard_admin(principal):
        return True
    return getattr(flow, "owner", "") == principal


def _assert_flow_access(request: Request, flow) -> None:
    if not _can_access_flow(request, flow):
        raise HTTPException(status_code=404, detail="no such flow")


def _flow_publication_fields(flow_id: str) -> dict:
    """Small activation-state projection for authoring/list responses."""
    from maverick.flow import store

    try:
        published = store.load_published_bundle(flow_id)
    except store.FlowSnapshotError as exc:
        raise HTTPException(
            status_code=409, detail="published flow release failed integrity checks"
        ) from exc
    if published is None:
        return {
            "published": False,
            "published_version": None,
            "published_revision": "",
        }
    _flow, release = published
    return {
        "published": True,
        "published_version": int(release["definition_version"]),
        "published_revision": str(release["release_id"]),
    }


@router.get("/flows")
async def list_flows_endpoint(request: Request) -> dict:
    """The saved flow definitions (a summary per flow)."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import store
    return {"flows": [
        {"id": f.id, "name": f.name, "nodes": len(f.nodes),
         "single_agent": f.is_single_agent(), "schedule": f.schedule,
         "inputs": f.inputs, **_flow_publication_fields(f.id)}
        for f in store.list_flows()
        if _can_access_flow(request, f)
    ]}


@router.post("/flows", status_code=201)
async def save_flow_endpoint(request: Request, payload: FlowSaveIn) -> dict:
    """Create or replace a flow definition (validated before it is stored)."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import Flow, store
    try:
        flow = Flow.from_dict(payload.flow)
    except Exception as e:  # a malformed graph
        raise HTTPException(status_code=400, detail=f"invalid flow: {e}") from e
    if not flow.id:
        raise HTTPException(status_code=400, detail="flow needs an 'id'")
    errs = flow.validate()
    if errs:
        raise HTTPException(status_code=400, detail="invalid flow: " + "; ".join(errs[:5]))
    # Stamp ownership: the creator on first save, preserved across later edits so a
    # re-save can't silently transfer ownership. Scheduled (cron) runs attribute to
    # this owner (they have no request principal of their own).
    prior = store.load_flow(flow.id)
    if prior is not None:
        _assert_flow_access(request, prior)
        # Updates are compare-and-swap against what the editor actually loaded,
        # not a fresh server read performed moments before the write. Requiring
        # both the monotonic version and non-reused generation token closes stale
        # tab overwrites and delete+recreate ABA/ownership races.
        if "version" not in payload.flow or "revision" not in payload.flow:
            raise HTTPException(
                status_code=409,
                detail="flow changed or this editor is stale; reload before saving",
            )
    flow.owner = prior.owner if (prior and prior.owner) else (caller_principal(request) or "")
    try:
        store.save_flow(
            flow,
            expected_version=(payload.flow.get("version") if prior is not None else 0),
            expected_revision=(payload.flow.get("revision") if prior is not None else ""),
        )
    except store.FlowVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": flow.id,
        "nodes": len(flow.nodes),
        "version": flow.version,
        "revision": flow.revision,
        **_flow_publication_fields(flow.id),
    }


@router.post("/flows/{flow_id}/publish")
async def publish_flow_endpoint(
    request: Request, flow_id: str, payload: FlowPublishIn,
) -> dict:
    """Activate one immutable draft revision for real runs/triggers/schedules."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import store

    current = store.load_flow(flow_id)
    if current is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, current)
    try:
        release = store.publish_flow(
            flow_id,
            expected_version=payload.version,
            expected_revision=payload.revision,
        )
    except store.FlowVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except store.FlowSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Make schedule activation responsive to an explicit publish. The periodic
    # reconcile remains the crash/restart repair path.
    from maverick_dashboard import automation_queue

    try:
        automation_queue._reconcile_flow_crons()
    except Exception:
        # Publication already committed. Report that authoritative state and
        # let the periodic reconciler repair scheduling instead of returning a
        # false failure that might induce a duplicate operator retry.
        log.exception("flow cron reconcile failed after publishing %s", flow_id)
    return {
        "id": flow_id,
        "published": True,
        "published_version": int(release["definition_version"]),
        "published_revision": str(release["release_id"]),
    }


@router.post("/flows/{flow_id}/unpublish")
async def unpublish_flow_endpoint(
    request: Request, flow_id: str, payload: FlowPublishIn,
) -> dict:
    """Deactivate a release while retaining its draft and immutable history."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import store

    current = store.load_flow(flow_id)
    if current is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, current)
    try:
        store.unpublish_flow(
            flow_id,
            expected_version=payload.version,
            expected_revision=payload.revision,
        )
    except store.FlowVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from maverick_dashboard import automation_queue

    try:
        automation_queue._reconcile_flow_crons()
    except Exception:
        # The handler re-checks publication before firing, so a stale queued
        # cron is inert while the periodic reconciler repairs cancellation.
        log.exception("flow cron reconcile failed after unpublishing %s", flow_id)
    return {"id": flow_id, "published": False}


# Common connector tools to bias the NL drafter toward real action nodes; the
# operator edits the result in the designer, and unknown tools fail gracefully.
_FLOW_DRAFT_TOOLS = (
    "slack_bot", "discord_bot", "teams", "email", "gmail", "twilio",
    "github_issues", "github_actions", "jira", "linear", "gitlab_issues",
    "salesforce", "hubspot", "stripe", "shopify", "servicenow", "zendesk",
    "notion", "confluence", "trello", "calendar", "dropbox", "s3", "http_fetch",
)


_TOOL_INDEX_CACHE: list[dict] | None = None
_TOOL_SCHEMA_CACHE: dict[str, dict] | None = None
_TOOL_CATALOG_CACHE: dict[str, tuple[float, list[dict], dict[str, dict]]] = {}
_TOOL_CATALOG_TTL_SECONDS = 30.0
_TOOL_INDEX_LOCK = threading.Lock()


def _tool_catalog_scope(
    *, channel: str | None = None, user_id: str | None = None,
) -> str:
    """Tenant/home + execution-identity namespace for authoring metadata."""
    from maverick.paths import data_dir
    return "\0".join((
        str(data_dir().resolve(strict=False)),
        str(channel or ""),
        str(user_id or ""),
    ))


def _live_tool_index(
    *, channel: str | None = None, user_id: str | None = None,
) -> list[dict]:
    """Tenant-scoped, short-lived flattened registry metadata.

    Connector config, learned skills, and ACL-derived registries can differ by
    tenant and can change while the dashboard remains up. A process-global
    forever cache leaks one tenant's tool names/contracts to another and makes
    newly learned capabilities invisible. The brief TTL still avoids rebuilding
    hundreds of tool objects for every debounced search request.

    ``_TOOL_INDEX_CACHE`` remains an explicit test/embedding override; normal
    production requests use ``_TOOL_CATALOG_CACHE``.
    """
    if _TOOL_INDEX_CACHE is not None:
        return _TOOL_INDEX_CACHE
    scope = _tool_catalog_scope(channel=channel, user_id=user_id)
    now = time.monotonic()
    with _TOOL_INDEX_LOCK:
        cached = _TOOL_CATALOG_CACHE.get(scope)
        if cached is not None and now - cached[0] <= _TOOL_CATALOG_TTL_SECONDS:
            return cached[1]
    try:
        from maverick.sandbox import build_sandbox
        from maverick.tools import base_registry
        live = base_registry(
            _world(),
            build_sandbox(),
            goal_id=None,
            channel=channel,
            user_id=user_id,
        ).all()
    except Exception:  # pragma: no cover -- registry build varies by env
        live = []
    from maverick.flow.ir import action_tool_policy_error

    from .tool_categories import categorize

    idx = []
    schemas: dict[str, dict] = {}
    for t in live:
        if action_tool_policy_error(t.name):
            continue
        params = []
        if isinstance(t.input_schema, dict):
            params = list((t.input_schema.get("properties") or {}).keys())[:12]
            schemas[t.name] = dict(t.input_schema)
        desc = (t.description or "")[:200]
        idx.append({"name": t.name, "description": desc, "params": params,
                    "category": categorize(t.name, desc)})
    with _TOOL_INDEX_LOCK:
        _TOOL_CATALOG_CACHE[scope] = (time.monotonic(), idx, schemas)
    return idx


def _live_tool_schemas(
    *, channel: str | None = None, user_id: str | None = None,
) -> dict[str, dict]:
    """Full input contracts, kept out of the public picker response."""
    if _TOOL_SCHEMA_CACHE is not None:
        return _TOOL_SCHEMA_CACHE
    scope = _tool_catalog_scope(channel=channel, user_id=user_id)
    _live_tool_index(channel=channel, user_id=user_id)
    with _TOOL_INDEX_LOCK:
        cached = _TOOL_CATALOG_CACHE.get(scope)
        return cached[2] if cached is not None else {}


def _flow_tool_catalog(
    *, channel: str | None = None, user_id: str | None = None,
) -> list[dict]:
    """The curated connector catalog for the designer's action picker + the NL
    drafter's grounding: each tool's name, one-line description, and param names
    (from its input schema). Best-effort -- a tool absent from the live registry
    still appears by name so the picker/drafter can offer it."""
    by_name = {
        t["name"]: t
        for t in _live_tool_index(channel=channel, user_id=user_id)
    }
    out = []
    for name in _FLOW_DRAFT_TOOLS:
        t = by_name.get(name)
        if t is not None:
            out.append(dict(t))
    return out


def _flow_authoring_catalog(
    query: str,
    *,
    include_tools=(),
    channel: str | None = None,
    user_id: str | None = None,
) -> list[dict]:
    """Intent-ranked tools from the FULL live registry for one draft/chat turn.

    Existing action tools are appended only when they still exist in the live
    registry, so a harmless edit can retain a valid current binding while a
    stale/hallucinated legacy action remains fail-closed.
    """
    from maverick.flow.draft import rank_tool_catalog
    live = _live_tool_index(channel=channel, user_id=user_id)
    ranked = rank_tool_catalog(query, live, limit=24)
    by_name = {t["name"]: t for t in live}
    present = {t["name"] for t in ranked}
    for name in sorted({str(n) for n in include_tools if str(n)}):
        item = by_name.get(name)
        if item is not None and name not in present:
            ranked.append(item)
            present.add(name)
    schemas = _live_tool_schemas(channel=channel, user_id=user_id)
    return [
        {**item, "input_schema": dict(schemas.get(item["name"]) or {})}
        for item in ranked
    ]


@router.post("/flows/draft")
async def draft_flow_endpoint(request: Request, payload: FlowDraftIn) -> dict:
    """Draft a first-pass flow from a plain-English description (the designer's
    ✨ button). Returns the flow graph (not saved) + any notes; falls back to a
    single agent step if the model can't produce a full graph. The drafter is
    grounded with each connector's description so action nodes name a real tool."""
    require_permission(request, "operate")
    _require_flows()
    from maverick_dashboard._shared import require_provider_or_400
    require_provider_or_400(role="orchestrator")
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request, source="flow-authoring")
    from maverick.flow.draft import draft_flow
    user_id = execution_user_id_from_request(request)
    channel = "api" if user_id else None

    def _draft():
        catalog = _flow_authoring_catalog(
            payload.description, channel=channel, user_id=user_id)
        tool_docs = {t["name"]: t["description"] for t in catalog if t["description"]}
        tool_schemas = {t["name"]: t["input_schema"] for t in catalog}
        return draft_flow(
            payload.description,
            tools=tuple(t["name"] for t in catalog),
            tool_docs=tool_docs,
            tool_schemas=tool_schemas,
            flow_id=payload.flow_id,
        )
    flow, notes = await run_in_threadpool(_draft)
    return {"flow": flow.to_dict(), "notes": notes}


@router.post("/flows/chat")
async def flow_chat_endpoint(request: Request, payload: FlowChatIn) -> dict:
    """One turn of the designer's flow copilot: answer a question about the
    current canvas, or edit it via validated patches (never a whole-graph
    regeneration on a populated canvas). ``run_id`` grounds "why did this
    fail?" / "fix it" turns in that run's real per-node trace. The patched
    graph is returned to the canvas; nothing is saved until the user saves."""
    require_permission(request, "operate")
    _require_flows()
    from maverick_dashboard._shared import require_provider_or_400
    require_provider_or_400(role="orchestrator")
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request, source="flow-authoring")
    from maverick.flow import Flow
    from maverick.flow.chat import chat_flow
    from maverick.flow.draft import action_tool_names
    try:
        current = Flow.from_dict(payload.flow) if payload.flow else None
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid flow: {e}") from e
    run = _run_out(_load_owned_run(request, payload.run_id)) if payload.run_id else None
    user_id = execution_user_id_from_request(request)
    channel = "api" if user_id else None

    def _turn():
        catalog = _flow_authoring_catalog(
            payload.message,
            include_tools=action_tool_names(current) if current is not None else (),
            channel=channel,
            user_id=user_id,
        )
        tool_docs = {t["name"]: t["description"] for t in catalog if t["description"]}
        tool_schemas = {t["name"]: t["input_schema"] for t in catalog}
        return chat_flow(payload.message, flow=current, history=payload.history,
                         run=run, tools=tuple(t["name"] for t in catalog),
                         tool_docs=tool_docs, tool_schemas=tool_schemas)
    res = await run_in_threadpool(_turn)
    return {
        "reply": res.reply,
        "flow": res.flow.to_dict() if res.flow is not None else None,
        "applied": res.applied,
        "notes": res.notes,
    }


@router.get("/flows/runs")
async def list_flow_runs_endpoint(request: Request, flow_id: str = "",
                                  limit: int = 50) -> dict:
    """Recent flow runs (owner-scoped). Defined before ``/flows/{flow_id}`` so the
    literal ``runs`` path isn't captured as a flow id."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import store
    owner = goal_owner_filter(request)
    runs = store.list_runs(flow_id=flow_id or None, owner=owner,
                           limit=max(1, min(500, limit)))
    return {"runs": [_run_out(r) for r in runs]}


@router.get("/flows/{flow_id}/schema")
async def flow_schema_endpoint(request: Request, flow_id: str) -> dict:
    """The learned output-key shapes for a flow (from real runs): per key, its
    value type and, for objects/arrays-of-objects, the field names -- so the
    designer's data pills can offer nested keys like ``order.total``. Field
    names + types only; no values are ever stored or returned."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import store
    flow = store.load_flow(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, flow)
    return {"schema": store.load_flow_schema(flow_id)}


def _run_out(run) -> dict:
    """A flow run for the API with secret-looking values in its payloads masked,
    so a token a connector put into the run never leaves over HTTP."""
    from maverick.flow.redact import redact
    d = run.to_dict()
    d["data"] = redact(d.get("data") or {})
    d["input_data"] = redact(d.get("input_data") or {})
    # Execution snapshots are private durability state, not an API surface.
    # Definitions may contain literal connector parameters from older/user-made
    # flows, so never echo the pinned graph through run polling or SSE.
    d.pop("definition_digest", None)
    d.pop("release_digest", None)
    d.pop("release_id", None)
    d.pop("definition_version", None)
    d.pop("subflow_digests", None)
    return d


def _load_owned_run(request: Request, run_id: str):
    """A flow run the caller may see, or a 404 (indistinguishable from missing,
    so a run's existence isn't leaked across owners)."""
    from maverick.flow import store
    run = store.load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    owner = goal_owner_filter(request)
    if owner is not None and run.owner != owner:
        raise HTTPException(status_code=404, detail="no such run")
    return run


@router.get("/flows/runs/{run_id}")
async def get_flow_run_endpoint(request: Request, run_id: str) -> dict:
    require_permission(request, "operate")
    _require_flows()
    return _run_out(_load_owned_run(request, run_id))


@router.get("/flows/runs/{run_id}/events")
async def flow_run_events_stream(request: Request, run_id: str) -> StreamingResponse:
    """Server-Sent Events stream of a flow run's live state: one snapshot per
    change (keyed on the run's ``updated`` stamp), ending with a terminal event
    when the run completes/fails/rejects or parks on a human/event pause. The
    canvas overlay and the run viewer prefer this over polling; both fall back
    to polling when SSE is unavailable. Same slot cap + lifetime bound as the
    goal-events stream, so open tabs can't exhaust the event loop."""
    import asyncio as _asyncio
    import json as _json

    require_permission(request, "operate")
    _require_flows()
    _load_owned_run(request, run_id)     # 404 before holding a stream slot
    from maverick.flow import store as flow_store

    from maverick_dashboard._shared import _get_sse_semaphore
    sem = _get_sse_semaphore()
    if sem.locked():
        raise HTTPException(status_code=503,
                            detail="too many concurrent event streams; retry shortly",
                            headers={"Retry-After": "5"})
    await sem.acquire()

    # completed/failed/rejected are final; a run parked on a human or an
    # external event won't change until someone acts -- close the stream and
    # let the page's actions (approve / resume) drive the next one.
    end_states = (
        "completed", "failed", "rejected", "indeterminate",
        "paused_approval", "paused_event",
    )
    poll, max_poll, max_seconds, heartbeat_every = 0.5, 5.0, 300.0, 30.0

    async def generate():
        started = _asyncio.get_running_loop().time()
        last_stamp = None
        interval, idle = poll, 0.0
        yield "retry: 3000\n\n"
        try:
            while True:
                if (_asyncio.get_running_loop().time() - started) >= max_seconds:
                    yield "event: timeout\ndata: {\"detail\": \"stream lifetime exceeded\"}\n\n"
                    return
                # Stop re-reading the run file the moment the client goes away
                # (a closed tab) instead of leaning solely on CancelledError
                # timing -- matches the goal-events stream.
                if await request.is_disconnected():
                    return
                run = await run_in_threadpool(flow_store.load_run, run_id)
                if run is None:
                    yield "event: error\ndata: {\"detail\": \"run vanished\"}\n\n"
                    return
                if run.updated != last_stamp:
                    last_stamp = run.updated
                    payload = _run_out(run)
                    kind = "terminal" if run.status in end_states else "snapshot"
                    yield f"event: {kind}\ndata: {_json.dumps(payload, default=str)}\n\n"
                    if kind == "terminal":
                        return
                    interval, idle = poll, 0.0
                else:
                    idle += interval
                    if idle >= heartbeat_every:
                        yield ": heartbeat\n\n"
                        idle = 0.0
                    interval = min(max_poll, interval * 1.5)
                await _asyncio.sleep(interval)
        except _asyncio.CancelledError:
            return
        finally:
            sem.release()

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/flows/runs/{run_id}/resume")
async def resume_flow_run_endpoint(request: Request, run_id: str,
                                   payload: FlowResumeIn) -> dict:
    """Resume a paused flow run with a human decision (approve/reject)."""
    require_permission(request, "operate")
    _require_flows()
    run = _load_owned_run(request, run_id)
    # Only a paused run may be resumed -- resuming a completed/rejected run would
    # re-run the flow or run a rejected node (409, not a silent no-op).
    from maverick.flow.runner import PAUSED_STATUSES
    if run.status not in PAUSED_STATUSES:
        raise HTTPException(status_code=409,
                            detail=f"run is not paused (status {run.status})")
    decided_by = ""
    if run.status == "paused_approval":
        if not str(payload.decision or "").strip():
            raise HTTPException(
                status_code=400,
                detail="an explicit approval decision is required",
            )
        decided_by = caller_principal(request) or ""
        if not decided_by and auth_genuinely_off():
            decided_by = "local:dashboard"
        if not decided_by:
            raise HTTPException(
                status_code=403,
                detail="an authenticated approver identity is required",
            )
    from maverick.flow import store

    from maverick_dashboard import automation_queue as aq
    try:
        aq.enqueue_flow_resume(
            run.flow_id,
            run_id,
            payload.decision,
            run.owner,
            decided_by=decided_by,
            inputs=dict(payload.inputs or {}),
        )
    except store.FlowSnapshotError as exc:
        if "approver" in str(exc) or "approval decision" in str(exc):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        raise HTTPException(
            status_code=409,
            detail="run release is no longer active",
        ) from exc
    return {"run_id": run_id, "status": "resuming", "decision": payload.decision}


@router.post("/flows/runs/{run_id}/retry")
async def retry_flow_run_endpoint(request: Request, run_id: str,
                                  from_failure: bool = False) -> dict:
    """Re-run a finished/failed run from scratch with its ORIGINAL trigger inputs
    (a fresh run, so any partial side effects aren't re-driven mid-flow). This is
    the recovery path a dead-lettered run needs -- the run's input_data was kept
    for exactly this.

    ``?from_failure=1`` instead RESUMES a FAILED run in place, re-entering AT the
    node that failed with the data as of the failure -- the pre-failure nodes'
    side effects are not re-driven. Requires the run to be failed with a
    recorded failure cursor (409 otherwise)."""
    require_permission(request, "operate")
    _require_flows()
    run = _load_owned_run(request, run_id)
    # A still-in-flight run (queued/running or parked on ANY pause, including
    # paused_event) must not be retried-from-scratch -- that would spawn a
    # duplicate run alongside the live one. from_failure below is the only path
    # that acts on a failed run.
    from maverick.flow.runner import ACTIVE_STATUSES, QUARANTINED_STATUSES
    if run.status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409,
                            detail=f"run is still active (status {run.status}); nothing to retry")
    if run.status in QUARANTINED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                "run has an indeterminate external-effect boundary; reconcile the "
                "target system before creating any replacement run"
            ),
        )
    from maverick.flow import store
    current_flow = store.load_flow(run.flow_id)
    if current_flow is None:
        raise HTTPException(status_code=404, detail="flow no longer exists")
    _assert_flow_access(request, current_flow)
    from maverick_dashboard import automation_queue as aq
    if from_failure:
        if run.status != "failed" or not run.cursor:
            raise HTTPException(
                status_code=409,
                detail="only a failed run with a recorded failure node can be "
                       "resumed from its failure; use a plain retry instead")
        try:
            aq.enqueue_flow_resume(
                run.flow_id,
                run_id,
                owner=run.owner,
                from_failure=True,
            )
        except store.FlowSnapshotError as exc:
            raise HTTPException(
                status_code=409,
                detail="run release is no longer active",
            ) from exc
        return {"run_id": run_id, "status": "resuming", "from_node": run.cursor}
    # Runs written before immutable definition snapshots have no pin fields at
    # all.  A user-requested retry is explicitly a fresh run against the current
    # definition, so migrate that narrow legacy shape by letting the ordinary
    # enqueue path take a new snapshot.  Partially pinned/tampered rows and
    # modern runs from another definition generation still fail closed.
    legacy_unpinned = (
        not run.definition_digest
        and run.definition_version == 0
        and not run.definition_revision
        and not run.subflow_digests
    )
    if not legacy_unpinned and current_flow.revision != run.definition_revision:
        raise HTTPException(
            status_code=409,
            detail=(
                "the flow definition has changed since this run; start a new "
                "run from the current flow instead"
            ),
        )
    identity = {
        "channel": run.execution_channel or None,
        "user_id": run.execution_user_id or None,
        "allowed_suites": (
            frozenset(run.allowed_suites) if run.allowed_suites is not None else None
        ),
    }
    if run.dry_run:
        if legacy_unpinned:
            raise HTTPException(
                status_code=409,
                detail="legacy dry run has no immutable definition to retry safely",
            )
        new_id = aq.enqueue_flow_run(
            run.flow_id,
            dict(run.input_data or {}),
            run.owner,
            origin=f"retry:{run_id}",
            dry_run=True,
            definition_digest=run.definition_digest,
            release_digest=run.release_digest,
            release_id=run.release_id,
            definition_version=run.definition_version,
            definition_revision=run.definition_revision,
            subflow_digests=dict(run.subflow_digests or {}),
            **identity,
        )
    else:
        try:
            published = store.load_published_bundle(run.flow_id)
        except store.FlowSnapshotError as exc:
            raise HTTPException(
                status_code=409,
                detail="published flow release failed integrity checks",
            ) from exc
        if published is None:
            raise HTTPException(
                status_code=409,
                detail="flow is not published; publish a reviewed draft before retrying",
            )
        published_flow, release = published
        _assert_flow_access(request, published_flow)
        new_id = aq.enqueue_published_flow_run(
            run.flow_id,
            dict(run.input_data or {}),
            run.owner,
            origin=f"retry:{run_id}",
            expected_revision=str(release["release_id"]),
            **identity,
        )
    return {"run_id": new_id, "status": "queued", "retried_from": run_id}


@router.get("/flows/tools")
async def flow_tools_endpoint(request: Request, q: str = "", category: str = "") -> dict:
    """The connector/action catalog for the designer's categorized, searchable
    tool picker: name, description, param names, and a coarse ``category``.

    - no ``q``, no ``category``: the curated starter set.
    - ``category`` (no ``q``): every live tool in that bucket (built-ins + every
      enabled connector), so a user can browse one area instead of a 3k-name list.
    - ``q``: a substring search over the FULL live registry, optionally narrowed
      to ``category``.

    Every response also carries ``categories`` (the display-ordered bucket names)
    so the picker can offer the filter without a second round-trip. Declared
    before ``/flows/{flow_id}`` so the literal ``tools`` path isn't captured as a
    flow id."""
    require_permission(request, "operate")
    _require_flows()
    from .tool_categories import categories as _cat_names
    q = q.strip().lower()[:80]
    category = category.strip()[:40]
    cats = _cat_names()
    user_id = execution_user_id_from_request(request)
    channel = "api" if user_id else None

    if not q and not category:
        return {"tools": await run_in_threadpool(
                    lambda: _flow_tool_catalog(channel=channel, user_id=user_id)),
                "categories": cats}

    def _filter():
        out = _live_tool_index(channel=channel, user_id=user_id)
        if category:
            out = [t for t in out if t.get("category") == category]
        if q:
            out = [t for t in out if q in f"{t['name']} {t['description']}".lower()]
            # name-prefix matches first, then name matches, then description-only
            out = sorted(out, key=lambda t: (not t["name"].lower().startswith(q),
                                             q not in t["name"].lower(), t["name"]))
        else:
            out = sorted(out, key=lambda t: t["name"])
        return out[:50]
    return {"tools": await run_in_threadpool(_filter), "categories": cats}


# Curated starter flows for the designer's gallery: small, runnable graphs a
# user edits (or asks the copilot to change) instead of starting from a blank
# canvas. Each is a plain Flow dict -- validated by the same save path as any
# hand-built graph when the user saves it.
_FLOW_GALLERY: tuple[dict, ...] = (
    {
        "id": "", "name": "Triage inbound issues", "start": "n0",
        "description": "Summarize a new issue, escalate urgent ones, ask before posting.",
        "nodes": [
            {"id": "n0", "kind": "agent", "brief": "Summarize this issue and rate its urgency high/normal: {{title}} — {{body}}", "output": "summary", "next": "n1"},
            {"id": "n1", "kind": "branch", "condition": "urgency == high", "if_true": "n2"},
            {"id": "n2", "kind": "approval", "prompt": "Escalate this to the on-call channel?", "next": "n3"},
            {"id": "n3", "kind": "action", "tool": "slack_bot", "params": {"text": "{{summary}}"}},
        ],
    },
    {
        "id": "", "name": "Daily digest", "start": "n0",
        "description": "On a schedule, gather updates and email a one-paragraph digest.",
        "schedule": "0 9 * * *",
        "nodes": [
            {"id": "n0", "kind": "agent", "brief": "Gather yesterday's notable updates and write a one-paragraph digest.", "output": "digest", "next": "n1_gate"},
            {"id": "n1_gate", "kind": "approval", "prompt": "Send this daily digest?", "next": "n1"},
            {"id": "n1", "kind": "action", "tool": "email", "params": {"body": "{{digest}}"}},
        ],
    },
    {
        "id": "", "name": "Process a list with review", "start": "n0",
        "description": "Loop items concurrently, then a human signs off on the batch.",
        "nodes": [
            {"id": "n0", "kind": "foreach", "items": "rows", "var": "row", "concurrent": True, "output": "results", "next": "n1",
             "body": {"id": "", "name": "per row", "start": "b0",
                      "nodes": [{"id": "b0", "kind": "agent", "brief": "Handle {{row}}", "output": "r"}]}},
            {"id": "n1", "kind": "approval", "prompt": "Batch processed — publish the results?", "next": "n2"},
            {"id": "n2", "kind": "agent", "brief": "Publish the approved results: {{results}}"},
        ],
    },
    {
        "id": "", "name": "Wait for a callback", "start": "n0",
        "description": "Kick off work, wait for an external system to call back, then finish.",
        "nodes": [
            {"id": "n0", "kind": "agent", "brief": "Submit the request described in {{request}}", "output": "submitted", "next": "n1"},
            {"id": "n1", "kind": "wait_event", "prompt": "Waiting for the provider callback", "next": "n2"},
            {"id": "n2", "kind": "agent", "brief": "Reconcile the callback payload {{payload}} with {{submitted}}", "output": "done"},
        ],
    },
)


@router.get("/flows/gallery")
async def flow_gallery_endpoint(request: Request) -> dict:
    """Starter flow graphs for the designer's gallery. Declared before
    ``/flows/{flow_id}`` so the literal path isn't captured as a flow id."""
    require_permission(request, "operate")
    _require_flows()
    return {"flows": [dict(f) for f in _FLOW_GALLERY]}


@router.get("/flows/analytics")
async def flow_analytics_endpoint(request: Request, limit: int = 500) -> dict:
    """Per-flow run analytics over recent runs: volume, success rate, failure
    count, duration p50/p95, and the top recent error messages -- the aggregate
    view a single run page can't give. Owner-scoped. Declared before
    ``/flows/{flow_id}`` so the literal path isn't captured as a flow id."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import analytics
    owner = goal_owner_filter(request)
    # The aggregation reads + parses every run file in the window: keep that
    # off the event loop.
    return await run_in_threadpool(
        analytics.aggregate, owner=owner, limit=max(1, min(2000, limit)))


@router.get("/flows/insights")
async def flow_insights_endpoint(request: Request) -> dict:
    """Proactive automation intelligence across all flows: pending self-rewrite
    proposals and the measured before/after impact of changes already applied --
    so the automations page can surface 'N suggestions to improve your flows'
    without a human opening each one. Read-only. Declared before ``/flows/{flow_id}``
    so the literal ``insights`` path isn't captured as a flow id."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import evolution_log, evolve, store
    flows_out = []
    proposal_count = 0
    for f in store.list_flows():
        if not _can_access_flow(request, f):
            continue
        proposals = [p.to_dict() for p in evolve.maybe_propose(f)]
        changes = []
        for node_id in f.nodes:
            last = evolution_log.last_apply(f.id, node_id)
            if not last:
                continue
            changes.append({
                "node_id": node_id, "to_kind": last.get("to_kind"),
                "source": last.get("source"),
                "impact": evolve.measure(
                    f.id,
                    node_id,
                    float(last.get("ts") or 0.0),
                    before_revision=str(last.get("before_revision") or ""),
                    after_revision=str(last.get("after_revision") or ""),
                )})
        if proposals or changes:
            flows_out.append({"flow_id": f.id, "name": f.name,
                              "proposals": proposals, "changes": changes})
        proposal_count += len(proposals)
    return {"flows": flows_out, "proposal_count": proposal_count}


@router.post("/flows/dry-run")
async def dry_run_flow_draft_endpoint(
    request: Request, payload: FlowDryRunIn,
) -> dict:
    """Run exactly the submitted unsaved graph with side-effect-free executors.

    This endpoint intentionally never calls ``save_flow`` or ``publish_flow``.
    It writes only the immutable execution object and durable dry-run row needed
    to execute/watch the test. A schedule present on the draft is inert.
    """
    require_permission(request, "operate")
    _require_flows()
    from uuid import uuid4

    from maverick.flow import Flow, store
    from maverick.flow.ir import coerce_inputs

    try:
        draft = Flow.from_dict(payload.flow)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid flow: {exc}") from exc
    if not draft.id:
        raise HTTPException(status_code=400, detail="flow needs an 'id'")
    prior = store.load_flow(draft.id)
    if prior is not None:
        _assert_flow_access(request, prior)
        draft.owner = prior.owner
        draft.version = int(prior.version) + 1
        draft.revision = prior.revision
    else:
        draft.owner = caller_principal(request) or ""
        draft.version = 1
        # Never accept a client-selected generation token as provenance.
        draft.revision = uuid4().hex
    errors = draft.validate()
    if errors:
        raise HTTPException(
            status_code=400, detail="invalid flow: " + "; ".join(errors[:5])
        )
    data, input_errors = coerce_inputs(draft, dict(payload.data or {}))
    if input_errors:
        raise HTTPException(status_code=400, detail="; ".join(input_errors))
    try:
        digest, version, subflows = store.snapshot_flow_bundle(draft)
        from maverick_dashboard import automation_queue as aq

        run_id, deduplicated = aq.enqueue_flow_run_once(
            draft.id,
            data,
            _supervisor(request),
            "",
            origin="manual-dry-run",
            dry_run=True,
            channel="api" if execution_user_id_from_request(request) else None,
            user_id=execution_user_id_from_request(request),
            allowed_suites=caller_suites(request),
            definition_digest=digest,
            definition_version=version,
            definition_revision=draft.revision,
            subflow_digests=subflows,
        )
    except store.FlowSnapshotError as exc:
        raise HTTPException(
            status_code=409,
            detail="draft or its immutable execution plan is unavailable",
        ) from exc
    run = store.load_run(run_id)
    return {
        "run_id": run_id,
        "status": run.status if run else "queued",
        "dry_run": True,
        "deduplicated": deduplicated,
    }


@router.get("/flows/{flow_id}")
async def get_flow_endpoint(request: Request, flow_id: str) -> dict:
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import store
    f = store.load_flow(flow_id)
    if f is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, f)
    return {**f.to_dict(), **_flow_publication_fields(flow_id)}


@router.delete("/flows/{flow_id}", status_code=204)
async def delete_flow_endpoint(request: Request, flow_id: str) -> None:
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import store
    f = store.load_flow(flow_id)
    if f is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, f)
    try:
        deleted = store.delete_flow(
            flow_id,
            expected_version=f.version,
            expected_revision=f.revision,
        )
    except store.FlowVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except store.FlowSnapshotError as exc:
        raise HTTPException(
            status_code=409,
            detail="flow could not be safely revoked; its draft was not deleted",
        ) from exc
    if not deleted:
        raise HTTPException(status_code=409, detail="flow changed while it was being deleted")


@router.post("/flows/{flow_id}/run")
async def run_flow_endpoint(request: Request, flow_id: str, payload: FlowRunIn) -> dict:
    """Start the exact active release as a durable live run."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import store
    from maverick.flow.ir import coerce_inputs
    draft = store.load_flow(flow_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, draft)
    if payload.dry_run:
        raise HTTPException(
            status_code=400,
            detail="submit the unsaved graph to /flows/dry-run for a safe test",
        )
    try:
        published = store.load_published_bundle(flow_id)
    except store.FlowSnapshotError as exc:
        raise HTTPException(
            status_code=409,
            detail="published flow release failed integrity checks",
        ) from exc
    if published is None:
        raise HTTPException(
            status_code=409,
            detail="flow is not published; publish the reviewed draft before a live run",
        )
    flow, release = published
    _assert_flow_access(request, flow)
    owner = _supervisor(request)
    # Validate/coerce the payload against the flow's declared inputs (a no-op when
    # the flow declares none): reject a missing required input or a bad number/date
    # up front, and coerce number/bool/date so a node's condition sees typed values.
    data, input_errs = coerce_inputs(flow, dict(payload.data or {}))
    if input_errs:
        raise HTTPException(status_code=400, detail="; ".join(input_errs))
    # Idempotency: a caller-supplied key dedups a repeat submit (double-click, retry)
    # to the same run instead of firing a second one.
    idem = (payload.idempotency_key or "").strip()
    from maverick_dashboard import automation_queue as aq
    try:
        run_id, deduplicated = aq.enqueue_published_flow_run_once(
            flow_id, data, owner, idem,
            origin="manual",
            channel="api" if execution_user_id_from_request(request) else None,
            user_id=execution_user_id_from_request(request),
            allowed_suites=caller_suites(request),
            expected_revision=str(release["release_id"]),
        )
    except store.FlowSnapshotError as exc:
        # Snapshot internals may include object paths/digests. The caller only
        # needs to know that the definition changed or cannot be pinned safely.
        raise HTTPException(
            status_code=409,
            detail="flow changed or its immutable execution plan is unavailable",
        ) from exc
    run = store.load_run(run_id)
    return {"run_id": run_id, "status": run.status if run else "queued",
            "dry_run": False, "deduplicated": deduplicated}


@router.get("/flows/{flow_id}/proposals")
async def flow_proposals_endpoint(request: Request, flow_id: str) -> dict:
    """Self-rewrite proposals for a flow: node-type swaps its grounded per-node
    outcomes justify (harden a reliable agent to an action, soften a flaky action
    to an agent). Proposals only -- never auto-applied."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import evolve, store
    f = store.load_flow(flow_id)
    if f is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, f)
    return {"proposals": [p.to_dict() for p in evolve.maybe_propose(f)]}


@router.get("/flows/{flow_id}/signals")
async def flow_signals_endpoint(request: Request, flow_id: str) -> dict:
    """The grounded learning signals a flow has accumulated, for display:
    per-node success rate (work nodes), human accept-rate (approval nodes), and
    trigger reliability (the reserved ``__trigger__`` outcome). This is what the
    loop CAPTURED made visible -- "which gates humans keep rejecting", "which
    triggers produce runs that succeed" -- so a builder can act on it."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import execution, node_outcomes, store
    f = store.load_flow(flow_id)
    if f is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, f)
    stats = node_outcomes.stats(flow_id)
    trigger = stats.get(execution.TRIGGER_NODE)
    nodes = {}
    for nid, s in stats.items():
        if nid == execution.TRIGGER_NODE:
            continue
        node = f.nodes.get(nid)
        nodes[nid] = {"n": s["n"], "mean": s["mean"], "kind": s.get("kind"),
                      "label": (node.label if node else "") or nid}
    return {"nodes": nodes,
            "trigger": ({"n": trigger["n"], "mean": trigger["mean"]} if trigger else None)}


@router.post("/flows/{flow_id}/apply")
async def apply_flow_proposal_endpoint(request: Request, flow_id: str,
                                       payload: FlowApplyIn) -> dict:
    """Apply a self-rewrite: swap a node between agent and action, producing a new
    flow version. This is the step that lets a flow *change itself* -- the applied
    definition is validated before it is saved, the prior version is retained (so
    it can be rolled back), and the apply is logged so its effect can be measured."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import evolution_log, evolve, store
    f = store.load_flow(flow_id)
    if f is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, f)
    if payload.version is None or not payload.revision:
        raise HTTPException(
            status_code=409,
            detail="flow changed or this editor is stale; reload before applying",
        )
    node = f.nodes.get(payload.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="no such node")
    if node.kind == payload.to_kind:
        raise HTTPException(status_code=400,
                            detail=f"node is already a {payload.to_kind}")
    from_kind = node.kind
    try:
        new_flow = evolve.apply_proposal(
            f,
            payload.node_id,
            payload.to_kind,
            tool=payload.tool,
            params=payload.params,
            brief=payload.brief,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    errs = new_flow.validate()
    if errs:
        raise HTTPException(status_code=400,
                            detail="resulting flow invalid: " + "; ".join(errs[:5]))
    try:
        saved = store.save_flow(
            new_flow,
            expected_version=payload.version,
            expected_revision=payload.revision,
        )
    except store.FlowVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    evolution_log.record_apply(flow_id, payload.node_id, from_kind,
                               payload.to_kind, saved.version, source="manual",
                               before_revision=store.flow_cohort(f),
                               after_revision=store.flow_cohort(saved))
    return {"id": flow_id, "version": saved.version, "node_id": payload.node_id,
            "from_kind": from_kind, "to_kind": payload.to_kind}


@router.get("/flows/{flow_id}/versions")
async def flow_versions_endpoint(request: Request, flow_id: str) -> dict:
    """Every stored version of a flow (newest first), each with a per-kind node
    count -- so the designer can show history and offer a rollback."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import store
    current = store.load_flow(flow_id)
    if current is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, current)

    def _kinds(f) -> dict:
        c: dict[str, int] = {}
        for n in f.nodes.values():
            c[n.kind] = c.get(n.kind, 0) + 1
        return c
    versions = store.list_versions(flow_id)
    versions.reverse()  # newest first
    return {"versions": [{"version": v.version, "name": v.name,
                          "nodes": len(v.nodes), "kinds": _kinds(v)} for v in versions]}


@router.post("/flows/{flow_id}/rollback")
async def rollback_flow_endpoint(request: Request, flow_id: str,
                                 payload: FlowRollbackIn) -> dict:
    """Roll a flow back to a prior version (default: the immediately previous),
    non-destructively -- the restore is itself a new version."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import store
    current = store.load_flow(flow_id)
    if current is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, current)
    if payload.current_version is None or not payload.revision:
        raise HTTPException(
            status_code=409,
            detail="flow changed or this editor is stale; reload before rolling back",
        )
    try:
        restored = store.rollback_flow(
            flow_id,
            payload.version,
            expected_version=payload.current_version,
            expected_revision=payload.revision,
        )
    except store.FlowVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if restored is None:
        raise HTTPException(status_code=404, detail="no such version to roll back to")
    return {"id": flow_id, "version": restored.version}


@router.get("/flows/{flow_id}/nodes/{node_id}/impact")
async def flow_node_impact_endpoint(request: Request, flow_id: str,
                                    node_id: str) -> dict:
    """Whether a node was rewritten and, if so, how its grounded outcomes compare
    before vs. after the change -- the proof that a self-rewrite actually helped."""
    require_permission(request, "operate")
    _require_flows()
    from maverick.flow import evolution_log, evolve, store
    f = store.load_flow(flow_id)
    if f is None:
        raise HTTPException(status_code=404, detail="no such flow")
    _assert_flow_access(request, f)
    if node_id not in f.nodes:
        raise HTTPException(status_code=404, detail="no such node")
    last = evolution_log.last_apply(flow_id, node_id)
    if last is None:
        return {"node_id": node_id, "changed": False, "applied": None, "impact": None}
    impact = evolve.measure(
        flow_id,
        node_id,
        float(last.get("ts") or 0.0),
        before_revision=str(last.get("before_revision") or ""),
        after_revision=str(last.get("after_revision") or ""),
    )
    return {"node_id": node_id, "changed": True,
            "applied": {"from_kind": last.get("from_kind"), "to_kind": last.get("to_kind"),
                        "version": last.get("version"), "ts": last.get("ts"),
                        "source": last.get("source")},
            "impact": impact}


@router.get("/skills", response_model=list[SkillOut])
async def list_installed_skills() -> list[SkillOut]:
    from maverick.skills import load_skills
    return [
        SkillOut(name=s.name, triggers=s.triggers, tools_needed=s.tools_needed)
        for s in load_skills()
    ]


@router.get("/flywheel")
async def get_flywheel_state() -> dict:
    """What the Cognitive Data Engine has learned from the workforce's own outcomes.

    A read-only window into the moat: the **guardrails** it has mined (actions it
    causally shows lower outcomes, each with the effect that justifies it -- and
    auto-dropped when the harm is gone) and the **habits** it has consolidated
    (causally-beneficial actions, with a reinforcement strength). Empty until the
    flywheel has turned. Read-only; never mutates."""
    out: dict = {"guardrails": [], "habits": [], "signal": {}}
    try:
        from maverick.negative_knowledge import shared as _guardrails
        out["guardrails"] = [g.to_dict() for g in _guardrails().all()]
    except Exception:  # pragma: no cover -- observability never errors the API
        pass
    try:
        from maverick.procedural_memory import shared as _memory
        out["habits"] = [m.to_dict() for m in _memory().recall(top_k=20)]
    except Exception:  # pragma: no cover
        pass
    # The grounded signal accumulating under the moat: how many real outcomes
    # (failures, sign-offs, feedback, system-of-record results) and human verdicts
    # have landed. Makes "the workforce is learning from reality" a live number.
    try:
        from maverick import consequence

        from maverick_dashboard import goal_feedback_store
        out["signal"] = {
            "grounded_outcomes": consequence.count(),
            "human_feedback": goal_feedback_store.count(),
        }
    except Exception:  # pragma: no cover
        pass
    return out


@router.get("/codec")
async def get_codec_telemetry() -> dict:
    """What the token-aware emergent codec is saving on the LIVE coordination stream.

    Confirms the bench numbers against production: as the swarm runs, the blackboard
    measures (never applies) what the codec would compress each rendered coordination
    block to -- byte savings always, token savings when a tokenizer is registered in
    this process. In-process counters, so this reflects the runtime hosting the
    agents. Zeroed until ``[emergent_codec] enable`` is on and a codebook is learned.
    Read-only; never mutates."""
    try:
        from maverick.codec_telemetry import snapshot
        return snapshot().to_dict()
    except Exception:  # pragma: no cover -- observability never errors the API
        return {"n_blocks": 0, "tokens_measured": False}


def _require_skill_install_opt_in() -> None:
    if os.environ.get("MAVERICK_ALLOW_SKILL_INSTALL", "").lower() not in {"1", "true", "yes"}:
        raise HTTPException(
            status_code=403,
            detail=(
                "Installing skills from the dashboard is disabled on this "
                "server — an administrator can opt in (set "
                "MAVERICK_ALLOW_SKILL_INSTALL=1, or run "
                "`maverick skill install` on the host)."
            ),
        )


@router.post("/skills", response_model=SkillOut, status_code=201)
async def install_skill_endpoint(request: Request, payload: SkillInstallIn) -> SkillOut:
    """Install a skill from a URL or ``gh:org/repo[:path]``.

    Skill install runs untrusted code at the next agent invocation. The
    endpoint is gated behind ``MAVERICK_ALLOW_SKILL_INSTALL=1`` so a
    compromised dashboard token can't be turned into one-shot RCE; an
    operator opting in is taking explicit ownership of the supply
    chain. CLI ``maverick skill install`` remains available without
    the flag because it requires shell access on the host.

    RBAC: this is a control-plane change to the code the agent loads, so it
    requires ``admin`` -- matching ``/plugins/install``. The opt-in flag is
    process-wide, not per-user, so it can't stand in for a role check (a
    view-only principal must not install/replace agent code).
    """
    require_permission(request, "admin")
    _require_skill_install_opt_in()
    from maverick.skills import install_skill
    try:
        s = install_skill(payload.source, trusted_local=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SkillOut(name=s.name, triggers=s.triggers, tools_needed=s.tools_needed)


@router.post("/skills/create", response_model=SkillOut, status_code=201)
async def create_skill_endpoint(request: Request, payload: SkillCreateIn) -> SkillOut:
    """Author a skill from the dashboard form (name / triggers / tools /
    instructions) and install it.

    Lower risk than installing a remote source -- the content is the body the
    author typed, not fetched code -- but it still lands in agent prompts and is
    secret/shield-scanned, so it shares the same ``MAVERICK_ALLOW_SKILL_INSTALL``
    opt-in as install. 422 on invalid input (no trigger, empty body, ...).
    Requires ``admin`` (installs agent-loaded code), like install."""
    require_permission(request, "admin")
    _require_skill_install_opt_in()
    from maverick.skills import create_skill
    try:
        s = create_skill(payload.name, payload.instructions,
                         triggers=payload.triggers, tools_needed=payload.tools_needed)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return SkillOut(name=s.name, triggers=s.triggers, tools_needed=s.tools_needed)




@router.get("/diag/tail-latency")
async def diag_tail_latency(ratio: float = 3.0, min_count: int = 20) -> dict:
    """Tools with a fat latency tail (p99/p50 ≥ ratio) — the ones worth hunting.

    Reads this serving process's in-memory per-tool latency samples, so it's
    meaningful on a long-lived dashboard/worker, not a fresh CLI invocation."""
    from maverick.tail_latency import hunt
    return {"flagged": hunt(ratio_threshold=ratio, min_count=min_count)}


@router.get("/marketplace/stats")
async def marketplace_stats() -> dict:
    """Aggregate stats over the local ratings ledger (total / average / 1–5★
    distribution / per-kind / top-rated). Self-host-first: the operator's own
    ratings, the JSON face of the marketplace stats view."""
    from maverick.marketplace.ratings import RatingsLedger
    from maverick.marketplace.stats import summarize
    return summarize(RatingsLedger())


@router.get("/templates")
async def templates_catalog() -> dict:
    """The goal-template catalog with the operator's own ratings — the JSON
    face of the /templates marketplace page."""
    from maverick_dashboard.app import template_market_entries
    return {"templates": template_market_entries()}


@router.get("/templates/suggested")
async def templates_suggested(request: Request, k: int = 5) -> dict:
    """Personalized starter templates: the catalog ranked for THIS user from
    their goal-title history (pure scorer, no LLM — ``maverick.starter_templates``).
    Owner-scoped history: an authenticated non-admin is ranked on their own
    goals only."""
    from maverick.starter_templates import suggest
    k = max(1, min(int(k or 5), 20))
    return {
        "suggested": suggest(_world(), k=k, owner=goal_owner_filter(request)),
    }


def _voice_commands_enabled() -> bool:
    """Dashboard voice-command input. ON by default; off via
    MAVERICK_VOICE_COMMANDS=0 / [voice] dashboard_commands = false."""
    import os as _os
    raw = (_os.environ.get("MAVERICK_VOICE_COMMANDS") or "").strip().lower()
    if raw:
        return raw not in {"0", "false", "no", "off"}
    try:
        from maverick.config import load_config
        return bool((load_config().get("voice") or {}).get("dashboard_commands", True))
    except Exception:  # pragma: no cover -- config never blocks the endpoint
        return True


# Audio container suffixes the STT backends accept; browsers record webm/ogg.
_VOICE_SUFFIXES = {
    ".webm": ".webm", ".ogg": ".ogg", ".oga": ".ogg", ".mp3": ".mp3",
    ".wav": ".wav", ".m4a": ".m4a", ".mp4": ".mp4", ".flac": ".flac",
}
_VOICE_MAX_BYTES = 25 * 1024 * 1024


def _voice_transcribe_concurrency_limit() -> int:
    try:
        return max(1, int(os.environ.get("MAVERICK_VOICE_TRANSCRIBE_CONCURRENCY", "2")))
    except ValueError:
        return 2


_voice_transcribe_sem_limit = _voice_transcribe_concurrency_limit()
_voice_transcribe_sem = asyncio.Semaphore(_voice_transcribe_sem_limit)


def _get_voice_transcribe_semaphore() -> asyncio.Semaphore:
    global _voice_transcribe_sem, _voice_transcribe_sem_limit
    limit = _voice_transcribe_concurrency_limit()
    if limit != _voice_transcribe_sem_limit:
        _voice_transcribe_sem = asyncio.Semaphore(limit)
        _voice_transcribe_sem_limit = limit
    return _voice_transcribe_sem


@router.post("/voice/transcribe")
async def voice_transcribe(
    request: Request,
    file: UploadFile = File(...),
    language: str = "",
) -> JSONResponse:
    """Transcribe a short voice recording to text (dashboard voice commands).

    The chat composer's mic button records audio in the browser and posts it
    here; the transcript goes back into the goal field so the user can review
    before submitting (speech never starts a goal unreviewed). Reuses the
    kernel STT backends (OpenAI / Groq Whisper, local faster-whisper, local
    whisper.cpp — the built-in offline path, `maverick voice setup`) via
    ``maverick.tools.voice``; 503 with a setup hint when none is configured,
    which the client treats as "fall back to browser speech recognition".
    """
    if not _voice_commands_enabled():
        raise HTTPException(status_code=404, detail="voice commands are disabled")
    require_permission(request, "operate")
    # STT can spend provider quota or run a CPU-heavy local Whisper model.
    # Apply the same dashboard spend backstop used by paid goal creation before
    # reading/writing the upload or dispatching to the backend.
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request)
    # First-run warm-up still downloading/loading the local model: tell the
    # client to retry shortly (Retry-After distinguishes "almost ready" from
    # the terminal "no backend" 503, which triggers browser fallback).
    from maverick.tools.voice import stt_warming
    if stt_warming():
        raise HTTPException(
            status_code=503,
            detail=("local speech-to-text is still warming up "
                    "(first-run model download) — retry in a few seconds."),
            headers={"Retry-After": "5"},
        )

    import os as _os
    import tempfile as _tempfile

    max_bytes = _VOICE_MAX_BYTES
    try:
        max_bytes = int(_os.environ.get("MAVERICK_VOICE_MAX_BYTES", max_bytes))
    except (TypeError, ValueError):
        pass
    data = await file.read(max_bytes + 1)
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"audio too large: {len(data)} bytes (limit {max_bytes})",
        )

    suffix = _VOICE_SUFFIXES.get(
        _os.path.splitext(file.filename or "")[1].lower(), ".webm")
    lang = (language or "").strip()[:8] or None

    from maverick.tools.voice import _run_transcribe
    tmp = _tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.close()
        # sandbox=None: the path is our own temp file, not model-supplied.
        args: dict = {"source": tmp.name}
        if lang:
            args["language"] = lang
        sem = _get_voice_transcribe_semaphore()
        if sem.locked():
            raise HTTPException(
                status_code=429,
                detail="voice transcription concurrency limit reached",
                headers={"Retry-After": "1"},
            )
        async with sem:
            text = await run_in_threadpool(_run_transcribe, args, None)
    finally:
        try:
            _os.unlink(tmp.name)
        except OSError:  # pragma: no cover
            pass
    if text.startswith("ERROR:"):
        raise HTTPException(
            status_code=503,
            detail=(
                "no speech-to-text backend available. Run `maverick voice "
                "setup` for the built-in local engine (whisper.cpp), set "
                "OPENAI_API_KEY / GROQ_API_KEY, or install faster-whisper "
                "(python -m pip install -e './packages/maverick-core[voice]')."
            ),
        )
    return JSONResponse({"text": text.strip()})


@router.post("/voice/speak")
async def voice_speak(request: Request, payload: SpeakIn) -> Response:
    """Synthesize speech for a reply (dashboard read-aloud).

    Returns the mp3 bytes; the client plays them inline. Reuses the kernel
    TTS backends (OpenAI TTS / ElevenLabs via ``maverick.tools.voice``),
    including the voice-safety redaction pass — a secret must never be
    spoken aloud. 503 with a setup hint when no backend is configured.
    """
    if not _voice_commands_enabled():
        raise HTTPException(status_code=404, detail="voice commands are disabled")
    require_permission(request, "operate")
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    text = text[:4096]
    try:
        from maverick.safety.voice_safety import redact_for_speech
        text, _redactions = redact_for_speech(text)
    except Exception:  # pragma: no cover -- redaction is fail-open by design
        pass

    import os as _os
    import tempfile as _tempfile
    from pathlib import Path as _Path

    from maverick.tools.voice import _tts_elevenlabs, _tts_openai

    def _synthesize() -> bytes | None:
        tmp = _tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        out = _Path(tmp.name)
        try:
            if _tts_openai(text, payload.voice, out) or _tts_elevenlabs(
                    text, payload.voice, out):
                return out.read_bytes()
            return None
        finally:
            try:
                _os.unlink(tmp.name)
            except OSError:  # pragma: no cover
                pass

    audio = await run_in_threadpool(_synthesize)
    if not audio:
        raise HTTPException(
            status_code=503,
            detail=(
                "no text-to-speech backend available. Set OPENAI_API_KEY or "
                "ELEVENLABS_API_KEY."
            ),
        )
    return Response(content=audio, media_type="audio/mpeg")


@router.get("/voice/captions")
async def voice_captions(
    request: Request, source: str = "default", max_chars: int = 160,
) -> StreamingResponse:
    """Live captions (SSE) over the voice transcript seam.

    Streams one ``data: {caption, final, ts}`` frame per transcript segment
    from the named source in ``maverick.live_captions``'s source registry,
    then ``event: end`` when the source is exhausted. Default-off: the
    registry starts empty (no live mic — a deployment registers its ASR
    pipeline; tests register scripted sources), so an unregistered source
    404s.
    """
    principal = caller_principal(request)
    if principal is not None and not is_dashboard_admin(principal):
        raise HTTPException(status_code=404, detail="no such caption source")

    from maverick.live_captions import caption_stream, get_source
    factory = get_source(source)
    if factory is None:
        raise HTTPException(
            status_code=404,
            detail=f"no caption source registered as {source!r}; "
                   "register one via maverick.live_captions.register_source",
        )
    try:
        max_chars = max(16, min(int(max_chars), 500))
    except (TypeError, ValueError):
        max_chars = 160

    # Bound concurrent caption streams and release the slot on disconnect/error,
    # exactly like the goal-events stream. A live caption source never exhausts
    # on its own, so without this an abandoned (or maliciously opened-and-never-
    # read) connection would pin an async task + fd indefinitely, and unlimited
    # such connections would exhaust the event loop. Shares the SSE semaphore.
    sem = _get_sse_semaphore()
    if sem.locked():
        raise HTTPException(
            status_code=503,
            detail="too many concurrent caption streams; retry shortly",
            headers={"Retry-After": "5"},
        )
    await sem.acquire()

    async def _gen():
        try:
            yield ": captions\n\n"
            async for frame in caption_stream(factory(), max_chars=max_chars):
                if await request.is_disconnected():
                    return
                yield f"data: {json.dumps(frame)}\n\n"
            yield "event: end\ndata: {}\n\n"
        except asyncio.CancelledError:
            return
        finally:
            sem.release()

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/catalog/{kind}")
async def catalog_list(kind: str) -> dict:
    """List federated catalog entries for a kind (skills/plugins/mcp/personas).

    Tolerates an unreachable index by returning an empty list, so a
    fresh install shows "no catalog entries" rather than 500ing.
    """
    from maverick.catalog import VALID_KINDS, load_catalog
    if kind not in VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown kind {kind!r}; valid: {', '.join(VALID_KINDS)}",
        )
    entries = load_catalog(kind)
    return {"kind": kind, "entries": [e.to_dict() for e in entries]}


@router.post("/catalog/skills/install", response_model=SkillOut, status_code=201)
async def catalog_install_skill(request: Request, payload: CatalogInstallIn) -> SkillOut:
    """Install a catalog skill by name.

    Catalog metadata (source + hash) can come from remote indexes, so
    this endpoint keeps the same operator opt-in gate as free-text skill
    installs. Requires ``admin`` (installs agent-loaded code), like install.
    """
    require_permission(request, "admin")
    _require_skill_install_opt_in()
    from maverick.skills import install_from_catalog
    try:
        s = install_from_catalog(payload.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return SkillOut(name=s.name, triggers=s.triggers, tools_needed=s.tools_needed)


@router.delete("/skills/{name}", status_code=204)
async def remove_skill_endpoint(request: Request, name: str) -> None:
    # Removing an installed skill is a control-plane mutation of agent-loaded
    # code; require admin (a view-only principal must not pull skills).
    require_permission(request, "admin")
    from maverick.skills import remove_skill
    if not remove_skill(name):
        raise HTTPException(status_code=404, detail="no such skill")


# ---------- self-learning: learned ledger + generated tools (#427) ----------


def _learned_snapshot(limit: int = 50, *, include_pending_items: bool = False) -> dict:
    """Read-only view of the self-learning ledger + on-disk generated tools.

    Powers the /learned page and GET /api/v1/learned. Pending corpus
    counts are safe for viewers, but staged candidate details may contain
    harvested operator text and are included only for admin callers. Degrades
    to empty lists if the self-learning module / ledger / dir is unavailable, so a
    fresh install (feature never enabled) renders "nothing learned yet"
    instead of 500ing.
    """
    learned: list[dict] = []
    tools: list[str] = []
    try:
        from maverick import self_learning
        # Pass the path explicitly: history()'s default arg is bound at
        # import time, so reading the module global here lets the live
        # LEARNED_PATH (and tests) take effect.
        learned = [
            e.to_dict()
            for e in self_learning.history(
                limit=limit, path=self_learning.LEARNED_PATH,
            )
        ]
        d = self_learning.GENERATED_TOOLS_DIR
        if d.exists():
            tools = sorted(
                p.name for p in d.glob("*.py")
                if not p.name.startswith((".", "_"))
            )
    except Exception as e:  # pragma: no cover -- never block the page
        log.debug("learned snapshot failed: %s", e)
    harness, harness_enabled = _harness_guidance()
    pending = _pending_corpus_items()
    return {
        "learned": learned, "generated_tools": tools,
        "harness": harness, "harness_enabled": harness_enabled,
        "pending_corpus": {k: len(v) for k, v in pending.items()},
        "pending_items": pending if include_pending_items else {},
        "transfer_memory": _transfer_memory(),
    }


def _pending_corpus_items() -> dict[str, list[dict]]:
    """Harvested corpus candidates awaiting operator review, per corpus key --
    so staged ground truth is visible (and resolvable in-page), not
    forgotten. A CORPUS-key fact, kept apart from the learned-guidance
    snapshot: the section must show even when a key has staged cases but no
    learned lines yet (the bootstrap case), and a failure here must not blank
    the guidance list (or vice versa)."""
    try:
        from maverick.self_harness import settings
        from maverick.self_harness_eval import load_pending
        cpath = settings().get("eval_corpus")
        if cpath:
            return {k: v for k, v in load_pending(cpath).items() if v}
    except Exception as e:  # pragma: no cover -- pending items are advisory
        log.debug("pending corpus snapshot failed: %s", e)
    return {}


def _transfer_memory() -> dict:
    """Content-free transfer tried-memory stats (judged pairs + newest ts)
    for the /learned page -- how caught-up the nightly sweep is."""
    try:
        from maverick.self_harness import transfer_memory_stats
        return transfer_memory_stats()
    except Exception as e:  # pragma: no cover -- advisory
        log.debug("transfer memory snapshot failed: %s", e)
        return {"pairs": 0, "last_judged_at": None}


def _harness_guidance() -> tuple[list[dict], bool]:
    """The self-harness per-model operating guidance recalled into prompts.

    Returns ``([{model_id, lines, provenance, conflicts}], enabled)``. This is
    the self-harness loop's most prompt-impacting output, yet the /learned page
    surfaced only the capability ledger -- an operator could see it ONLY via
    `maverick self-harness show`. Read-only; ``list_learned`` reads the store
    regardless of the toggle (so paused guidance is still inspectable), so we
    return the ``enabled`` flag too -- stored guidance is NOT recalled into
    prompts until the feature is on. Kept in its own try so a self-harness
    failure never blanks the capability ledger, and vice versa."""
    try:
        from maverick.self_harness import (
            detect_store_conflicts,
            enabled,
            line_efficacy,
            line_provenance,
            list_canaries,
            list_learned,
        )
        conflicts_by_model: dict[str, list] = {}
        try:
            for m, a, b in detect_store_conflicts():
                conflicts_by_model.setdefault(m, []).append([a, b])
        except Exception:  # pragma: no cover -- conflicts are advisory
            pass

        def _enriched_provenance(m: str) -> list[dict]:
            # Fold the per-line outcome record (efficacy counters) and canary
            # flag into each provenance entry, keyed by (domain, text) so a line
            # that lives in two scopes isn't cross-attributed. Both are advisory
            # reads -- a failure here must not blank the guidance list.
            prov = line_provenance(m)
            try:
                eff = {(r.get("domain"), r["text"]): r for r in line_efficacy(m)}
            except Exception:  # pragma: no cover -- efficacy is advisory
                eff = {}
            try:
                canaries = set(list_canaries(m))
            except Exception:  # pragma: no cover -- canary state is advisory
                canaries = set()
            for rec in prov:
                e = eff.get((rec.get("domain"), rec["text"]))
                if e:
                    rec["success"], rec["failure"] = e["success"], e["failure"]
                    rec["rate"] = e["rate"]
                    rec["recent_success"] = e.get("recent_success")
                    rec["recent_failure"] = e.get("recent_failure")
                    rec["recent_rate"] = e.get("recent_rate")
                rec["canary"] = rec["text"] in canaries
            return prov

        rows = [{"model_id": m, "lines": lines,
                 "provenance": _enriched_provenance(m),
                 "conflicts": conflicts_by_model.get(m, [])}
                for m, lines in sorted(list_learned().items())]
        return rows, bool(enabled())
    except Exception as e:  # pragma: no cover -- never block the page
        log.debug("harness guidance snapshot failed: %s", e)
        return [], False


def _resolve_generated_tool(name: str):
    """Resolve ``name`` to a ``*.py`` file strictly inside GENERATED_TOOLS_DIR.

    Path-safety guard for the removal endpoint: the resolved target must be
    a direct child of the generated-tools dir and end in ``.py``. Rejects
    traversal (``..``), absolute paths, and subdirectory escapes by
    comparing the resolved parent against the resolved dir. Returns the
    Path or raises HTTPException(400).
    """
    from maverick import self_learning
    d = self_learning.GENERATED_TOOLS_DIR
    # A legitimate generated-tool filename is a bare ``<name>.py`` with no
    # separators; reject anything with a path separator or traversal token
    # before touching the filesystem.
    if "/" in name or "\\" in name or name in ("", ".", "..") or not name.endswith(".py"):
        raise HTTPException(status_code=400, detail="invalid generated-tool name")
    target = (d / name).resolve()
    base = d.resolve()
    if target.parent != base:
        raise HTTPException(status_code=400, detail="path outside generated_tools")
    return target


@router.get("/learned")
async def learned_api(request: Request) -> dict:
    """Learned-capability ledger entries + on-disk generated tool filenames."""
    return _learned_snapshot(include_pending_items=has_permission(request, "admin"))


@router.delete("/generated-tools/{name}", status_code=204)
async def remove_generated_tool(request: Request, name: str) -> None:
    """Delete a persisted generated tool so a bad one can be pulled.

    The valuable mutation of #427: removes ~/.maverick/generated_tools/<name>
    without filesystem access. The path is resolved strictly under the
    generated-tools dir (see ``_resolve_generated_tool``) so traversal /
    absolute / out-of-dir names are refused. Auth/same-origin is enforced
    centrally by the dashboard's bearer_auth middleware (DELETE is a
    mutating method, so the same-origin check applies), and RBAC requires
    ``admin`` -- deleting agent-loaded code is not a view-only action.
    """
    require_permission(request, "admin")
    target = _resolve_generated_tool(name)
    from maverick import self_learning
    from maverick.audit import AuditRefused
    from maverick.safety.consent import ConsentLedgerError

    try:
        await run_in_threadpool(
            self_learning.delete_generated_tool,
            target.stem,
            actor=caller_principal(request) or "local",
        )
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="invalid generated-tool name",
        ) from None
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="no such generated tool",
        ) from None
    except (
        AuditRefused,
        ConsentLedgerError,
        self_learning.GeneratedToolRemovalError,
    ):
        # The source stays present when its durable authority/audit tombstone
        # cannot be committed. Do not turn an evidence failure into an
        # unaudited deletion.
        raise HTTPException(
            status_code=503,
            detail="could not durably revoke generated-tool authority",
        ) from None
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"could not remove: {e}") from e


@router.post("/harness-corpus/review")
async def review_harness_corpus(request: Request, payload: dict) -> dict:
    """Resolve staged corpus candidates from the dashboard -- the same
    accept/reject verdicts as ``maverick self-harness corpus review``, so
    staged ground truth can be reviewed where it is displayed. Mutating the
    loop's ground truth is an ``admin`` action (same rung as pulling a
    generated tool); bearer/same-origin is enforced centrally by the
    middleware (POST is a mutating method)."""
    require_permission(request, "admin")
    from maverick import self_harness_eval as ev
    from maverick.self_harness import settings
    corpus_path = settings().get("eval_corpus")
    if not corpus_path:
        raise HTTPException(status_code=409, detail="no eval corpus configured")
    key = str(payload.get("key") or "")
    if not key:
        raise HTTPException(status_code=400, detail="missing corpus key")

    def _indexes(name: str) -> list[int]:
        v = payload.get(name) or []
        if not isinstance(v, list) or not all(
                isinstance(i, int) and not isinstance(i, bool) for i in v):
            raise HTTPException(status_code=400,
                                detail=f"{name} must be a list of 1-based indexes")
        return v

    accept, reject = _indexes("accept"), _indexes("reject")
    if not accept and not reject:
        raise HTTPException(status_code=400, detail="nothing to resolve")
    try:
        res = ev.resolve_pending(corpus_path, key, accept=accept, reject=reject)
    except ValueError as e:
        # Indexes rebased under the caller (another review/harvest landed):
        # a conflict, not a bad request -- the page reloads and re-lists.
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"merged": res["merged"], "duplicates": res["duplicates"],
            "rejected": res["rejected"]}


@router.get("/spend")
async def get_spend(request: Request) -> dict:
    """Spend + recent run costs, scoped to the caller's own runs.

    ``goal_owner_filter`` returns the caller's principal (so each user sees
    only their own spend/episodes) or ``None`` for an admin / auth-off caller
    (the deployment-wide view). Previously this returned every user's runs and
    total spend to any authenticated caller.
    """
    owner = goal_owner_filter(request)
    w = _world()
    total = w.total_spend(owner=owner)
    episodes = w.list_episodes(limit=30, owner=owner)
    return {
        "total": total,
        "episodes": [
            {
                "id": e.id, "goal_id": e.goal_id, "started_at": e.started_at,
                "ended_at": e.ended_at, "outcome": e.outcome,
                "cost_dollars": e.cost_dollars,
                "input_tokens": e.input_tokens,
                "output_tokens": e.output_tokens,
                "tool_calls": e.tool_calls,
            }
            for e in episodes
        ],
    }


@router.get("/savings")
async def get_savings(request: Request, days: int = 90) -> dict:
    """The savings report: money saved vs the typical human cost, computed
    from REAL completed work and the client's OWN assumptions ([value]).

    ``(deliverables x hours_per_task x hourly_rate) - actual agent spend``,
    per department and in total, over ``?days`` (clamped 1..730). Read-only;
    with no run history yet it reports zeros, never an error, and never
    invents a number.
    """
    require_permission(request, "view")
    from maverick import savings as savings_mod
    from maverick.config import get_value
    days = max(1, min(int(days), 730))
    cfg = get_value()
    report = await run_in_threadpool(
        savings_mod.compute, _world(), window_days=days, cfg=cfg)
    return {
        "enabled": cfg["enable"],
        "report": savings_mod.to_dict(report),
        "assumptions": {
            "hourly_rate": cfg["hourly_rate"],
            "hours_per_task": cfg["hours_per_task"],
            "currency": cfg["currency"],
            "departments": cfg["departments"],
        },
    }


@router.put("/savings/assumptions")
async def put_savings_assumptions(request: Request,
                                  body: ValueAssumptionsIn) -> dict:
    """Persist the client's cost/value inputs (their hourly rate and human
    hours per task, plus per-department overrides) to the dashboard overlay --
    the same [value] section config.toml carries, so the wizard and the UI
    stay one source of truth. Operate-gated: these numbers become the ROI
    story a client repeats to their CFO."""
    require_permission(request, "operate")
    from maverick.config import get_value, reset_config_cache

    from . import settings_store
    deps = {
        name: (None if ov is None else
               {k: v for k, v in (("hourly_rate", ov.hourly_rate),
                                  ("hours_per_task", ov.hours_per_task))
                if v is not None})
        for name, ov in (body.departments or {}).items()
    }
    try:
        settings_store.set_value_assumptions(
            hourly_rate=body.hourly_rate,
            hours_per_task=body.hours_per_task,
            currency=body.currency,
            departments=deps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reset_config_cache()
    cfg = get_value()
    return {
        "saved": True,
        "assumptions": {
            "hourly_rate": cfg["hourly_rate"],
            "hours_per_task": cfg["hours_per_task"],
            "currency": cfg["currency"],
            "departments": cfg["departments"],
        },
    }


_SECURITY_REGISTER_CACHE_TTL_SECONDS = 60.0
_SECURITY_REGISTER_HUNT_DAYS = 7
_security_register_cache: tuple[float, dict] | None = None
_security_register_cache_lock = threading.Lock()


def _security_hunt_window() -> tuple[str, str]:
    today = _dt.datetime.now(_dt.timezone.utc).date()
    since = today - _dt.timedelta(days=_SECURITY_REGISTER_HUNT_DAYS - 1)
    return since.isoformat(), today.isoformat()


def _security_register_snapshot_uncached() -> dict:
    controls: list[dict] = []
    try:
        from maverick.compliance import compliance_report
        controls = [
            {"control": c.control, "status": c.status, "regulation": c.regulation,
             "detail": c.detail, "framework": c.framework}
            for c in compliance_report()
        ]
    except Exception:
        log.warning("security register: compliance probe failed", exc_info=True)

    threat: dict = {"risk": "clear", "events_scanned": 0, "findings": []}
    breaches: list[dict] = []
    try:
        from maverick.threat_hunt import hunt
        since, until = _security_hunt_window()
        r = hunt(all_days=False, since=since, until=until)
        findings = [
            {"kind": f.kind, "title": f.title, "severity": f.severity,
             "count": f.count, "agents": f.agents}
            for f in r.findings
        ]
        threat = {
            "risk": r.risk_rating, "events_scanned": r.events_scanned,
            "window": {"since": since, "until": until},
            "findings": findings,
        }
        breaches = [
            {"kind": f.kind, "title": f.title, "severity": f.severity, "count": f.count}
            for f in r.findings
        ]
    except Exception:
        log.warning("security register: threat hunt failed", exc_info=True)

    remediation: dict = {"auto_fix_enabled": False, "gaps": [], "breaches": breaches}
    try:
        from maverick.remediation import plan
        p = plan(include_breaches=False)
        remediation = {
            "auto_fix_enabled": p.auto_fix_enabled,
            "gaps": [
                {"control": g.control, "title": g.title, "auto": g.auto,
                 "rationale": g.rationale}
                for g in p.gaps
            ],
            "breaches": breaches,
        }
    except Exception:
        log.warning("security register: remediation plan failed", exc_info=True)

    return {"controls": controls, "threat_hunt": threat, "remediation": remediation}


def _security_register_snapshot() -> dict:
    global _security_register_cache
    with _security_register_cache_lock:
        now = time.monotonic()
        if (
            _security_register_cache is not None
            and now - _security_register_cache[0] < _SECURITY_REGISTER_CACHE_TTL_SECONDS
        ):
            return _security_register_cache[1]
        snapshot = _security_register_snapshot_uncached()
        _security_register_cache = (time.monotonic(), snapshot)
        return snapshot


@router.get("/security")
async def security_register() -> dict:
    """The privacy/security agent team's register, read-only and fail-soft.

    The audit hunt is bounded to a recent window, cached briefly, and run off
    the event loop so cross-site or repeated GETs cannot force unbounded
    synchronous audit-log scans on every request.
    """
    return await run_in_threadpool(_security_register_snapshot)


# ---------- council pass: control surface ----------



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
    # Reflect the cluster-wide halt too: on a shared backend it may be armed by
    # another replica with no local file here. Postgres only; best-effort.
    shared = None
    if _shared_halt_backend():
        try:
            shared = _world().active_halt()
        except Exception:
            shared = None
    if shared:
        out["active"] = True
        out["cluster_halt"] = True
        out["reason"] = out["reason"] or (shared.get("reason") or None)
        out["armed_at"] = out["armed_at"] or shared.get("armed_at")
        out["armed_by"] = shared.get("armed_by") or None
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
    # Also arm the cluster-wide halt in the shared store so the stop propagates
    # to every replica (the file only halts this one). Only on a shared backend
    # (Postgres): on single-host SQLite the file halt is the whole mechanism and
    # the killswitch doesn't consult the shared row. Best-effort -- a shared-store
    # error must not fail the local arm, which already took effect above.
    if _shared_halt_backend():
        try:
            _world().arm_halt(reason, source="dashboard",
                              armed_by=caller_principal(request) or "")
        except Exception:
            log.warning("cluster-wide halt arm failed (local file halt still set)",
                        exc_info=True)


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
    # Clear the cluster-wide halt too (Postgres only; see halt_set). Best-effort
    # so a shared-store error doesn't block clearing the local halt.
    if _shared_halt_backend():
        try:
            _world().disarm_halt()
        except Exception:
            log.warning("cluster-wide halt clear failed", exc_info=True)


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
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    if _goal_gate(g.domain) is None:
        raise HTTPException(status_code=400, detail="this deliverable has no sign-off gate")
    if g.status != "done":
        raise HTTPException(
            status_code=409,
            detail="the deliverable must be finished before it can be signed off",
        )
    who = _supervisor(request)
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
    # Feed the human's certify/reject verdict back into the learning loop as a
    # GROUNDED outcome: a person judging the actual deliverable is the cleanest
    # ground-truth signal in the system, and it lands as exactly the
    # (goal_id, episode_id, value) tuple the Consequence Engine / data-engine
    # already consume. Both verdicts are signal -- an approval is a 1.0 reward,
    # a rejection a 0.0. Best-effort and gated: no-op unless [consequence] is on.
    if changed:
        _record_signoff_outcome(w, goal_id, payload.decision)
        if payload.decision == "approved":
            _handoff_approved_deliverable(g, who)
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
    from maverick_dashboard import goal_feedback_store
    return {"feedback": goal_feedback_store.latest_for_goal(goal_id)}


@router.post("/goals/{goal_id}/feedback")
async def post_feedback(request: Request, goal_id: int, payload: FeedbackIn) -> dict:
    """Record a human thumbs-up/down on a goal's result.

    The always-available verdict: unlike the sign-off gate (which only exists when
    a pack declares one), any finished result can be rated here, and the rating is
    fed back into the learning loop as a GROUNDED outcome -- an up is a 1.0 reward,
    a down a 0.0 -- exactly the ``(goal_id, episode_id, value)`` tuple the
    Consequence Engine / flywheel consume. Persisted for the UI + the "what your
    workforce learned" count. Best-effort grounding, gated on ``[consequence]``."""
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    value = 1.0 if payload.rating == "up" else 0.0
    who = _supervisor(request)
    from maverick_dashboard import goal_feedback_store
    row = goal_feedback_store.record(goal_id, g.owner or "", payload.rating, value=value,
                                     note=payload.note or "", by=who)
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


def _handoff_approved_deliverable(g, decided_by: str) -> None:
    """Push an approved deliverable to the configured system-of-record endpoint.

    Best-effort and never raises: the sign-off is already recorded, so a missing
    or failing hand-off endpoint must not fail the request. A no-op unless
    ``[deliverables] handoff_webhook`` is configured."""
    try:
        from maverick import webhooks
        from maverick.deliverable import render_deliverable
        rendered = render_deliverable(_goal_shape(g.domain), g.result)
        table = ({"headers": rendered.table.headers, "rows": rendered.table.rows}
                 if rendered.table else None)
        # Keep the outbound payload aligned with the reviewed artifact.
        # Structured table deliverables render only the parsed cells in the
        # dashboard, so do not include surrounding raw model text that the
        # reviewer did not approve. Prose fallbacks carry the rendered prose.
        webhooks.fire_deliverable_handoff({
            "goal_id": g.id,
            "domain": g.domain,
            "title": g.title,
            "shape": rendered.shape,
            "decided_by": decided_by,
            "table": table,
            "result": rendered.prose,
        })
    except Exception:  # pragma: no cover -- hand-off is best-effort
        log.warning("deliverable hand-off failed for goal %s", getattr(g, "id", "?"))


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
    rendered = render_deliverable(_goal_shape(g.domain), g.result)
    if rendered.table is None:
        raise HTTPException(status_code=404, detail="no tabular deliverable to export")
    # Gated deliverables must carry an approved human sign-off before they can be
    # exported (checked after the no-table 404 so an empty deliverable still 404s).
    gate = _goal_gate(g.domain)
    if gate is not None:
        signoff = w.signoff_for(goal_id)
        if signoff is None or signoff.get("decision") != "approved":
            raise HTTPException(
                status_code=403,
                detail=f"{gate} sign-off is required before exporting this deliverable",
            )
    buf = _io.StringIO()
    writer = csv.writer(buf)
    # Neutralize spreadsheet formulas so an opened CSV can't execute injected
    # formula cells (=, +, -, @) in Excel/Sheets.
    writer.writerow([_csv_formula_safe(cell) for cell in rendered.table.headers])
    writer.writerows(
        [_csv_formula_safe(cell) for cell in row] for row in rendered.table.rows
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


# Default share-link lifetime (7 days). Operators revoke early from the goal page.
_SHARE_TTL_SECONDS = 7 * 24 * 3600


@router.post("/goals/{goal_id}/share", status_code=201)
async def create_goal_share(request: Request, goal_id: int) -> dict:
    """Mint a read-only share link to a goal's deliverable (default 7-day
    expiry). Operator role + goal access. The clear token is returned ONCE --
    only its hash is stored, so it can't be re-fetched later, only revoked."""
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    require_permission(request, "operate")
    if g.status != "done":
        raise HTTPException(
            status_code=409,
            detail="only a finished deliverable can be shared",
        )
    gate = _goal_gate(g.domain)
    signoff = w.signoff_for(goal_id)
    if gate is not None and (
        signoff is None or signoff.get("decision") != "approved"
    ):
        raise HTTPException(
            status_code=403,
            detail=f"{gate} sign-off is required before sharing this deliverable",
        )
    link_id, token = w.create_share_link(
        goal_id, created_by=caller_principal(request) or "", ttl_seconds=_SHARE_TTL_SECONDS)
    url = str(request.base_url).rstrip("/") + "/share/" + token
    return {"id": link_id, "url": url}


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


@router.get("/plugins")
async def list_plugins() -> dict:
    """Discovered + allow-listed plugins, broken out by kind."""
    try:
        from maverick.plugins import _allowed_plugin_names, _entry_points
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"plugin discovery failed: {e}") from e
    allow = _allowed_plugin_names()
    out: dict[str, list[dict]] = {
        "tools": [], "channels": [], "skills": [], "personas": [],
    }
    for kind, group in (
        ("tools",    "maverick.tools"),
        ("channels", "maverick.channels"),
        ("skills",   "maverick.skills"),
        ("personas", "maverick.personas"),
    ):
        try:
            for ep in _entry_points(group):
                out[kind].append({
                    "name": ep.name,
                    "module": getattr(ep, "value", str(ep)),
                    "enabled": allow is None or ep.name in allow,
                })
        except Exception:
            continue
    return {"plugins": out, "allowlist_active": allow is not None}


@router.get("/mcp")
async def list_mcp_servers() -> dict:
    """Configured MCP servers from ~/.maverick/config.toml."""
    try:
        from maverick.config import load_config
        cfg = (load_config() or {}).get("mcp_servers") or {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"config read failed: {e}") from e
    return {
        "servers": [
            {"name": name, "command": s.get("command"), "args": s.get("args", [])}
            for name, s in cfg.items()
        ],
    }


@router.get("/tools")
async def list_tools() -> dict:
    """Tools the agent currently has registered (post-ACL, post-rate-limit)."""
    try:
        from maverick.sandbox import build_sandbox
        from maverick.tools import base_registry
        wm = _world()  # honor the configured backend (SQLite or Postgres)
        sb = build_sandbox()
        reg = base_registry(world=wm, sandbox=sb)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"registry build failed: {e}") from e
    from maverick.safety.tool_risk import risk_map
    tools = reg.all()
    risks = risk_map([t.name for t in tools])
    return {
        "tools": [
            {"name": t.name, "description": (t.description or "")[:200],
             "risk": risks.get(t.name, "medium")}
            for t in tools
        ],
    }


# ---- schedules: arm a saved template (or a prompt) to run on a cron ----------
# A schedule enqueues a recurring "start_goal" job (worker.py) that mints a fresh
# goal on every fire. Nothing runs until `maverick worker` drains the queue. The
# mutating routes are gated by [features] scheduling (like pack/role editing); the
# read-only list is always available.


def _require_scheduling() -> None:
    from maverick.config import get_features
    if not get_features().get("scheduling", True):
        raise HTTPException(
            status_code=403,
            detail=("scheduling is disabled ([features] scheduling = false). "
                    "Re-enable it in config, or use `maverick schedule` on the host."),
        )


def _schedule_out(job) -> ScheduleOut:
    p = job.payload or {}
    return ScheduleOut(
        id=job.id,
        cron=str(p.get("__cron__") or ""),
        kind=job.kind,
        title=(str(p.get("title") or p.get("text") or ""))[:200],
        next_run=job.run_at,
        schedule_id=str(p.get("schedule_id") or ""),
    )


@router.get("/schedules")
async def list_schedules(request: Request) -> dict:
    """Armed recurring schedules: pending cron jobs in the worker queue.

    Owner-scoped like ``delete_schedule``: an authenticated non-admin sees only
    the schedules stamped with their own principal (``create_schedule`` records
    it in ``payload['owner']``), so one tenant can't enumerate another's
    schedule titles. Admin / auth-off callers see all."""
    from maverick.job_queue import JobQueue
    owner = goal_owner_filter(request)
    jobs = [j for j in JobQueue().list(status="pending") if (j.payload or {}).get("__cron__")]
    if owner is not None:
        jobs = [j for j in jobs if (j.payload or {}).get("owner") == owner]
    jobs.sort(key=lambda j: j.run_at)
    return {"schedules": [_schedule_out(j).model_dump() for j in jobs]}


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
async def create_schedule(request: Request, payload: ScheduleIn) -> ScheduleOut:
    require_permission(request, "operate")
    _require_scheduling()
    from maverick.scheduler import CronError, next_run, schedule_cron
    cron = (payload.cron or "").strip()
    try:
        next_run(cron)  # validate up front; CronError -> 400
    except CronError as e:
        raise HTTPException(status_code=400, detail=f"bad cron expression: {e}") from e
    # Resolve the goal text: render a saved template (with params), or a prompt.
    title = (payload.title or "").strip()
    if payload.template:
        from maverick.templates import load_template
        try:
            tpl = load_template(payload.template)
            rtitle, body = tpl.render(**(payload.params or {}))
        except ValueError as e:           # unknown template / missing params
            raise HTTPException(status_code=400, detail=str(e)) from e
        except FileNotFoundError as e:
            # Don't echo the raw error: its str() carries the absolute on-disk
            # template path. Reflect the caller's own template name instead; the
            # original (with path) stays chained for server-side logs.
            raise HTTPException(
                status_code=404, detail=f"template not found: {payload.template!r}"
            ) from e
        text, title = body, (title or rtitle)
    else:
        text = (payload.text or "").strip()
        if not text:
            raise HTTPException(
                status_code=400, detail="provide a template or text to schedule")
    title = (title or text)[:200]
    # A stable id carried in the payload across cron re-arms (each occurrence is
    # a fresh job with a new id), so the worker can stamp provenance and the
    # Automations page can group this schedule's run history.
    from uuid import uuid4
    schedule_id = uuid4().hex
    job_payload = {"text": text, "title": title, "__cron__": cron,
                   "schedule_id": schedule_id}
    owner = caller_principal(request) or ""
    if owner:
        job_payload["owner"] = owner
    allowed_suites = caller_suites(request)
    if allowed_suites is not None:
        job_payload["allowed_suites"] = sorted(allowed_suites)
    user_id = execution_user_id_from_request(request)
    if user_id:
        job_payload["channel"] = "api"
        job_payload["user_id"] = user_id

    from maverick.job_queue import JobQueue
    job_id, run_at = schedule_cron(
        JobQueue(), cron, "start_goal", job_payload,
    )
    return ScheduleOut(id=job_id, cron=cron, kind="start_goal", title=title,
                       next_run=run_at, schedule_id=schedule_id)


@router.delete("/schedules/{job_id}")
async def delete_schedule(request: Request, job_id: int) -> dict:
    require_permission(request, "operate")
    _require_scheduling()
    from maverick.job_queue import JobQueue
    q = JobQueue()
    # Owner scoping, mirroring the goal routes: an authenticated non-admin may
    # cancel only schedules stamped with their own principal (create_schedule
    # records it in payload["owner"]). 404, not 403, so a cross-user probe
    # can't distinguish "exists but not yours" from "does not exist".
    owner = goal_owner_filter(request)
    if owner is not None:
        job = q.get(job_id)
        if job is None or (job.payload or {}).get("owner") != owner:
            raise HTTPException(
                status_code=404, detail="no pending schedule with that id")
    if not q.cancel(job_id):
        raise HTTPException(
            status_code=404, detail="no pending schedule with that id")
    return {"cancelled": job_id}


# ---- triggers: bind a saved template to an inbound webhook (POST /webhook/run)
# These routes MANAGE triggers (dashboard-authed, operate-gated, feature-knobbed).
# The inbound firing route lives in app.py (/webhook/run) and authenticates with
# its own HMAC signature -- exactly like /webhook/start, but strictly narrower:
# it runs only an operator-registered template, never arbitrary text.

_WEBHOOK_RUN_PATH = "/webhook/run"
_IMPORT_MAX_DEFINITIONS = 25
_IMPORT_MAX_DEFINITION_BYTES = 64_000
_IMPORT_MAX_TOTAL_DEFINITION_BYTES = 512_000
_IMPORT_MAX_RENDERED_BODY_CHARS = 16_000


def _definition_size(raw: dict) -> int:
    return len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _bounded_import_definitions(raws: list[dict]) -> list[dict]:
    if len(raws) > _IMPORT_MAX_DEFINITIONS:
        raise HTTPException(
            status_code=413,
            detail=f"too many import definitions (max {_IMPORT_MAX_DEFINITIONS})",
        )
    total = 0
    for raw in raws:
        size = _definition_size(raw)
        if size > _IMPORT_MAX_DEFINITION_BYTES:
            raise HTTPException(
                status_code=413,
                detail=("import definition is too large "
                        f"(max {_IMPORT_MAX_DEFINITION_BYTES} bytes)"),
            )
        total += size
        if total > _IMPORT_MAX_TOTAL_DEFINITION_BYTES:
            raise HTTPException(
                status_code=413,
                detail=("import definitions are too large "
                        f"(max {_IMPORT_MAX_TOTAL_DEFINITION_BYTES} bytes total)"),
            )
    return raws


def _ensure_import_body_size(body: str) -> None:
    if len(body) > _IMPORT_MAX_RENDERED_BODY_CHARS:
        raise HTTPException(
            status_code=413,
            detail=("rendered import template is too large "
                    f"(max {_IMPORT_MAX_RENDERED_BODY_CHARS} characters)"),
        )


def _require_triggers() -> None:
    from maverick.config import get_features
    if not get_features().get("triggers", True):
        raise HTTPException(
            status_code=403,
            detail=("triggers are disabled ([features] triggers = false). "
                    "Re-enable it in config to manage inbound webhook triggers."),
        )


def _inbound_secret_set() -> bool:
    from maverick.webhooks import inbound_secret
    return bool(inbound_secret())


@router.get("/triggers")
async def list_triggers_endpoint(request: Request) -> dict:
    """Registered inbound webhook triggers (read-only; always available).

    Owner-scoped: an authenticated non-admin sees only the triggers it
    registered, so one tenant can't enumerate another's trigger names, bound
    templates, and default param values. Admin / auth-off callers see all."""
    from maverick_dashboard import triggers_store
    triggers = triggers_store.list_triggers()
    owner = goal_owner_filter(request)
    if owner is not None:
        triggers = [t for t in triggers if t.get("owner", "") == owner]
    return {
        "triggers": triggers,
        "webhook_url": _WEBHOOK_RUN_PATH,
        "secret_configured": _inbound_secret_set(),
    }


@router.post("/triggers", response_model=TriggerOut, status_code=201)
async def create_trigger(request: Request, payload: TriggerIn) -> TriggerOut:
    require_permission(request, "operate")
    _require_triggers()
    if bool(payload.template) == bool(payload.flow):
        raise HTTPException(
            status_code=400, detail="set exactly one of 'template' or 'flow'")
    target_flow = None
    target_flow_release = None
    target_template = None
    if payload.flow:
        # A flow target: the flow engine must be on and the flow must exist, so
        # a trigger can't be armed against a missing graph.
        _require_flows()
        from maverick.flow import store as flow_store
        draft = flow_store.load_flow(payload.flow)
        if draft is None:
            raise HTTPException(
                status_code=404, detail=f"flow not found: {payload.flow!r}")
        _assert_flow_access(request, draft)
        try:
            published = flow_store.load_published_bundle(payload.flow)
        except flow_store.FlowSnapshotError as exc:
            raise HTTPException(
                status_code=409, detail="published flow release is unavailable"
            ) from exc
        if published is None:
            raise HTTPException(
                status_code=409, detail="publish the flow before arming a trigger"
            )
        target_flow, target_flow_release = published
    else:
        # Validate now: the template must exist and render with the given
        # defaults, so a trigger can't be armed against a missing/incompatible
        # template.
        from maverick.templates import load_template
        try:
            target_template = load_template(payload.template)
            target_template.render(**(payload.params or {}))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except FileNotFoundError as e:
            # Reflect the caller's template name, not the raw error (it carries
            # the absolute on-disk path). Original stays chained for server logs.
            raise HTTPException(
                status_code=404, detail=f"template not found: {payload.template!r}"
            ) from e
    from maverick_dashboard import triggers_store
    # Owner-scope the write like list/delete: a non-admin must not overwrite
    # (hijack) a trigger registered by another tenant just by reusing its slug.
    # set_trigger keys by slugify(name), so check the same slug for a foreign
    # owner before replacing it.
    default_name = payload.name or payload.template or payload.flow
    owner_filter = goal_owner_filter(request)
    if owner_filter is not None:
        existing = triggers_store.get_trigger(triggers_store.slugify(default_name))
        if existing is not None and existing.get("owner", "") != owner_filter:
            raise HTTPException(
                status_code=409, detail="a trigger with that name already exists")
    try:
        from maverick.paths import current_tenant_id

        rec = triggers_store.set_trigger(
            default_name, payload.template, payload.params or {},
            owner=durable_automation_owner(request), flow=payload.flow,
            tenant=current_tenant_id() or "",
            flow_owner=(target_flow.owner if target_flow is not None else ""),
            flow_revision=(
                str(target_flow_release["release_id"])
                if target_flow_release is not None else ""
            ),
            template_snapshot=(
                triggers_store.snapshot_from_template(target_template)
                if target_template is not None
                else None
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return TriggerOut(
        name=rec["name"], template=rec["template"], flow=rec.get("flow") or "",
        params=rec["params"],
        webhook_url=_WEBHOOK_RUN_PATH, secret_configured=_inbound_secret_set(),
    )


@router.delete("/triggers/{name}")
async def delete_trigger_endpoint(request: Request, name: str) -> dict:
    require_permission(request, "operate")
    _require_triggers()
    from maverick_dashboard import triggers_store
    # Owner-scope like schedules: a non-admin may delete only its own triggers.
    # The ownership check happens atomically inside delete_trigger (one locked
    # read, no TOCTOU). 404 (not 403) so a cross-tenant probe can't distinguish
    # "exists but not yours" from "does not exist".
    if not triggers_store.delete_trigger(name, owner=goal_owner_filter(request)):
        raise HTTPException(status_code=404, detail="no trigger with that name")
    return {"deleted": name}


# ---- event triggers: fire a saved template when a source has new items -------
# The POLL half of workflow automation (the push half is /webhook/run).
# Management is dashboard-authed + operate-gated + feature-knobbed; a scheduled
# POST /event-triggers/poll drains each source and fires per new event.


def _require_event_triggers() -> None:
    from maverick.automation_events import enabled
    if not enabled():
        raise HTTPException(
            status_code=403,
            detail=("Event triggers aren't enabled on this server yet — an "
                    "administrator can switch them on (enable [event_triggers] "
                    "in the server configuration, or set MAVERICK_EVENT_TRIGGERS=1)."),
        )


def _event_trigger_out(rec: dict) -> EventTriggerOut:
    return EventTriggerOut(
        name=rec["name"], template=rec["template"], flow=rec.get("flow") or "",
        source=rec["source"],
        config=rec.get("config") or {}, params=rec.get("params") or {},
        cursor=rec.get("cursor") or "",
        interval_seconds=int(rec.get("interval_seconds") or 0),
    )


def _owned_event_triggers(request: Request) -> list[dict]:
    """Event triggers visible to the caller: their own when auth is on, all of
    them for auth-off/admin (``goal_owner_filter`` returns None)."""
    from maverick.paths import current_tenant_id

    from maverick_dashboard import event_triggers_store

    tenant = current_tenant_id() or ""
    triggers = event_triggers_store.list_triggers()
    triggers = [t for t in triggers if t.get("tenant", "") == tenant]
    owner = goal_owner_filter(request)
    if owner is not None:
        triggers = [t for t in triggers if t.get("owner", "") == owner]
    return triggers


@router.get("/event-triggers")
async def list_event_triggers_endpoint(request: Request) -> dict:
    """Registered polled event triggers (owner-scoped) + the available sources."""
    from maverick.automation_events import available_sources
    from maverick.automation_events import enabled as _ev_enabled

    triggers = _owned_event_triggers(request)
    return {
        "triggers": [_event_trigger_out(t).model_dump() for t in triggers],
        "sources": available_sources(),
        "enabled": _ev_enabled(),
    }


@router.post("/event-triggers", response_model=EventTriggerOut, status_code=201)
async def create_event_trigger(request: Request, payload: EventTriggerIn) -> EventTriggerOut:
    require_permission(request, "operate")
    _require_event_triggers()
    # Sources that read shared credentials and then connect to caller-supplied
    # endpoints must be ADMIN-only. Otherwise a low-priv operator could name a
    # shared secret (OAuth provider or process env var) and exfiltrate it to
    # their own host during polling.
    cfg = payload.config or {}
    reads_env_secret = payload.source == "imap_email" and cfg.get("password_env")
    if cfg.get("provider") or reads_env_secret:
        require_permission(request, "admin")
    from maverick.automation_events import EventSourceError, get_source
    try:
        get_source(payload.source)   # a registered source?
    except EventSourceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Exactly one target: a template (rendered into a goal) OR a flow (which runs
    # the graph with the event as its data).
    template, flow = (payload.template or "").strip(), (payload.flow or "").strip()
    if bool(template) == bool(flow):
        raise HTTPException(status_code=400,
                            detail="set exactly one of 'template' or 'flow'")
    target_flow = None
    target_flow_release = None
    target_template = None
    if flow:
        from maverick import flow as flow_mod
        from maverick.flow import store as flow_store
        if not flow_mod.enabled():
            raise HTTPException(
                status_code=403,
                detail=("Flows aren't enabled on this server yet — an "
                        "administrator can switch on the flow engine ([flows] "
                        "in the server configuration, or MAVERICK_FLOWS=1)."))
        draft = flow_store.load_flow(flow)
        if draft is None:
            raise HTTPException(status_code=404, detail=f"flow not found: {flow!r}")
        _assert_flow_access(request, draft)
        try:
            published = flow_store.load_published_bundle(flow)
        except flow_store.FlowSnapshotError as exc:
            raise HTTPException(
                status_code=409, detail="published flow release is unavailable"
            ) from exc
        if published is None:
            raise HTTPException(
                status_code=409, detail="publish the flow before arming a trigger"
            )
        target_flow, target_flow_release = published
    else:
        # The template must EXIST, but -- unlike a webhook trigger -- we don't
        # require it to render with the current defaults: each polled event fills
        # the declared params at fire time.
        from maverick.templates import load_template
        try:
            target_template = load_template(template)
        except FileNotFoundError as e:
            raise HTTPException(
                status_code=404, detail=f"template not found: {template!r}") from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    from maverick.paths import current_tenant_id

    from maverick_dashboard import event_triggers_store

    default_name = payload.name or template or flow
    tenant = current_tenant_id() or ""
    owner_filter = goal_owner_filter(request)
    if owner_filter is not None:
        existing = event_triggers_store.get_trigger(
            event_triggers_store.slugify(default_name), tenant=tenant)
        if existing is not None and existing.get("owner", "") != owner_filter:
            raise HTTPException(
                status_code=409, detail="an event trigger with that name already exists")
    try:
        rec = event_triggers_store.set_trigger(
            default_name, template, payload.source,
            config=payload.config or {}, params=payload.params or {},
            owner=durable_automation_owner(request), flow=flow,
            flow_owner=(target_flow.owner if target_flow is not None else ""),
            flow_revision=(
                str(target_flow_release["release_id"])
                if target_flow_release is not None else ""
            ),
            template_snapshot=(
                event_triggers_store.snapshot_from_template(target_template)
                if target_template is not None
                else None
            ),
            interval_seconds=payload.interval_seconds,
            tenant=tenant)   # bind OAuth vault reads to this tenant
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _event_trigger_out(rec)


@router.delete("/event-triggers/{name}")
async def delete_event_trigger_endpoint(request: Request, name: str) -> dict:
    require_permission(request, "operate")
    _require_event_triggers()
    from maverick.paths import current_tenant_id

    from maverick_dashboard import event_triggers_store

    if not event_triggers_store.delete_trigger(
        name,
        owner=goal_owner_filter(request),
        tenant=current_tenant_id() or "",
    ):
        raise HTTPException(status_code=404, detail="no event trigger with that name")
    return {"deleted": name}


@router.post("/event-triggers/poll")
async def poll_event_triggers(request: Request, bg: BackgroundTasks, name: str = "") -> dict:
    """Poll event sources and fire the bound template for each new item.

    Owner-scoped: a caller drains only its own triggers. Each source's cursor
    advances so an item fires at most once; the per-poll event count is bounded
    by the engine. A broken template still advances the cursor so it can't
    re-fire a backlog forever. This is the on-demand path (the "Poll now" button
    or an external cron); the app also polls on a background tick automatically
    when the feature is on."""
    require_permission(request, "operate")
    _require_event_triggers()
    from maverick.paths import current_tenant_id

    from maverick_dashboard import event_poll
    from maverick_dashboard.app import _run_trigger_goal_in_tenant

    principal = caller_principal(request) or ""
    tenant = current_tenant_id() or ""
    triggers = _owned_event_triggers(request)
    if name:
        triggers = [t for t in triggers if t["name"] == name]

    def _fire(goal_id: int) -> None:
        goal = _world().get_goal(goal_id)
        if goal is None:
            raise RuntimeError("event-created goal is unavailable")
        owner, user_id, allowed_suites = stored_automation_identity(goal.owner)
        bg.add_task(
            _run_trigger_goal_in_tenant,
            goal_id,
            None,
            None,
            tenant=tenant,
            owner=owner,
            allowed_suites=allowed_suites,
        )

    def _fire_flow(flow_id: str, data: dict, owner: str, idem_key: str = "",
                   origin: str = "manual", expected_revision: str = "") -> str:
        # Flow targets go through the durable queue (same as the background tick),
        # so a flow run gets retry + a trackable run id even on the on-demand path.
        # The idempotency key dedups a re-delivered event; origin records the trigger.
        from maverick_dashboard import automation_queue
        identity_owner, user_id, allowed_suites = stored_automation_identity(owner)

        return automation_queue.enqueue_published_flow_run(
            flow_id,
            data,
            identity_owner,
            idem_key,
            origin,
            channel="api" if user_id else None,
            user_id=user_id,
            allowed_suites=allowed_suites,
            expected_revision=expected_revision,
        )

    results = await run_in_threadpool(
        lambda: event_poll.poll_and_fire(_world, triggers, principal=principal,
                                         fire=_fire, fire_flow=_fire_flow))
    return {"polled": len(results), "triggers": results}


@router.get("/event-triggers/history")
async def event_trigger_history(request: Request, name: str = "", limit: int = 50) -> dict:
    """Recent firings + errors for event triggers (owner-scoped). Complements
    ``/automation-runs?kind=event`` (which shows the goals a trigger spawned)
    with the no-goal outcomes: poll failures, missing templates, skipped events."""
    require_permission(request, "operate")
    _require_event_triggers()
    from maverick.paths import current_tenant_id

    from maverick_dashboard import trigger_events_store

    owner = goal_owner_filter(request)
    rows = trigger_events_store.history(
        name=name or None,
        owner=owner,
        tenant=current_tenant_id() or "",
        limit=max(1, min(1000, limit)),
    )
    return {"history": rows}


@router.get("/event-triggers/outcomes")
async def event_trigger_outcomes(request: Request) -> dict:
    """Per-trigger goal-outcome counts (done / failed / blocked / active ...).

    Closes the attribution loop the firing history can't: a trigger that fires a
    *bad* goal shows up here as a rising ``failed``/``blocked`` count, so an
    operator can see which automations produce work that doesn't land -- and the
    same failed goals are grounded into the learning loop by the run itself.
    Sourced from the goal-origin join (``origin_status_counts('event', name)``)."""
    require_permission(request, "operate")
    _require_event_triggers()
    w = _world()
    outcomes = {
        t["name"]: w.origin_status_counts("event", t["name"])
        for t in _owned_event_triggers(request)
    }
    return {"outcomes": outcomes}


# ---- automation import: pull clients' existing automations into Lightwork -----


def _require_automation_import() -> None:
    from maverick.automation_import import enabled
    if not enabled():
        raise HTTPException(
            status_code=403,
            detail=("Automation import isn't enabled on this server yet — an "
                    "administrator can switch it on (enable [automation_import] "
                    "in the server configuration, or set MAVERICK_AUTOMATION_IMPORT=1)."),
        )


@router.get("/import/sources")
async def import_sources_endpoint() -> dict:
    """Automation platforms Lightwork can import from + each one's mode."""
    from maverick.automation_import import available_sources, get_importer
    sources = []
    for s in available_sources():
        imp = get_importer(s)
        sources.append({
            "source": s,
            "mode": "definition-import" if imp.can_fetch_definitions else "connect-and-trigger",
        })
    return {"sources": sources}


@router.post("/import/run", response_model=ImportRunOut)
async def import_run_endpoint(  # noqa: C901 - staged importer intentionally explicit
    request: Request, payload: ImportRunIn
) -> ImportRunOut:
    require_permission(request, "operate")
    _require_automation_import()
    from maverick.automation_import import ImporterError, get_importer, materialize, translate_all

    try:  # validate the source name -> 400, not a scrubbed 500
        get_importer(payload.source)
    except ImporterError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if payload.as_flows:
        _require_flows()          # importing to graphs needs the flow engine

    # Definitions from the request (offline/connect) or a live fetch (env creds).
    if payload.definitions is not None:
        raws = _bounded_import_definitions([
            d for d in payload.definitions if isinstance(d, dict)
        ])
    else:
        try:
            fetched = await run_in_threadpool(get_importer(payload.source).fetch)
        except ImporterError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        raws = _bounded_import_definitions([
            d for d in fetched if isinstance(d, dict)
        ])

    automations = translate_all(payload.source, raws)
    if not automations:
        raise HTTPException(
            status_code=400,
            detail=f"no importable automations found from {payload.source!r}",
        )
    # translate_all drops malformed/unsupported definitions (logging each). It's
    # 1:1, so the shortfall is the count that silently failed -- surface it so a
    # partial import doesn't read as a clean success.
    skipped = len(raws) - len(automations)

    # Resolve schedule activation: an explicit request field wins; when the UI
    # omits it (None), fall back to the [automation_import] create_schedules
    # config knob (previously a dead setting -- nothing read it). Off by default.
    activate = payload.activate_schedules
    if activate is None:
        from maverick.config import get_automation_import
        activate = bool(get_automation_import().get("create_schedules", False))
    queue = None
    if activate and not payload.dry_run:
        from maverick.job_queue import JobQueue
        queue = JobQueue()

    from maverick.config import get_features
    from maverick.paths import current_tenant_id
    triggers_on = get_features().get("triggers", True)
    from maverick_dashboard import triggers_store
    # Prefer the authenticated request principal explicitly, then use the
    # durable fallback for auth modes (for example a static dashboard token)
    # that intentionally have no end-user subject.  Keeping the request-bound
    # lookup here also ensures imported schedules retain the same identity as
    # schedules authored through the regular dashboard endpoint.
    owner = caller_principal(request) or durable_automation_owner(request)
    user_id = execution_user_id_from_request(request)
    channel = "api" if user_id else None

    results: list[dict] = []
    for a in automations:
        _, body = a.render()
        _ensure_import_body_size(body)
        res = materialize(
            a,
            save=not payload.dry_run,
            queue=queue,
            owner=owner,
            channel=channel,
            user_id=user_id,
        )
        webhook_trigger = None
        # Wire the inbound webhook trigger when asked and the automation is
        # webhook-triggered (the one step the CLI can't do: triggers_store is
        # dashboard-side). Respect the [features] triggers gate -- if the
        # operator disabled triggers, don't silently create ones that bypass
        # that decision; import the template and say so.
        sug = res.suggested_trigger or {}
        want_webhook = (payload.create_webhook_triggers and not payload.dry_run
                        and res.created_template and sug.get("kind") == "webhook")
        if want_webhook and not triggers_on:
            res.notes.append("webhook trigger not created: [features] triggers is off")
        elif want_webhook:
            # Default params for the trigger come from the recovered automation
            # (sug["params"]); validate the template renders with them before
            # arming so we don't create a trigger that always 400s at fire time
            # on a required param the import couldn't supply.
            sug_params = sug.get("params")
            if not isinstance(sug_params, dict):
                sug_params = {}
            from maverick.templates import load_template
            imported_template = None
            try:
                imported_template = load_template(res.template_name)
                imported_template.render(**sug_params)
            except (ValueError, FileNotFoundError) as e:
                res.notes.append(
                    f"webhook trigger not created: template needs param values "
                    f"the import can't supply ({e}); create it in the builder")
            else:
                try:
                    rec = triggers_store.set_trigger(
                        res.template_name,
                        res.template_name,
                        sug_params,
                        owner=owner,
                        tenant=current_tenant_id() or "",
                        template_snapshot=triggers_store.snapshot_from_template(
                            imported_template
                        ),
                    )
                    webhook_trigger = rec["name"]
                except ValueError as e:
                    res.notes.append(f"could not create webhook trigger: {e}")
        flow_id = None
        fidelity: list[dict] = []
        if payload.as_flows:
            # Lower to a real graph too: structure preserved where the source
            # exposes it, each step's disposition logged -- the migration is
            # never silently lossy.
            from maverick.automation_import.to_flow import to_flow_with_report
            from maverick.flow import store as flow_store
            flow_graph, fidelity = to_flow_with_report(a)
            if owner:
                flow_graph.owner = owner
            flow_id = flow_graph.id
            flow_errors = flow_graph.validate()
            if flow_errors:
                res.notes.append(
                    "flow preview was not saved: " + "; ".join(flow_errors[:5]))
                if not payload.dry_run:
                    flow_id = None
            elif not payload.dry_run:
                try:
                    # Imports are creates, not implicit replacements. A repeated
                    # source name must never overwrite a graph a human may have
                    # edited since the first import.
                    flow_store.save_flow(flow_graph, expected_version=0)
                except flow_store.FlowVersionConflict:
                    res.notes.append(
                        f"flow {flow_graph.id!r} already exists; left it unchanged"
                    )
                    flow_id = None
        results.append({
            "source": a.source, "name": a.name, "template": res.template_name,
            "created": res.created_template, "trigger": a.trigger.kind,
            "webhook_trigger": webhook_trigger, "schedule": res.schedule,
            "tools": res.tool_hints, "notes": res.notes,
            "flow": flow_id, "fidelity": fidelity,
        })

    return ImportRunOut(
        imported=results,
        dry_run=payload.dry_run,
        webhook_url=_WEBHOOK_RUN_PATH,
        secret_configured=_inbound_secret_set(),
        skipped=skipped,
    )


# ---- automation run history (provenance): goals a schedule/trigger spawned ---
# Read-only, behind the dashboard middleware like the other GETs. Powers the
# "last N runs · X done / Y failed" summary on the Automations page.


@router.get("/automation-runs")
async def automation_runs(request: Request, kind: str, ref: str, limit: int = 8) -> dict:
    """Recent goals an automation spawned + a status summary. ``kind`` is
    'schedule', 'trigger', or 'event'; ``ref`` is the schedule_id or trigger name."""
    if kind not in ("schedule", "trigger", "event"):
        raise HTTPException(
            status_code=400, detail="kind must be 'schedule', 'trigger', or 'event'")
    ref = (ref or "").strip()
    if not ref:
        return {"runs": [], "summary": {}}
    w = _world()
    capped = max(1, min(int(limit), 50))
    # Owner-scope: a trigger name (enumerable via GET /triggers) is shared across
    # tenants, so an authenticated non-admin must not read another owner's goal
    # titles/history -- nor the cross-owner aggregate from origin_status_counts.
    # Auth-off / admin keep the original (unscoped) behaviour exactly.
    if goal_owner_filter(request) is None:
        goals = w.goals_for_origin(kind, ref, limit=capped)
        summary = w.origin_status_counts(kind, ref)
    else:
        accessible = [
            g for g in w.goals_for_origin(kind, ref, limit=10_000)
            if can_access_goal(request, g)
        ]
        summary = {}
        for g in accessible:
            summary[g.status] = summary.get(g.status, 0) + 1
        goals = accessible[:capped]
    runs = [
        {"goal_id": g.id, "title": g.title, "status": g.status,
         "created_at": g.created_at}
        for g in goals
    ]
    return {"runs": runs, "summary": summary}


# ---- agents (domain packs): per-client view + override editor ---------------
# GET is always available (read-only roster/inspector). The mutating routes are
# gated behind the [features] pack_editing knob so a governed deployment can
# lock the agent roster; write_override additionally refuses any override whose
# *merged* result fails lint, so editing can never weaken the safety envelope.


def pack_editing_denial(request: Request) -> str | None:
    """Why this caller may NOT edit domain packs (agent playbooks), or ``None``
    if it may. One predicate for both consumers -- the enforce path
    (:func:`_require_pack_editing`) raises it as a 403 detail, and the builder
    page reads ``is None`` to decide whether to offer playbook mode -- so the two
    can't drift. Mirrors the ``*_denial`` pattern used for enterprise egress."""
    principal = caller_principal(request)
    if principal is not None and not is_dashboard_admin(principal):
        return "pack editing requires a dashboard admin"
    from maverick.config import get_features
    if not get_features().get("pack_editing", True):
        return ("pack editing is disabled ([features] pack_editing = false). "
                "Edit override TOML on the host, or re-enable it in config.")
    return None


def _require_pack_editing(request: Request) -> None:
    denial = pack_editing_denial(request)
    if denial is not None:
        raise HTTPException(status_code=403, detail=denial)


@router.get("/agents")
async def list_agents_endpoint(request: Request) -> dict:
    """The agent roster: every pack, flagged by override/workflow status.

    Suite-scoped (job function): a caller with a department grant sees their
    departments' specialists plus generic packs (no suite); unscoped callers
    (auth off / admin / no grant) see the full catalog — matching /departments.
    """
    from maverick.domain_edit import list_agents
    allowed = caller_suites(request)
    agents = list_agents()
    if allowed is not None:
        agents = [a for a in agents
                  if a.get("suite") is None or a.get("suite") in allowed]
    return {"agents": agents}


@router.get("/agents/{name}")
async def get_agent_endpoint(request: Request, name: str) -> dict:
    """The merged pack the agent runs, plus provenance (overridden vs inherited)
    and lint findings -- the payload the editor renders."""
    from maverick.domain import suite_for
    from maverick.domain_edit import resolved_view
    view = resolved_view(name)
    if view is None:
        raise HTTPException(status_code=404, detail=f"no such agent: {name!r}")
    # Job-function scoping: pack config (persona, tool allowlist, risk ceiling)
    # for another department is not this caller's to read. 404 first (catalog
    # existence is public), 403 on grant, mirroring /departments/{key}.
    require_suite(request, suite_for(name))
    return view


def _agent_model(view: dict) -> str:
    """The model a specialist runs on: its pack override if any, else the
    deployment's default agent-role model."""
    models = view.get("models") or {}
    for role in ("agent", "worker", "default"):
        if models.get(role):
            return str(models[role])
    if models:
        return str(next(iter(models.values())))
    from maverick.llm import MODEL_SONNET
    try:
        from maverick.config import get_role_model
        return get_role_model("agent") or MODEL_SONNET
    except Exception:  # pragma: no cover -- config read never breaks the card
        return MODEL_SONNET


@router.get("/agents/{name}/jobcard")
async def agent_jobcard(request: Request, name: str, days: int = 90) -> dict:
    """The Agent Manager card in one call: the specialist's job description
    (role, mission, responsibilities, deliverable, tools, guardrails, risk,
    department), its manager scorecard (runs, cost, value, ROI, success rate),
    and its model's cost tier. Read-only. Suite-scoped like /agents/{name}."""
    from maverick.domain import suite_for
    from maverick.domain_edit import resolved_view
    view = await run_in_threadpool(resolved_view, name)
    if view is None:
        raise HTTPException(status_code=404, detail=f"no such agent: {name!r}")
    require_suite(request, suite_for(name))

    from maverick import agent_scorecard, model_cost_tier
    model = _agent_model(view)
    days = max(1, min(int(days), 730))

    def _score() -> dict:
        return agent_scorecard.for_agent(_world(), name,
                                         window_days=days).to_dict()

    scorecard = await run_in_threadpool(_score)
    output = view.get("output") or {}
    jd = {
        "name": view["name"], "suite": view.get("suite"),
        "role": view.get("description", ""),
        "mission": view.get("persona", ""),
        "responsibilities": view.get("workflow") or [],
        "deliverable": output.get("deliverable", ""),
        "consumers": output.get("consumers") or [],
        "cadence": output.get("cadence", ""),
        "gate": view.get("enforced_gate", ""),
        "tools": view.get("allow_tools") or [],
        "guardrails": view.get("deny_tools") or [],
        "max_risk": view.get("max_risk", ""),
        "knowledge_sources": view.get("knowledge_sources") or [],
    }
    return {"job_description": jd, "scorecard": scorecard,
            "cost_tier": model_cost_tier.band_detail(model)}


@router.get("/model-cost-tiers")
async def list_model_cost_tiers(request: Request) -> dict:
    """The settable cost-tier table: every priced model with its band (Low /
    Medium / High / Very High) and whether it was operator-set."""
    require_permission(request, "view")
    from maverick import model_cost_tier
    return {"bands": list(model_cost_tier.BANDS),
            "models": await run_in_threadpool(model_cost_tier.catalog)}


@router.post("/model-cost-tiers")
async def set_model_cost_tier_endpoint(
    request: Request, body: ModelCostTierIn) -> dict:
    """Set (or clear, with band=null) a model's cost band. Admin-gated: it
    shapes routing economics deployment-wide."""
    require_permission(request, "admin")
    from maverick_dashboard import settings_store
    await run_in_threadpool(settings_store.set_model_cost_tier,
                            body.model, body.band)
    from maverick import config, model_cost_tier
    config.reset_config_cache()
    return model_cost_tier.band_detail(body.model)


# Platform systems an operator can switch on/off INSIDE the app -- the
# replacement for every "edit the server configuration" instruction. Each
# entry is a config section whose `enable` key settings_store can overlay;
# the allowlist keeps the endpoint from becoming a generic config writer.
_FEATURE_SWITCHES: dict = {
    "dreaming": ("Dreaming", "Consolidates completed goals overnight into "
                 "reusable lessons the fleet can recall."),
    "self_improvement": ("Self-improvement", "Proposes and tests its own "
                         "prompt/skill refinements; every change is gated "
                         "and reversible."),
    "self_harness": ("Self-harness", "Re-runs learned skills against known "
                     "outcomes before they are trusted."),
    "fleet_memory": ("Fleet memory", "Shares vetted lessons between agents "
                     "so one agent's fix helps the whole workforce."),
    "rehearsal": ("Rehearsal", "Practices risky operations in a sandbox "
                  "before doing them for real."),
    "flows": ("Flow engine", "Runs the visual flow designer's automations "
              "end to end."),
    "threat_hunt": ("Platform threat hunter", "Defensively scans "
                    "Lightwork's own signed telemetry for anomalies."),
    "env_hunt": ("Environment threat hunter", "Reads your configured "
                 "telemetry sources and flags suspicious activity."),
    "entity_graph": ("Entity graph", "Links vendors, documents, clauses, "
                     "and decisions into one queryable lineage."),
}


@router.get("/features/switches")
async def list_feature_switches(request: Request) -> dict:
    """Every in-app switchable system with its label, plain-language
    description, and current effective state."""
    require_permission(request, "view")
    from maverick import config as _cfg

    def _read() -> list[dict]:
        loaded = _cfg.load_config() or {}
        out = []
        for section, (label, description) in _FEATURE_SWITCHES.items():
            enabled = bool((loaded.get(section) or {}).get("enable", False))
            out.append({"section": section, "label": label,
                        "description": description, "enabled": enabled})
        return out

    return {"switches": await run_in_threadpool(_read)}


@router.post("/features/switches")
async def set_feature_switch(request: Request,
                             body: FeatureSwitchIn) -> dict:
    """Flip one system on or off from inside the app. Admin-gated and
    allowlisted; persists through the same settings overlay the rest of the
    in-app configuration uses -- no server files, no restart."""
    require_global_permission(request, "admin")
    if body.section not in _FEATURE_SWITCHES:
        raise HTTPException(status_code=400,
                            detail=f"unknown system {body.section!r}")
    if body.section in {"threat_hunt", "env_hunt"}:
        from . import security_api
        from .api_schemas import SecuritySuiteConfigIn

        current = await security_api.security_config(request)
        update = SecuritySuiteConfigIn(
            security_ops=current["security_ops"],
            threat_hunt=(bool(body.enabled) if body.section == "threat_hunt"
                         else current["threat_hunt"]),
            env_hunt=(bool(body.enabled) if body.section == "env_hunt"
                      else current["env_hunt"]),
            response_execution=current["response_execution"],
            expected_revision=current["revision"],
        )
        await security_api.update_security_config(request, update)
        return {"section": body.section, "enabled": bool(body.enabled)}
    from maverick_dashboard import settings_store
    await run_in_threadpool(settings_store.set_section_enable, body.section,
                            bool(body.enabled))
    from maverick import config as _cfg
    _cfg.reset_config_cache()
    return {"section": body.section, "enabled": bool(body.enabled)}


@router.post("/copilot")
async def copilot_endpoint(request: Request, payload: CopilotIn) -> dict:
    """One turn of the platform copilot -- the lower-right helper on every
    page. Answers product questions grounded in the page the user is on.
    Provider-key gated; the panel falls back to its built-in tips without
    one. Read-only by design: it explains and points, it never acts."""
    require_permission(request, "view")
    require_provider_or_400(role="orchestrator")
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request, source="copilot")
    msg = (payload.message or "").strip()[:2000]
    if not msg:
        raise HTTPException(status_code=400, detail="empty message")
    page = (payload.page or "").strip()[:200]
    history = [
        {"role": h["role"], "content": str(h["content"])[:2000]}
        for h in (payload.history or [])[-6:]
        if isinstance(h, dict) and h.get("role") in ("user", "assistant")
        and h.get("content")
    ]

    def _turn() -> str:
        from maverick.budget import Budget
        from maverick.llm import LLM, model_for_role
        llm = LLM(model_for_role("orchestrator"))
        budget = Budget(max_dollars=0.25, max_output_tokens=1500,
                        max_tool_calls=0)
        system = (
            "You are the Lightwork copilot -- a concise in-app guide for a "
            "governed AI-workforce platform. The user is on the page "
            f"{page or 'unknown'} of the Lightwork dashboard. Explain what "
            "pages and controls do and point the user to the right place; "
            "keep answers under 120 words, plain language, no code or "
            "config-file instructions (everything is managed inside the "
            "app). You cannot take actions -- when asked to do something, "
            "explain where in the app to do it. If asked something outside "
            "Lightwork, say so briefly."
        )
        resp = llm.complete(
            system=system,
            messages=history + [{"role": "user", "content": msg}],
            budget=budget,
        )
        return (getattr(resp, "text", "") or "").strip()

    try:
        reply = await run_in_threadpool(_turn)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="the copilot's model is unavailable right now") from exc
    return {"reply": reply or "I could not produce an answer -- try the "
                              "chat page for a full conversation."}


@router.post("/agents/{name}/validate")
async def validate_agent_override(
    request: Request, name: str, payload: AgentOverrideIn
) -> dict:
    """Lint the merged result of a proposed override without persisting it."""
    # Same gate as save/delete override: pack editing is admin-only, so the
    # lint helper that previews a pack edit must not be reachable unauthenticated.
    _require_pack_editing(request)
    from maverick.domain_edit import validate_override
    errors, warnings = validate_override(name, payload.model_dump(exclude_unset=True))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


@router.post("/agents/{name}/override")
async def save_agent_override(request: Request, name: str, payload: AgentOverrideIn) -> dict:
    """Persist a tenant override for ``name``. 403 if pack editing is disabled,
    422 if the merged pack fails lint (the override is rejected, not written)."""
    _require_pack_editing(request)
    from maverick.domain_edit import resolved_view, write_override
    try:
        write_override(name, payload.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return resolved_view(name)


@router.delete("/agents/{name}/override")
async def delete_agent_override(request: Request, name: str) -> dict:
    """Drop a tenant override, reverting the agent to its built-in pack."""
    _require_pack_editing(request)
    from maverick.domain_edit import remove_override, resolved_view
    try:
        removed = remove_override(name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    view = resolved_view(name)
    if view is None:
        raise HTTPException(status_code=404, detail=f"no such agent: {name!r}")
    return {"removed": removed, "agent": view}


# ---- JD hiring: map a job description onto the roster / draft a new pack ----
# HR uploads a JD; matching is offline (hybrid router over the full roster,
# custom packs included), drafting rides the same intake pipeline as
# `maverick onboard` (clamped envelope, generated deny-floor), and persisting
# stays the pack editor's existing human-approved save.

def _require_jd_hiring() -> None:
    from maverick.jd_hiring import jd_hiring_enabled
    if not jd_hiring_enabled():
        raise HTTPException(
            status_code=403,
            detail="JD hiring is disabled ([agent_factory] jd_hiring = false)")


def _scoped_jd_matches(request: Request, jd_text: str, k: int) -> dict:
    from maverick.domain import available_domains, suite_for
    from maverick.jd_hiring import match_jd
    # Job-function scoping, mirroring /agents: a department-scoped caller only
    # ranks their departments' specialists plus generic (suite-less) packs.
    # Filter before routing so unauthorized profile text never enters the
    # lexical/embedding index and cannot crowd allowed hits out of top-k.
    allowed = caller_suites(request)
    domains = available_domains()
    if allowed is not None:
        domains = {
            name: profile
            for name, profile in domains.items()
            if (suite := suite_for(name)) is None or suite in allowed
        }
    matches = match_jd(jd_text, k=k, domains=domains)
    return {"matches": [m.to_dict() for m in matches]}


@router.post("/agents/jd/match")
async def match_agents_to_jd(request: Request, payload: JDMatchIn) -> dict:
    """Rank the specialist roster against a pasted job description.

    Deterministic and offline (no LLM): "do we already employ this role?"
    answered against all shipped packs plus tenant custom packs."""
    _require_jd_hiring()
    # First call builds the router index over the ~2k-pack roster; keep the
    # event loop free (same reason the drafters run in the threadpool).
    return await run_in_threadpool(_scoped_jd_matches, request, payload.jd_text, payload.k)


@router.post("/agents/jd/match-from-file")
async def match_agents_to_jd_upload(
    request: Request,
    file: UploadFile = File(...),
    k: int = Form(5),
) -> dict:
    """Upload path: rank the roster against a JD document (.txt/.md, or
    PDF/Word/OpenDocument when the maverick-knowledge parsers are installed)."""
    _require_jd_hiring()
    raw = await file.read(_WORKFLOW_DOC_MAX_BYTES + 1)
    if len(raw) > _WORKFLOW_DOC_MAX_BYTES:
        raise HTTPException(status_code=400, detail="file too large; paste the key parts instead")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = await run_in_threadpool(
            _extract_binary_doc_text, raw, file.filename or "upload")
        if text is None:
            raise HTTPException(
                status_code=400,
                detail=("could not read this file as text — upload a .txt / .md / "
                        ".pdf / .docx job description, or paste it instead"),
            ) from None
    if not text.strip():
        raise HTTPException(status_code=400, detail="the uploaded file was empty")
    return await run_in_threadpool(
        _scoped_jd_matches, request, text, max(1, min(int(k), 20)))


@router.post("/agents/jd/draft")
async def draft_agent_from_jd(request: Request, payload: JDDraftIn) -> dict:
    """Draft (not save) a clamped specialist pack from a job description.

    Uses the intake LLM proposer when a provider is configured (spend-capped
    like workflow drafting), else the deterministic keyless path. The response
    prefills the pack editor; saving remains the editor's existing
    admin-gated, lint-checked override write."""
    _require_jd_hiring()
    # Same gate as the editor this feeds: drafting reveals pack-editing surface.
    _require_pack_editing(request)
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request, source="agent-factory-draft")
    from maverick.jd_hiring import draft_from_jd

    llm = None
    llm_model = None
    budget = None
    from maverick.config import load_config
    from maverick.operator_preflight import _role_configuration_missing

    _, missing = _role_configuration_missing("writer", load_config())
    if not missing:
        try:
            from maverick.budget import Budget
            from maverick.llm import LLM, model_for_role

            llm_model = model_for_role("writer")
            llm = LLM(model=llm_model)
            # A draft is one bounded completion; cap it like workflow drafting
            # so the endpoint can't be spammed into a cost amplifier.
            budget = Budget(max_dollars=0.50)
        except Exception:  # provider misconfig -> deterministic draft, not a 500
            llm = None
            budget = None
    try:
        profile = await run_in_threadpool(
            lambda: draft_from_jd(payload.role_title, payload.jd_text,
                                  industry=payload.industry, llm=llm,
                                  model=llm_model, budget=budget))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "draft": {
            "name": profile.name,
            "description": profile.description,
            "persona": profile.persona,
            "allow_tools": list(profile.allow_tools),
            "deny_tools": list(profile.deny_tools),
            "max_risk": profile.max_risk,
            "refuse": list(profile.refuse),
            "workflow": [
                {"name": s.name, "instruction": s.instruction,
                 "tools": list(s.tools), "gate": s.gate}
                for s in profile.workflow
            ],
        },
        "generated_with_llm": llm is not None,
    }


# ---- roles: per-client editable system-prompt addendum -----------------------
# Same gate pattern as agents: GET is read-only; mutations require role_editing.
# Role model/effort routing is configured elsewhere ([models]/[effort]) and is
# read-only here.


def _require_role_editing(request: Request) -> None:
    principal = caller_principal(request)
    if principal is not None and not is_dashboard_admin(principal):
        raise HTTPException(status_code=403, detail="role editing requires dashboard admin")

    from maverick.config import get_features
    if not get_features().get("role_editing", True):
        raise HTTPException(
            status_code=403,
            detail=("role editing is disabled ([features] role_editing = false). "
                    "Edit roles.toml on the host, or re-enable it in config."),
        )


@router.get("/roles")
async def list_roles_endpoint() -> dict:
    """The core-role roster, flagged by override status."""
    from maverick.role_edit import list_roles
    return {"roles": list_roles()}


@router.get("/roles/{role}")
async def get_role_endpoint(role: str) -> dict:
    """A role's merged view: resolved model/effort, addendum, and provenance."""
    from maverick.role_edit import resolved_role
    view = resolved_role(role)
    if view is None:
        raise HTTPException(status_code=404, detail=f"no such role: {role!r}")
    return view


@router.post("/roles/{role}/override")
async def save_role_override(role: str, payload: RoleOverrideIn, request: Request) -> dict:
    """Persist a role's system-prompt addendum. 403 unless role editing is
    enabled and the caller is an admin (auth-off local mode remains allowed),
    422 if validation fails (unknown role, over-long addendum)."""
    _require_role_editing(request)
    from maverick.role_edit import resolved_role, write_role_override
    try:
        write_role_override(role, payload.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return resolved_role(role)


@router.delete("/roles/{role}/override")
async def delete_role_override(role: str, request: Request) -> dict:
    """Drop a role's override, reverting it to the built-in template."""
    _require_role_editing(request)
    from maverick.role_edit import remove_role_override, resolved_role
    removed = remove_role_override(role)
    view = resolved_role(role)
    if view is None:
        raise HTTPException(status_code=404, detail=f"no such role: {role!r}")
    return {"removed": removed, "role": view}


@router.get("/channels")
async def list_channels() -> dict:
    """Enabled channels from ~/.maverick/config.toml."""
    try:
        from maverick.config import load_config
        cfg = (load_config() or {}).get("channels") or {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"config read failed: {e}") from e
    return {
        "channels": [
            {"name": name, "enabled": bool(c.get("enabled", True))}
            for name, c in cfg.items()
        ],
    }


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


@router.get("/replay/{goal_id}")
async def replay_json(request: Request, goal_id: int) -> dict:
    """Flight-recorder timeline + chain-verification verdict for one run."""
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    from .control_plane import build_replay, evidence_packet
    replay = build_replay(goal_id, window=(g.created_at, g.updated_at))
    return evidence_packet(g, replay)


@router.get("/replay/{goal_id}/evidence")
async def replay_evidence(request: Request, goal_id: int) -> Response:
    """Download the run's evidence packet as a standalone JSON artifact."""
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    from .control_plane import build_replay, evidence_packet
    replay = build_replay(goal_id, window=(g.created_at, g.updated_at))
    body = json.dumps(
        evidence_packet(g, replay), indent=2, ensure_ascii=False, default=str,
    )
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="evidence-goal-{goal_id}.json"',
        },
    )


@router.get("/trust/agents")
async def trust_agents(request: Request) -> dict:
    """The Agent Trust Plane registry: external agents + their tool/risk/budget
    ceilings and lifecycle status (the cross-agent permission graph as JSON)."""
    from .control_plane import trust_overview
    return trust_overview()


@router.post("/trust/agents", status_code=201)
async def trust_agent_register(request: Request, payload: TrustAgentIn) -> dict:
    """Register or replace an external agent from the app (admin).

    Writes the dashboard/CLI-managed overlay (``agent_trust.json``), never the
    operator's config file; a managed entry with the same id overrides the
    config-file one, so admins can also amend hand-edited entries here."""
    require_permission(request, "admin")
    from maverick.agent_trust import AgentTrustError, put_agent
    try:
        agent = put_agent(payload.model_dump())
    except AgentTrustError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "id": agent.id}


@router.post("/trust/agents/{agent_id}/revoke")
async def trust_agent_revoke(request: Request, agent_id: str,
                             payload: TrustRevokeIn) -> dict:
    """Revoke (or restore) a managed external agent (admin). Entries defined in
    the operator's config file can't be flipped here -- register the same id
    from the app first to take it under management."""
    require_permission(request, "admin")
    from maverick.agent_trust import set_revoked
    if not set_revoked(agent_id, payload.revoked):
        raise HTTPException(
            status_code=404,
            detail="this agent is defined in the server's config file, not in "
                   "the app-managed registry. Register the same id here to "
                   "override it, or ask your administrator to edit the config.",
        )
    return {"ok": True, "id": agent_id, "revoked": payload.revoked}


@router.delete("/trust/agents/{agent_id}", status_code=204)
async def trust_agent_remove(request: Request, agent_id: str) -> None:
    """Remove a managed external agent (admin). Config-file entries reappear
    after their managed override is deleted; the same 404 rule applies."""
    require_permission(request, "admin")
    from maverick.agent_trust import remove_agent
    if not remove_agent(agent_id):
        raise HTTPException(
            status_code=404,
            detail="this agent is not in the app-managed registry (it may be "
                   "defined in the server's config file).",
        )


@router.get("/external-agents")
async def external_agents_list(request: Request) -> dict:
    """The bring-your-own-agent roster: platform provenance, ownership,
    ceilings, lifecycle, reported spend, and which credential surfaces each
    agent holds (never token values)."""
    from maverick import external_agents as xa
    return {"enabled": xa.enabled(), "status": xa.status(),
            "agents": xa.roster()}


@router.post("/external-agents", status_code=201)
async def external_agent_enroll(request: Request,
                                payload: ExternalAgentIn) -> dict:
    """Enroll an agent built on another platform (admin): trust entry +
    fleet-memory roster + platform metadata in one call."""
    require_permission(request, "admin")
    from maverick import external_agents as xa
    if not xa.entitled():
        raise HTTPException(
            status_code=403,
            detail="external-agent governance is a paid (Gold) add-on not "
                   "included in this license")
    try:
        out = xa.enroll(
            payload.id, payload.platform, description=payload.description,
            owner=payload.owner, department=payload.department,
            allow_tools=payload.allow_tools, deny_tools=payload.deny_tools,
            max_risk=payload.max_risk, max_dollars=payload.max_dollars,
            max_wall_seconds=payload.max_wall_seconds,
            data_scopes=payload.data_scopes,
            expires_days=payload.expires_days,
            budget_period=payload.budget_period,
            enrolled_by=caller_principal(request) or "admin")
    except xa.ExternalAgentsError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return out


@router.post("/external-agents/{agent_id}/release")
async def external_agent_release(request: Request, agent_id: str) -> dict:
    """Lift an auto-containment and clear the denial window (admin)."""
    require_permission(request, "admin")
    from maverick import external_agents as xa
    if not xa.release(agent_id,
                      released_by=caller_principal(request) or "admin"):
        raise HTTPException(status_code=404,
                            detail=f"agent {agent_id!r} is not enrolled")
    return {"ok": True, "id": agent_id}


@router.post("/external-agents/{agent_id}/reset-budget")
async def external_agent_reset_budget(request: Request,
                                      agent_id: str) -> dict:
    """Zero the active budget meter and clear the over-budget flag (admin).
    Lifetime totals are preserved — this changes what counts against the
    cap, never what was historically reported."""
    require_permission(request, "admin")
    from maverick import external_agents as xa
    if not xa.reset_budget(agent_id,
                           reset_by=caller_principal(request) or "admin"):
        raise HTTPException(status_code=404,
                            detail=f"agent {agent_id!r} is not enrolled")
    return {"ok": True, "id": agent_id}


@router.get("/external-agents/{agent_id}")
async def external_agent_detail(request: Request, agent_id: str) -> dict:
    """One enrolled agent: roster row + its Operating Record footprint
    (spend, recent episodes)."""
    from maverick import external_agents as xa
    try:
        return xa.agent_detail(agent_id)
    except xa.ExternalAgentsError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/external-agents/{agent_id}/credentials")
async def external_agent_mint(request: Request, agent_id: str,
                              payload: ExternalCredentialIn) -> dict:
    """Mint or rotate one per-surface bearer (admin). The token appears in
    THIS response only — it is stored for verification, never for reading.
    With ``[external_agents] mint_approval`` on, the first call parks a
    step-up approval and answers 409 with its id; re-post with
    ``approval_id`` once a decision-maker approves."""
    require_permission(request, "admin")
    from maverick import external_agents as xa
    try:
        token = xa.mint_token(
            agent_id, payload.surface,
            minted_by=caller_principal(request) or "admin",
            mint_approval_id=payload.approval_id)
    except xa.MintApprovalPending as e:
        return JSONResponse(status_code=409, content={
            "detail": str(e), "status": "approval_required",
            "approval_id": e.approval_id})
    except xa.ExternalAgentsError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "id": agent_id, "surface": payload.surface,
            "token": token}


@router.delete("/external-agents/{agent_id}", status_code=204)
async def external_agent_unenroll(request: Request, agent_id: str) -> None:
    """Unenroll (admin): removes the managed trust entry + metadata. The
    fleet-memory roster keeps its history — provenance is never rewritten.
    Prefer revoking (POST /trust/agents/{id}/revoke) to keep the entry."""
    require_permission(request, "admin")
    from maverick import external_agents as xa
    if not xa.unenroll(agent_id):
        raise HTTPException(status_code=404,
                            detail=f"agent {agent_id!r} is not enrolled")


@router.get("/discovery")
async def discovery(request: Request) -> dict:
    """Inventory of governable surfaces: tools (by risk), MCP servers (with
    supply-chain pins), configured providers, channels, and external agents."""
    from .control_plane import discovery_overview
    return discovery_overview()


@router.get("/simulate")
async def simulate(request: Request, surface: str, action: str, target: str = "") -> dict:
    """Dry-run a proposed action: classify its risk + report whether it would be
    gated, without executing it. surface = computer | browser | tool."""
    from .control_plane import simulate_action
    return simulate_action(surface, action, target)


def _build_compliance_packet_body() -> str:
    from .control_plane import compliance_packet

    return json.dumps(compliance_packet(), indent=2, ensure_ascii=False, default=str)


@router.get("/compliance/packet")
async def compliance_packet_download(request: Request) -> Response:
    """Download a one-click compliance evidence bundle (SOC 2 control snapshot +
    GDPR/EU-AI-Act control report + audit-chain verdict) as a JSON artifact."""
    require_permission(request, "audit")
    global _COMPLIANCE_PACKET_CACHE
    now = time.monotonic()
    if (
        _COMPLIANCE_PACKET_CACHE is not None
        and now - _COMPLIANCE_PACKET_CACHE[0] < _COMPLIANCE_PACKET_CACHE_TTL_SECONDS
    ):
        body = _COMPLIANCE_PACKET_CACHE[1]
    else:
        async with _COMPLIANCE_PACKET_LOCK:
            now = time.monotonic()
            if (
                _COMPLIANCE_PACKET_CACHE is not None
                and now - _COMPLIANCE_PACKET_CACHE[0] < _COMPLIANCE_PACKET_CACHE_TTL_SECONDS
            ):
                body = _COMPLIANCE_PACKET_CACHE[1]
            else:
                body = await run_in_threadpool(_build_compliance_packet_body)
                _COMPLIANCE_PACKET_CACHE = (time.monotonic(), body)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="lightwork-compliance-packet.json"',
        },
    )


@router.get("/permissions")
async def permissions() -> dict:
    """Everything the agent is currently allowed to do (read-only)."""
    from maverick_dashboard.app import _permissions_snapshot
    return _permissions_snapshot()


@router.post("/permissions/tools/{name}/disable", status_code=204)
async def disable_tool(request: Request, name: str) -> None:
    """Disable a tool via the dashboard runtime overlay.

    Writes ~/.maverick/runtime-overrides.toml (NOT config.toml), which
    the kernel unions into the deny-list. Takes effect on the next goal
    with no restart.
    """
    require_permission(request, "operate")
    from maverick.runtime_overrides import disable_tool as _disable
    try:
        _disable(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/permissions/tools/{name}/enable", status_code=204)
async def enable_tool(request: Request, name: str) -> None:
    """Clear a dashboard-set tool override.

    Only clears overrides set here; a tool denied in config.toml itself
    stays denied (the response is still 204 — the overlay no longer
    denies it, config does).
    """
    require_permission(request, "operate")
    from maverick.runtime_overrides import enable_tool as _enable
    try:
        _enable(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/approvals")
async def list_approvals(request: Request) -> dict:
    """Pending high-risk actions parked by safety.consent (dashboard mode).

    This is the operators' collaborative supervision queue (an approval has no
    per-user owner and may carry another user's goal content in ``detail``), so
    listing it requires the ``operate`` permission — the same gate as
    approve/deny/claim. Previously any authenticated caller, including a
    view-only user, could read every parked action.
    """
    require_permission(request, "operate")
    w = _world()
    return {
        "approvals": [
            {
                "id": a.id, "action": a.action, "risk": a.risk,
                "scope": a.scope, "detail": a.detail,
                "requested_at": a.requested_at,
                # Collaborative supervision: who is handling this review.
                "claimed_by": getattr(a, "claimed_by", None),
                "claimed_at": getattr(a, "claimed_at", None),
            }
            for a in w.pending_approvals()
        ],
    }


def _supervisor(request: Request) -> str:
    """The acting supervisor's identity for claims/attribution.

    The authenticated principal when auth is on; the shared "operator"
    identity in single-user/no-auth deployments (claims still prevent
    double-handling across that operator's browser tabs)."""
    from .auth import caller_principal
    return caller_principal(request) or "operator"


def _deliver_approval_audit_event(world, event) -> bool:
    """Deliver exactly one queued vote and persist its delivery marker."""
    if event.delivered_at is not None:
        return True

    from maverick.audit import AuditRefused, EventKind, audit_event

    try:
        written = audit_event(
            EventKind.APPROVAL_DECISION,
            approval_event_id=event.event_id,
            approval_id=int(event.approval_id),
            status=event.status,
            decided_by=event.decided_by,
            occurred_at=float(event.created_at),
        )
    except AuditRefused as exc:
        # This is not refusal swallowing: no approval becomes effective. The
        # exact event remains in the durable outbox for a policy/key repair and
        # idempotent retry.
        log.error(
            "approval audit refused; decision remains non-effective "
            "(event_id=%s): %s",
            event.event_id,
            exc,
        )
        return False
    if not written:
        log.error(
            "approval audit write failed; decision remains non-effective "
            "(event_id=%s)",
            event.event_id,
        )
        return False

    try:
        return bool(world.mark_approval_audit_delivered(event.event_id))
    # The signed append may have succeeded while the delivery-marker commit
    # acknowledgement failed. Reconcile instead of returning an error that
    # invites an unsafe, differently-keyed retry.
    # failure-policy: fail_soft_with_audit
    except Exception:
        log.exception(
            "approval audit was written but delivery finalization failed "
            "(event_id=%s)",
            event.event_id,
        )
        try:
            reconciled = world.get_approval_audit_event(event.event_id)
        except Exception:
            return False
        return bool(reconciled and reconciled.delivered_at is not None)


def _audit_approval_decision(world, event) -> bool:
    """Deliver an approval's queued votes in durable occurrence order.

    The world-model outbox is the authority seam: an audit refusal leaves every
    later vote durable but non-effective. A retry uses stable event ids and
    resumes at the oldest undelivered row, so a quorum-reaching vote cannot
    overtake an earlier sign-off in the signed chain.
    """
    try:
        pending = world.pending_approval_audit_events(
            approval_id=event.approval_id,
            limit=1000,
        )
    # The vote/outbox commit already succeeded. A read failure here is an
    # explicit audit-pending response, not a 500 that falsely invites a new
    # decision.
    # failure-policy: fail_soft_with_audit
    except Exception:
        log.exception(
            "approval audit outbox could not be read (event_id=%s)",
            event.event_id,
        )
        return False
    for queued in pending:
        if not _deliver_approval_audit_event(world, queued):
            return False
    try:
        current = world.get_approval_audit_event(event.event_id)
    except Exception:
        return False
    return bool(current and current.delivered_at is not None)


def _record_vote_or_raise(world, approval_id: int, status: str, who: str):
    """Apply one approver's vote, mapping rejection to the right HTTP error.

    Under N-of-M dual control a vote can be refused for segregation-of-duties
    reasons (the requester can't self-approve, or an approver identity is needed)
    -- distinguish that (403) from an unknown/already-decided approval (404)."""
    event_id = world.approval_audit_event_id(approval_id, status, who)
    try:
        event = world.decide_approval_audited(
            approval_id,
            status,
            decided_by=who,
        )
    except Exception as decision_error:
        # A commit acknowledgement can be lost after both the vote and outbox
        # row are durable. The stable id lets us prove that outcome before
        # deciding whether this request failed.
        try:
            event = world.get_approval_audit_event(event_id)
        except Exception as reconcile_error:
            log.exception(
                "approval decision outcome is uncertain (event_id=%s)",
                event_id,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "decision": "outcome_uncertain",
                    "decision_id": event_id,
                    "retry_safe": True,
                },
                headers={
                    "Retry-After": "1",
                    "X-Lightwork-Decision-Id": event_id,
                    "X-Lightwork-Approval-State": "uncertain",
                },
            ) from reconcile_error
        if event is not None and not event.matches(approval_id, status, who):
            log.critical(
                "approval outbox identity mismatch during commit reconciliation "
                "(event_id=%s)",
                event_id,
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "decision": "outbox_integrity_error",
                    "decision_id": event_id,
                    "retry_safe": False,
                },
                headers={
                    "X-Lightwork-Decision-Id": event_id,
                    "X-Lightwork-Approval-State": "integrity_error",
                },
            ) from decision_error
        if event is None:
            raise decision_error
    if event is not None:
        return event

    st = world.approval_state(approval_id)
    if st is None or st.get("status") != "pending":
        raise HTTPException(status_code=404, detail="no such pending approval")
    if world.approval_has_pending_finalization(approval_id):
        raise HTTPException(
            status_code=409,
            detail="a final decision is accepted and awaiting signed audit delivery",
        )
    raise HTTPException(
        status_code=403,
        detail="vote not accepted: the requester can't approve their own request "
               "(segregation of duties), or an approver identity is required for "
               "multi-party approval",
    )


def _approval_decision_response(world, event, audit_delivered: bool) -> Response:
    """Make pending-vs-effective state explicit at the HTTP boundary."""
    try:
        state = world.approval_state(event.approval_id) or {}
    # The decision/outbox transaction has already committed. A diagnostic read
    # failure must not turn that into a misleading 500/retry; fall back to the
    # conservative state derivable from the delivery result.
    # failure-policy: fail_soft_with_audit
    except Exception:
        log.exception(
            "approval state read failed after accepted decision (event_id=%s)",
            event.event_id,
        )
        state = {}
    fallback_status = (
        event.final_status
        if audit_delivered and event.final_status is not None
        else "pending"
    )
    approval_status = str(state.get("status") or fallback_status)
    common_headers = {
        "X-Lightwork-Decision-Id": event.event_id,
        "X-Lightwork-Approval-Audit": (
            "delivered" if audit_delivered else "pending"
        ),
        "X-Lightwork-Approval-State": approval_status,
    }
    if audit_delivered:
        return Response(status_code=204, headers=common_headers)
    return JSONResponse(
        status_code=202,
        content={
            "decision": "accepted",
            "decision_id": event.event_id,
            "audit": "pending",
            "approval_status": approval_status,
            "effective": False,
            "retry_safe": True,
        },
        headers={"Retry-After": "1", **common_headers},
    )


@router.post(
    "/approvals/{approval_id}/approve",
    status_code=204,
    responses={
        202: {
            "description": (
                "Vote accepted durably; approval remains non-effective while "
                "signed audit delivery is pending"
            ),
        },
        409: {"description": "A competing final vote is awaiting audit delivery"},
        503: {
            "description": (
                "Database commit outcome could not be reconciled; retrying the "
                "returned decision id is safe"
            ),
        },
    },
)
async def approve_approval(request: Request, approval_id: int) -> Response:
    """Record an approval vote; once the required quorum of distinct approvers is
    met the polling consent path proceeds (N-of-M dual control)."""
    require_permission(request, "operate")
    who = _supervisor(request)
    world = _world()
    event = _record_vote_or_raise(world, approval_id, "approved", who)
    return _approval_decision_response(
        world,
        event,
        _audit_approval_decision(world, event),
    )


@router.post(
    "/approvals/{approval_id}/deny",
    status_code=204,
    responses={
        202: {
            "description": (
                "Vote accepted durably; denial remains non-effective while "
                "signed audit delivery is pending"
            ),
        },
        409: {"description": "A competing final vote is awaiting audit delivery"},
        503: {
            "description": (
                "Database commit outcome could not be reconciled; retrying the "
                "returned decision id is safe"
            ),
        },
    },
)
async def deny_approval(request: Request, approval_id: int) -> Response:
    """Deny a parked action (a single deny rejects it); the polling consent path
    then refuses it."""
    require_permission(request, "operate")
    who = _supervisor(request)
    world = _world()
    event = _record_vote_or_raise(world, approval_id, "denied", who)
    return _approval_decision_response(
        world,
        event,
        _audit_approval_decision(world, event),
    )


@router.get("/approvals/{approval_id}/state")
async def approval_state(request: Request, approval_id: int) -> dict:
    """N-of-M quorum progress for an approval (status, approvers so far vs.
    required) — the operator view of an in-flight multi-party decision."""
    require_permission(request, "operate")
    st = _world().approval_state(approval_id)
    if st is None:
        raise HTTPException(status_code=404, detail="no such approval")
    return st


@router.post("/approvals/{approval_id}/claim")
async def claim_approval(request: Request, approval_id: int) -> dict:
    """Claim a pending approval (collaborative supervision).

    Marks "I'm handling this" so two supervisors don't double-work the same
    review. 409 when another supervisor already holds the claim."""
    # Same governance gate as approve/deny: claiming/releasing mutates the
    # human-oversight queue, so a read-only `viewer` must not be able to lock
    # pending approvals (or learn the claiming supervisor's identity via the 409).
    require_permission(request, "operate")
    who = _supervisor(request)
    if _world().claim_approval(approval_id, who):
        return {"claimed_by": who}
    a = _world().get_approval(approval_id)
    if a is None or a.status != "pending":
        raise HTTPException(status_code=404, detail="no such pending approval")
    raise HTTPException(status_code=409,
                        detail=f"already claimed by {a.claimed_by}")


@router.post("/approvals/{approval_id}/release")
async def release_approval(request: Request, approval_id: int) -> dict:
    """Release a claim you hold. 409 when you don't hold it."""
    require_permission(request, "operate")
    who = _supervisor(request)
    if _world().release_approval(approval_id, who):
        return {"released": True}
    raise HTTPException(status_code=409, detail="you do not hold this claim")


@router.get("/oversight/active")
async def oversight_active(request: Request) -> dict:
    """Active agents right now: running goals + their latest activity.

    Owner-scoped (auth-off/admin -> all). Powers the live "Active now" panel on
    the oversight console -- polled client-side so the operator watches the
    fleet work without a full-page reload. Fail-soft per goal: a goal whose
    event tail can't be read still lists with an empty activity.
    """
    w = _world()
    goals = w.list_goals(
        status="active", owner=goal_owner_filter(request), limit=50, order="desc",
    )
    out = []
    for g in goals:
        activity = ""
        updated_at = g.updated_at
        try:
            # Public method (works on SQLite AND Postgres) instead of a raw
            # `w.conn` query with `?` placeholders, which silently blanked every
            # activity under the Postgres backend. content is already decoded.
            events = w.recent_goal_events(g.id, limit=1)
            if events:
                ev = events[-1]  # newest (chronological order)
                content = (ev.content or "")[:120]
                activity = f"{ev.kind or ''}: {content}".strip(": ").strip()
                updated_at = ev.ts
        except Exception:
            activity = ""
            updated_at = g.updated_at
        out.append({
            "id": g.id, "title": g.title, "status": g.status,
            "updated_at": updated_at, "activity": activity,
        })
    return {"goals": out}


@router.get("/oversight/why/{goal_id}")
async def oversight_why(request: Request, goal_id: int, limit: int = 40) -> dict:
    """Explain *why* an agent is doing what it's doing — the governance drill-down.

    For one goal: status, cost-so-far, a by-kind summary, and the most-recent
    event chain (plan → tool → decision) that led here, so a supervisor can
    answer "why is this agent acting / why is this approval being requested"
    inline on the oversight console without hopping to the trajectory page.
    Owner-scoped via ``assert_goal_access``; fail-soft on cost so a spend-read
    error never 500s the drill-down.
    """
    from collections import Counter

    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    limit = max(1, min(limit, 200))
    events = w.recent_goal_events(goal_id, limit=limit)
    by_kind: Counter = Counter(e.kind for e in events)
    cost = 0.0
    try:
        cost = sum(ep.cost_dollars for ep in w.list_episodes(goal_id=goal_id, limit=200))
    except Exception:  # pragma: no cover -- cost is best-effort, never blocks
        cost = 0.0
    return {
        "goal_id": goal_id,
        "title": g.title,
        "status": g.status,
        "result": g.result,
        "cost_dollars": round(cost, 4),
        "summary": dict(by_kind),
        "events": [
            {"id": e.id, "agent": e.agent, "kind": e.kind,
             "content": (e.content or "")[:400], "ts": e.ts}
            for e in events
        ],
    }


@router.get("/fleets")
async def list_fleets_api(request: Request) -> dict:
    """The operator console roster: each fleet, its owner, and its agents.

    Read-only mirror of the ``/fleets`` page (Layer C of the enterprise control
    plane). Fail-soft to an empty list so a missing registry never 500s.

    Owner-scoped: a non-admin authenticated caller sees only the fleets they
    own; auth-off and admin callers see all (``goal_owner_filter`` returns None).
    """
    try:
        from maverick.fleet import list_fleets
        fleets = list_fleets()
    except Exception:
        fleets = []
    owner = goal_owner_filter(request)
    if owner is not None:
        fleets = [f for f in fleets if f.owner == owner]
    return {"fleets": [f.to_dict() for f in fleets]}




@router.post("/fleets/{fleet_name}/run", status_code=201)
async def run_fleet_agent(
    request: Request, fleet_name: str, payload: FleetRunIn, bg: BackgroundTasks,
) -> dict:
    """Dispatch a governed goal AS a fleet agent, from the operator console.

    The agent runs least-privileged under both its RBAC role's capability and
    the dispatching user's capability, while keeping its own audit principal
    (``agent:<fleet>.<agent>``), so the oversight control plane
    governs the work automatically (mirrors ``maverick fleet run``). Owner-scoped:
    only the fleet's owner -- or an admin / auth-off caller -- may dispatch; a
    cross-owner (or missing) fleet 404s, never revealing existence.
    """
    # Dispatching a fleet agent queues a governed goal that spends provider
    # money and runs tools -- an "operate" action, exactly like compose/resume
    # and POST /goals. Gate on role BEFORE any provider/rate/owner check so a
    # read-only viewer is 403'd up front (owner-scoping alone is not authz: a
    # viewer can own a fleet yet must never run one).
    require_permission(request, "operate")
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request)

    from maverick.capability import (
        UnknownRoleError,
        capability_for_role,
        capability_from_config,
    )
    from maverick.fleet import governance_enabled, load_fleet, record_run
    from maverick.runner import run_goal_in_background_async

    fleet = load_fleet(fleet_name)
    principal = caller_principal(request)
    is_admin = principal is not None and is_dashboard_admin(principal)
    if fleet is None or (
        principal is not None
        and not is_admin
        and fleet.owner != principal
    ):
        raise HTTPException(status_code=404, detail="no such fleet")
    agent = next((a for a in fleet.agents if a.name == payload.agent), None)
    if agent is None:
        raise HTTPException(status_code=404, detail="no such agent in fleet")
    # Job-function scoping via the KERNEL gate (maverick.fleet), so this HTTP
    # route and any other dispatcher share one resolution chain (explicit grant
    # -> SCIM-group grant -> configured default; admins and auth-off pass). A
    # department-deployed agent carries its specialist pack (agent.domain); a
    # caller granted other departments may not dispatch it, even if they own
    # the fleet (e.g. a grant narrowed after deploy).
    from maverick.fleet import ensure_dispatch_allowed
    from maverick.suite_grants import DepartmentAccessError
    try:
        ensure_dispatch_allowed(principal, agent)
    except DepartmentAccessError as exc:
        raise HTTPException(
            status_code=403,
            detail="insufficient department access for this action") from exc
    # Provider readiness is an operational precondition, not authorization.
    # Keep it behind owner and department gates so an incomplete deployment
    # cannot mask their 404/403 responses or disclose configuration state to a
    # caller who is not allowed to dispatch this agent.
    require_provider_or_400()
    # Running an agent under the oversight control plane is the paid (Gold)
    # "fleet governance" capability. Fail-open: a no-op unless license enforcement
    # is on and the license lacks it. Checked after existence (404) so a missing
    # fleet never reveals licensing state.
    if not governance_enabled():
        raise HTTPException(
            status_code=403,
            detail="fleet governance is a paid (Gold) add-on not included in this license")
    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    agent_principal = fleet.principal_for(agent.name)
    try:
        cap = capability_for_role(agent.role, principal=agent_principal)
    except UnknownRoleError:
        # A saved fleet may carry an undefined/empty role (created before role
        # validation, or with RBAC roles opt-out). The CLI rejects these at
        # `fleet create`, but the dashboard run endpoint stays lenient so a
        # previously-runnable fleet keeps working: fall back to the base grant,
        # which is attenuated to the caller below -- a non-admin can therefore
        # never exceed their own capability. (capability_for_role was made
        # strict in #931, which otherwise 500s this legacy path.)
        cap = capability_from_config(agent_principal, user_id=agent_principal)
    if principal is not None and not is_admin:
        caller_cap = capability_from_config(principal, user_id=principal)
        cap = cap.attenuate(
            allow=caller_cap.allow_tools or None,
            deny=caller_cap.deny_tools,
            max_risk=caller_cap.max_risk,
            allow_paths=caller_cap.allow_paths or None,
            allow_hosts=caller_cap.allow_hosts or None,
        )
    # Department-deployed agents carry their specialist pack: bind the run to
    # that pack's capability envelope (least privilege per domain), on top of
    # the role + caller grants. domain_capability only narrows (never broadens),
    # and an unknown/disabled pack leaves the grant unchanged.
    if agent.domain:
        from maverick.domain import available_domains, domain_capability
        prof = available_domains().get(agent.domain)
        if prof is not None:
            cap = domain_capability(prof, cap, agent_principal)
    max_dollars = (
        min(payload.max_dollars, DEFAULT_MAX_DOLLARS)
        if payload.max_dollars is not None else DEFAULT_MAX_DOLLARS
    )

    w = _world()
    goal_id = w.create_goal(prompt[:200], prompt, owner=fleet.owner)
    record_run(fleet_name, agent.name, goal_id)
    # Schedule against the authenticated caller (or the shared anonymous lane
    # when auth is off), not the fleet-agent audit principal.  Agent names are
    # user-created, so using them as scheduler principals lets one caller mint
    # many lanes and bypass MAVERICK_MAX_CONCURRENT_GOALS_PER_PRINCIPAL.
    bg.add_task(
        run_goal_in_background_async, goal_id, max_dollars,
        channel="fleet", user_id=agent_principal, capability=cap,
        concurrency_principal=principal,
    )
    return {"goal_id": goal_id, "principal": agent_principal, "role": agent.role}






@router.post("/fleets", status_code=201)
async def create_fleet(request: Request, payload: FleetCreateIn) -> dict:
    """Create (or replace) a fleet from the operator console, owned by the caller.

    Mirrors ``maverick fleet create`` so a non-technical operator never needs the
    CLI. Owner-scoped: replacing a fleet owned by someone else 404s (never
    reveals it). Blank agent rows are dropped; each agent needs a valid name
    and a configured RBAC role when roles are configured.
    """
    # Mutating fleet config is an "operate" action; owner-scoping decides WHOSE
    # fleet, not WHETHER the role may write one. A read-only viewer must be 403'd
    # (otherwise it could self-own a fleet here and then run it).
    require_permission(request, "operate")
    from maverick.capability import configured_roles
    from maverick.fleet import Fleet, FleetAgent, load_fleet, save_fleet, valid_name

    if not valid_name(payload.name):
        raise HTTPException(status_code=400, detail="invalid fleet name")
    agents = tuple(
        FleetAgent(
            name=a.name.strip(), role=a.role.strip(), description=a.description.strip(),
        )
        for a in payload.agents if a.name.strip()
    )
    configured = configured_roles()
    for a in agents:
        if not valid_name(a.name):
            raise HTTPException(status_code=400, detail=f"invalid agent name: {a.name!r}")
        if not a.role:
            raise HTTPException(status_code=400, detail=f"missing role for agent: {a.name!r}")
        if configured and a.role not in configured:
            raise HTTPException(status_code=400, detail=f"unknown role for agent: {a.name!r}")

    principal = caller_principal(request)
    owner = principal or ""
    existing = load_fleet(payload.name)
    if (
        existing is not None and existing.owner != owner
        and principal is not None and not is_dashboard_admin(principal)
    ):
        raise HTTPException(status_code=404, detail="no such fleet")

    try:
        save_fleet(Fleet(name=payload.name, owner=owner, agents=agents))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"fleet": load_fleet(payload.name).to_dict()}


@router.post("/fleets/{fleet_name}/agents", status_code=201)
async def add_fleet_agent(request: Request, fleet_name: str,
                          payload: FleetAgentAddIn) -> dict:
    """Add one specialist pack to a fleet roster (the JD-hire deploy step).

    Creates the fleet (owned by the caller) if it doesn't exist; owner-scoped
    like create/delete. The added agent carries ``domain=pack`` so dispatch
    binds the pack's capability envelope, and the caller's department grant
    must cover the pack's suite — the same kernel gate department deploys use.
    """
    require_permission(request, "operate")
    from maverick.domain import suite_for
    from maverick.fleet import load_fleet, valid_name
    from maverick.jd_hiring import add_pack_to_fleet
    from maverick.suite_grants import DepartmentAccessError, ensure_suite_allowed

    if not valid_name(fleet_name):
        raise HTTPException(status_code=400, detail="invalid fleet name")
    principal = caller_principal(request)
    existing = load_fleet(fleet_name)
    if (
        existing is not None and existing.owner != (principal or "")
        and principal is not None and not is_dashboard_admin(principal)
    ):
        raise HTTPException(status_code=404, detail="no such fleet")
    try:
        ensure_suite_allowed(principal, suite_for(payload.pack))
    except DepartmentAccessError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    owner = existing.owner if existing is not None else (principal or "")
    try:
        fleet = await run_in_threadpool(
            lambda: add_pack_to_fleet(payload.pack, fleet_name, owner,
                                      role=payload.role))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"fleet": fleet.to_dict()}


@router.delete("/fleets/{fleet_name}", status_code=204)
async def delete_fleet(request: Request, fleet_name: str) -> None:
    """Remove a fleet. Owner-scoped: a cross-owner or missing fleet 404s."""
    # Deleting a fleet is an "operate" action; gate on role before owner-scoping.
    require_permission(request, "operate")
    from maverick.fleet import load_fleet, remove_fleet

    fleet = load_fleet(fleet_name)
    principal = caller_principal(request)
    is_admin = principal is not None and is_dashboard_admin(principal)
    if fleet is None or (
        principal is not None
        and not is_admin
        and fleet.owner != principal
    ):
        raise HTTPException(status_code=404, detail="no such fleet")
    remove_fleet(fleet_name)


def _compliance_checks(framework: str):
    """Run the core control-coverage report, filtered like the CLI.

    Single source of truth for the /compliance page and these exports:
    ``maverick.compliance.compliance_report()`` (GDPR + EU AI Act + US
    frameworks). ``framework`` is one of ``eu``/``us``/``all``; anything else
    falls back to ``all``. Fail-soft to an empty list so a missing core install
    yields an empty (still-downloadable) report rather than a 500.
    """
    framework = framework if framework in {"eu", "us", "all"} else "all"
    try:
        from maverick.compliance import compliance_report
        checks = compliance_report()
    except Exception:  # pragma: no cover - never 500 the export if core is absent
        return framework, []
    if framework != "all":
        checks = [c for c in checks if c.framework == framework]
    return framework, checks


@router.get("/compliance/report.md")
async def compliance_report_md(framework: str = "all") -> Response:
    """Download the control-coverage report as Markdown for an auditor.

    Same data as the /compliance page (``maverick.compliance``). The
    ``?framework=eu|us|all`` filter mirrors ``maverick compliance``. Returned as
    an attachment so an operator can hand the file to an auditor.
    """
    from maverick.compliance import render_report_text
    framework, checks = _compliance_checks(framework)
    body = render_report_text(checks)
    fname = f"lightwork-compliance-{framework}.md"
    return Response(
        content=body,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/compliance/report.csv")
async def compliance_report_csv(framework: str = "all") -> Response:
    """Download the control-coverage report as CSV for an auditor.

    Same data source + ``?framework=`` filter as ``report.md``. One row per
    control: framework, control, regulation, status, detail.
    """
    import csv
    import io as _io

    from maverick.compliance import COMPLIANCE_DISCLAIMER
    framework, checks = _compliance_checks(framework)
    buf = _io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["framework", "control", "regulation", "status", "detail"])
    for c in checks:
        writer.writerow([
            _csv_formula_safe(value)
            for value in (c.framework, c.control, c.regulation, c.status, c.detail)
        ])
    writer.writerow([])
    writer.writerow([
        _csv_formula_safe("disclaimer"),
        _csv_formula_safe(COMPLIANCE_DISCLAIMER),
    ])
    fname = f"lightwork-compliance-{framework}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )




@router.post("/redact/preview")
async def redact_preview(request: Request, payload: RedactIn) -> dict:
    """Granular redaction preview: per-finding spans + kinds, nothing stored.

    ``kinds`` filters which detector classes to act on (e.g. only
    ``secret:*`` or only ``pii:email``) — the granular half; empty = all.
    The response carries each finding (kind + a safe preview of WHERE, never
    the raw value) and the fully-redacted text for the selected kinds.
    """
    # Gate behind auth: this runs the detector pipeline on caller-supplied text,
    # so it must not be an unauthenticated compute/probe surface.
    require_permission(request, "operate")
    from maverick.provable_redaction import redact_proven, verify_redacted
    from maverick.safety import pii_detector, secret_detector

    text = payload.text or ""
    findings = []
    for m in secret_detector.scan(text):
        findings.append({"kind": f"secret:{m.name}", "span": list(m.span)})
    for m in pii_detector.scan(text):
        findings.append({"kind": f"pii:{m.kind}", "span": list(m.span)})
    selected = set(payload.kinds or [])

    if not selected:
        proof = redact_proven(text)
        redacted, proven = proof.redacted, proof.proven
    else:
        # granular: replace only the selected kinds' spans (end-to-start)
        spans = [f for f in findings if f["kind"] in selected]
        redacted = text
        for f in sorted(spans, key=lambda f: f["span"][0], reverse=True):
            a, b = f["span"]
            redacted = redacted[:a] + f"[REDACTED:{f['kind'].split(':', 1)[1]}]" + redacted[b:]
        proven = not verify_redacted(redacted)

    return {
        "findings": findings,
        "redacted": redacted,
        "proven_clean": proven,
        "residual": verify_redacted(redacted),
    }


@router.get("/glance")
async def watch_glance(request: Request) -> dict:
    """The Apple Watch glance payload (tiny fixed shape; see maverick.glance)."""
    from maverick.glance import build_glance
    from maverick.world_model import close_world_if_owned, open_world
    world = open_world()
    try:
        return build_glance(world, owner=goal_owner_filter(request))
    finally:
        try:
            close_world_if_owned(world)
        except Exception:  # pragma: no cover -- best-effort response teardown
            pass


@router.get("/offline/bundle")
async def offline_bundle(request: Request) -> dict:
    """Bounded, versioned snapshot (``maverick-offline/1``) for the mobile
    companion's offline cache. Read-only; owner-scoped like ``/goals``."""
    from maverick.offline_bundle import build_bundle
    return await run_in_threadpool(
        build_bundle, _world(), owner=goal_owner_filter(request),
    )


@router.get("/perf")
async def perf_dashboard() -> dict:
    """Public perf dashboard data: SLA measurements + benchmark history.

    One JSON face for the perf story (roadmap 2027-H1 "public perf
    dashboard"): the live perf-SLA measurements against their published
    thresholds (docs/perf-sla.md), the recorded benchmark score history with
    short-window regression verdicts, and the longitudinal era retrospective.
    Everything is measured/read locally -- nothing fabricated; sections with
    no recorded data say so.
    """
    out: dict = {"sla": [], "benchmarks": {}, "retrospective": None}
    try:
        sla, error = await _cached_perf_sla()
        out["sla"] = sla
        if error:
            out["sla_error"] = error
    except Exception as e:  # measurement/cache must never 500 the dashboard
        out["sla_error"] = f"{type(e).__name__}: {e}"
    try:
        import json as _json

        from maverick.benchmark_retrospective import analyze, coverage
        from maverick.continuous_benchmark import _store_path, detect_regression, load_history
        store = _store_path()
        history: list[dict] = []
        if store.is_dir():
            # Legacy layout: a directory of per-suite *.json files.
            files = sorted(
                store.glob("*.json"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
                reverse=True,
            )[:_PERF_HISTORY_MAX_FILES]
            for f in sorted(files):
                try:
                    rows = _json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(rows, list):
                        history.extend(r for r in rows if isinstance(r, dict))
                except (OSError, ValueError):
                    continue
        else:
            # Production layout: the single history.json FILE the bench_track
            # tool writes via save_history -- the same store /benchmarks reads.
            history.extend(r for r in load_history(store) if isinstance(r, dict))
        names = sorted({r.get("name") for r in history if r.get("name")})
        for name in names:
            scores = [r["score"] for r in history if r.get("name") == name]
            verdict = detect_regression(history, name)
            out["benchmarks"][name] = {
                "runs": len(scores),
                "latest": scores[-1] if scores else None,
                "best": max(scores) if scores else None,
                "regression": verdict,
            }
        span = coverage(history)
        if span:
            retros = analyze(history)
            out["retrospective"] = {
                "coverage": list(span),
                "trends": {n: {"trend": r.trend,
                               "net_change": round(r.net_change, 4)}
                           for n, r in retros.items()},
            }
    except Exception as e:
        out["benchmarks_error"] = f"{type(e).__name__}: {e}"
    return out


async def _cached_perf_sla() -> tuple[list[dict], str | None]:
    """Return perf-SLA rows with a short single-flight cache.

    ``run_all()`` performs live CPU/IO probes.  The dashboard exposes this data
    via a GET endpoint, so cache the expensive portion and serialize refreshes
    to keep cross-site/simple GET floods from starting unbounded worker-thread
    measurements in no-token loopback mode.
    """
    global _PERF_SLA_CACHE

    now = time.monotonic()
    if _PERF_SLA_CACHE is not None:
        expires_at, rows, error = _PERF_SLA_CACHE
        if now < expires_at:
            return rows, error

    async with _PERF_SLA_LOCK:
        now = time.monotonic()
        if _PERF_SLA_CACHE is not None:
            expires_at, rows, error = _PERF_SLA_CACHE
            if now < expires_at:
                return rows, error

        try:
            from maverick.perf_sla import run_all

            # run_all's dispatch probe drives its own event loop; run it in a
            # worker thread so it never nests inside the server's running loop.
            results = await asyncio.to_thread(run_all)
            rows = [
                {
                    "name": r.name,
                    "measured": r.measured,
                    "threshold": r.threshold,
                    "unit": r.unit,
                    "passed": r.passed,
                }
                for r in results
            ]
            error = None
        except Exception as e:  # measurement must never 500 the dashboard
            rows = []
            error = f"{type(e).__name__}: {e}"
        _PERF_SLA_CACHE = (now + _PERF_SLA_CACHE_TTL_SECONDS, rows, error)
        return rows, error


@router.get("/cache/stats")
async def cache_stats() -> dict:
    """In-process cache sizes (file reads, repo-map, skill embeddings).

    Mirrors ``maverick cache stats`` — surfaced here so the dashboard
    Cache page can render without shelling out.
    """
    from maverick.cache import stats
    return stats()




@router.post("/cache/purge")
async def cache_purge(request: Request, payload: CachePurgeIn) -> dict:
    """Purge one or more cache scopes.

    Valid scopes (from maverick.cache._VALID_SCOPES): files, repo_map,
    skill_embeddings, all. Unknown scopes are ignored.
    """
    require_permission(request, "operate")
    from maverick.cache import purge
    return purge(payload.scopes or ["all"])


# ---------- goal graph: forest view + structural edits (graph editor) ----------


@router.get("/goal-tree")
async def goal_tree_api(request: Request, limit: int = 300) -> dict:
    """The caller's goal forest with a server-computed layered layout.

    Powers /graph-editor and /plan-tree-3d: nodes carry (x, y) pixel
    positions so the client JS is a thin renderer. Owner-scoped like
    GET /goals (auth-off/admin see all).
    """
    from .goal_tree import forest_view, goal_nodes
    nodes = goal_nodes(_world(), owner=goal_owner_filter(request), limit=limit)
    return forest_view(nodes)




@router.post("/goals/{goal_id}/retitle", status_code=204)
async def retitle_goal(request: Request, goal_id: int, payload: RetitleIn) -> None:
    """Rename a goal (graph editor).

    Uses the world model's public ``set_goal_title`` so it works identically on
    the SQLite and Postgres backends (the earlier raw-SQL path used SQLite-only
    internals and 500'd under Postgres).
    """
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




@router.post("/goals/{goal_id}/reparent", status_code=204)
async def reparent_goal(request: Request, goal_id: int, payload: ReparentIn) -> None:
    """Move a goal under a new parent — or to the root (``parent_id: null``).

    Refuses self-parenting and any move that would create a cycle (the new
    parent being a descendant of the goal). Both ends are access-checked.
    """
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    require_permission(request, "operate")
    new_parent = payload.parent_id
    if new_parent is not None:
        if new_parent == goal_id:
            raise HTTPException(status_code=400, detail="a goal cannot be its own parent")
        p = w.get_goal(new_parent)
        if p is None:
            raise HTTPException(status_code=404, detail="no such parent goal")
        assert_goal_access(request, p)
        from .goal_tree import descendant_ids
        if new_parent in descendant_ids(w.goal_parent_pairs(), goal_id):
            raise HTTPException(
                status_code=400,
                detail="cannot re-parent a goal under its own descendant",
            )
    w.set_goal_parent(goal_id, new_parent)




@router.post("/goals/{goal_id}/children", response_model=GoalOut, status_code=201)
async def create_child_goal(request: Request, goal_id: int, payload: ChildIn) -> GoalOut:
    """Create a child goal under ``goal_id`` (graph editor "add child").

    Structural only: the child is created ``pending`` and is NOT queued to
    run — start it later via chat or POST /api/v1/goals. It inherits the
    parent's owner so the subtree stays visible to the same principal.
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
    child_id = w.create_goal(
        title[:200], (payload.description or "")[:8000],
        parent_id=goal_id, owner=g.owner or (caller_principal(request) or ""),
    )
    c = w.get_goal(child_id)
    if c is None:
        raise HTTPException(status_code=500, detail="goal vanished after create")
    return _to_goal_out(c)


# ---------- goal builder: compose a goal from blocks ----------

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
    goal_id = w.create_goal(
        title[:200], description[:8000], owner=caller_principal(request) or "",
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
            channel="api", user_id=user_id, allowed_suites=allowed_suites,
        )
    else:
        bg.add_task(
            run_goal_in_background_async,
            goal_id,
            max_dollars,
            allowed_suites=allowed_suites,
        )
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=500, detail="goal vanished after create")
    return _to_goal_out(g)


# ---------- AI workflow builder ----------
#
# Draft from a natural-language brief or an uploaded document, in one of two
# forms (selected by `form`): a reusable, parameterized **template** (saved as a
# user Template that runs like any other), or a specialist **agent playbook** (a
# domain pack with persona, tool allowlist, risk ceiling, and gated steps).
# Drafting is one budget-capped LLM call (see maverick_dashboard.workflow_ai);
# templates save here (writes ~/.maverick/templates/<name>.md), while a playbook
# saves via the existing POST /agents/<name>/override (write_override).

_WORKFLOW_DOC_MAX_BYTES = 512_000  # ample for a spec/runbook; the model sees a truncated head


def _extract_binary_doc_text(raw: bytes, filename: str) -> str | None:
    """Text of a binary document (PDF/DOCX/ODT) via the maverick-knowledge
    parsers, or None when they're not installed / the file is unparseable.
    Blocking — call through run_in_threadpool."""
    import tempfile as _tempfile
    from pathlib import Path as _Path
    suffix = _Path(filename).suffix.lower() or ".bin"
    if suffix not in {".pdf", ".docx", ".odt", ".rtf", ".html", ".htm"}:
        return None
    try:
        from maverick_knowledge.parse import extract_text
    except ImportError:
        return None
    tmp = _tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(raw)
        tmp.close()
        out = extract_text(_Path(tmp.name))
        return out.strip() or None
    except Exception:  # noqa: BLE001 -- unparseable: caller 400s with a hint
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:  # pragma: no cover
            pass


def _require_provider_for_drafting() -> None:
    require_provider_or_400(role="orchestrator")


def _drafter_for(form: str):
    """The drafting function for the requested form (default: template)."""
    from .workflow_ai import draft_playbook, draft_workflow
    return draft_playbook if form == "playbook" else draft_workflow


def _workflow_form(form: str) -> str:
    """Validate the public authoring discriminator instead of defaulting typos."""
    form = str(form or "").strip().lower()
    if form not in {"template", "playbook"}:
        raise HTTPException(
            status_code=400,
            detail="form must be 'template' or 'playbook'",
        )
    return form


def _workflow_authoring_identity(request: Request) -> tuple[str | None, str | None]:
    user_id = execution_user_id_from_request(request)
    return ("api" if user_id else None), user_id


def _playbook_available_tools(
    *, channel: str | None, user_id: str | None,
) -> frozenset[str]:
    """Caller/tenant-scoped live names used to ground one Agent Factory draft."""
    return frozenset(
        str(item["name"])
        for item in _live_tool_index(channel=channel, user_id=user_id)
        if item.get("name")
    )


def _safe_workflow_drafting_error(exc: BaseException) -> str:
    """Bound and scrub upstream/provider text before returning it over HTTP."""
    try:
        from maverick.secrets import scrub

        detail = scrub(str(exc)).strip()[:500]
    except Exception:
        detail = ""
    return detail or "upstream drafting failed"


@router.post("/workflows/draft")
async def draft_workflow_from_brief(request: Request, payload: WorkflowDraftIn) -> dict:
    """Chat path: a natural-language brief -> a drafted workflow or playbook
    (per ``form``; not saved)."""
    require_permission(request, "operate")
    form = _workflow_form(payload.form)
    _require_provider_for_drafting()
    # Drafting is a paid LLM call; throttle it under the same ceiling as goal
    # creation so it can't be spammed into a cost amplifier.
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request, source="workflow-draft")
    brief = (payload.description or "").strip()
    if not brief:
        raise HTTPException(status_code=400, detail="describe the workflow you want")
    channel, user_id = _workflow_authoring_identity(request)
    try:
        # The drafter makes a synchronous LLM call; offload it so the single
        # event loop isn't frozen for the multi-second round-trip (which would
        # stall every other user's requests, SSE and health probes).
        def _draft():
            drafter = _drafter_for(form)
            if form == "playbook":
                return drafter(
                    brief,
                    available_tools=_playbook_available_tools(
                        channel=channel, user_id=user_id),
                )
            return drafter(brief)

        return await run_in_threadpool(_draft)
    except Exception as e:
        # ValueError is a parse failure; BudgetExceeded / provider / network
        # errors are NOT ValueError -- surface any of them as an upstream 502
        # rather than an unhandled 500.
        detail = _safe_workflow_drafting_error(e)
        raise HTTPException(
            status_code=502, detail=f"workflow drafting failed: {detail}") from e


@router.post("/workflows/draft-from-file")
async def draft_workflow_from_upload(
    request: Request,
    file: UploadFile = File(...),
    form: str = Form("template"),
) -> dict:
    """Upload path: extract a workflow or playbook (per ``form``) from a
    text / markdown / JSON document — or a PDF / Word / OpenDocument file
    when the maverick-knowledge parsers are installed."""
    require_permission(request, "operate")
    form = _workflow_form(form)
    _require_provider_for_drafting()
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request, source="workflow-draft")
    raw = await file.read(_WORKFLOW_DOC_MAX_BYTES + 1)
    if len(raw) > _WORKFLOW_DOC_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail="file too large to draft from; paste the key parts into the brief instead",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = await run_in_threadpool(
            _extract_binary_doc_text, raw, file.filename or "upload")
        if text is None:
            raise HTTPException(
                status_code=400,
                detail=("could not read this file as text — upload a .txt / .md / "
                        ".json / .pdf / .docx document, or describe the workflow "
                        "in the brief"),
            ) from None
    if not text.strip():
        raise HTTPException(status_code=400, detail="the uploaded file was empty")
    channel, user_id = _workflow_authoring_identity(request)
    try:
        drafter = _drafter_for(form)
        def _draft():
            if form == "playbook":
                return drafter(
                    "",
                    source_text=text,
                    available_tools=_playbook_available_tools(
                        channel=channel, user_id=user_id),
                )
            return drafter("", source_text=text)

        return await run_in_threadpool(_draft)
    except Exception as e:
        # See draft_workflow_from_brief: non-ValueError LLM/budget errors -> 502.
        detail = _safe_workflow_drafting_error(e)
        raise HTTPException(
            status_code=502, detail=f"workflow drafting failed: {detail}") from e


@router.post("/workflows/refine")
async def refine_workflow_draft(request: Request, payload: WorkflowRefineIn) -> dict:
    """Revise the current draft (template or playbook, per ``form``) with a
    natural-language follow-up — the iterative loop in the builder."""
    require_permission(request, "operate")
    form = _workflow_form(payload.form)
    _require_provider_for_drafting()
    from maverick_dashboard.app import check_goal_rate_limit
    check_goal_rate_limit(request, source="workflow-draft")
    instruction = (payload.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="describe the change you want")
    try:
        current_size = len(json.dumps(
            payload.current or {}, ensure_ascii=False, allow_nan=False,
        ).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="current draft is invalid") from exc
    if current_size > 50_000:
        raise HTTPException(status_code=400, detail="current draft is too large to refine")
    from .workflow_ai import refine_playbook, refine_workflow
    refiner = refine_playbook if form == "playbook" else refine_workflow
    channel, user_id = _workflow_authoring_identity(request)
    try:
        def _refine():
            if form == "playbook":
                return refiner(
                    payload.current or {},
                    instruction,
                    available_tools=_playbook_available_tools(
                        channel=channel, user_id=user_id),
                )
            return refiner(payload.current or {}, instruction)

        return await run_in_threadpool(_refine)
    except Exception as e:
        # See draft_workflow_from_brief: non-ValueError LLM/budget errors -> 502.
        detail = _safe_workflow_drafting_error(e)
        raise HTTPException(
            status_code=502, detail=f"refining the draft failed: {detail}") from e


@router.post("/workflows", status_code=201)
async def save_workflow(request: Request, payload: WorkflowSaveIn) -> dict:
    """Persist an (AI-drafted, edited) workflow as a runnable user template."""
    require_permission(request, "operate")
    from maverick.templates import save_user_template
    try:
        tpl = save_user_template(
            payload.name,
            title=payload.title,
            body=payload.body,
            params=payload.params,
            budget_dollars=payload.budget_dollars,
            budget_wall_seconds=payload.budget_wall_seconds,
            overwrite=payload.overwrite,
            owner=caller_principal(request) or "local",
            expected_generation=payload.expected_generation,
        )
    except FileExistsError as e:
        # A fresh save (overwrite=false) colliding with an existing template is a
        # 409 conflict, not a silent clobber. The ?edit= flow sets overwrite=true.
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return {
        "name": tpl.name,
        "title": tpl.title,
        "params": tpl.params,
        "generation": tpl.generation,
        "saved": True,
    }


# ---------- continuous-benchmark history ----------


def _benchmark_snapshot() -> dict:
    """Recorded benchmark runs, grouped per suite, with regression verdicts.

    Reads the same store ``maverick.continuous_benchmark`` (the bench_track
    tool) persists to — this deployment's own recorded runs, nothing else.
    Malformed rows (hand-edited file) are skipped, not invented around.
    """
    from maverick import continuous_benchmark as cb
    path = cb._store_path()
    history: list[dict] = []
    for h in cb.load_history(path):
        if not isinstance(h, dict) or not h.get("name"):
            continue
        try:
            score = float(h.get("score"))
        except (TypeError, ValueError):
            continue
        history.append({"name": str(h["name"]), "score": score,
                        "commit": str(h.get("commit") or ""), "t": h.get("t")})
    names: list[str] = []
    for h in history:
        if h["name"] not in names:
            names.append(h["name"])
    suites = []
    for name in names:
        entries = [h for h in history if h["name"] == name]
        r = cb.detect_regression(history, name)
        suites.append({
            "name": name,
            "runs": len(entries),
            "entries": entries[-50:],
            "latest": r["latest"],
            "baseline_mean": r["baseline_mean"],
            "delta": r["delta"],
            "drop_pct": r["drop_pct"],
            "regressed": r["regressed"],
        })
    return {"suites": suites, "history_path": str(path)}


@router.get("/benchmarks")
async def benchmarks_api() -> dict:
    """Benchmark history for this deployment (see ``_benchmark_snapshot``)."""
    snap = _benchmark_snapshot()
    if not snap["suites"]:
        snap["note"] = (
            "no benchmark runs recorded — record one with the bench_track "
            "tool (op=record, name, score) or "
            "maverick.continuous_benchmark.record_result"
        )
    return snap


# ---------- walkthrough export (replay video into the walkthroughs dir) ----------


def _walkthroughs_dir():
    """Where the dashboard's exported walkthrough videos live.

    ``maverick.replay.video.render`` writes wherever the caller points it
    (there is no fixed dir in core), so the dashboard standardises on
    ``<maverick home>/walkthroughs`` for everything the /walkthroughs page
    lists and serves.
    """
    from maverick.paths import maverick_home
    return maverick_home() / "walkthroughs"


def _vtt_for_frames(frames) -> str:
    """A WebVTT captions track derived from the storyboard frames.

    One cue per frame, timed by the frames' real durations; cue text is the
    frame's (already secret-scrubbed) caption, flattened to one line.
    """
    def ts(sec: float) -> str:
        ms = int(round(sec * 1000))
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    lines = ["WEBVTT", ""]
    t = 0.0
    for f in frames:
        end = t + f.seconds
        text = f"[{f.kind}] {f.caption}".replace("\n", " ").replace("-->", "→").strip()
        lines += [f"{ts(t)} --> {ts(end)}", text or "(no caption)", ""]
        t = end
    return "\n".join(lines)


@router.post("/goals/{goal_id}/walkthrough", status_code=201)
async def export_walkthrough(request: Request, goal_id: int) -> dict:
    """Export a run's replay video into the walkthroughs dir.

    Uses the real machinery (``maverick.replay.video.render``): always writes
    the frame manifest + a WebVTT captions track derived from the storyboard;
    the MP4 encode itself needs Pillow + ffmpeg and the response says honestly
    whether it happened (``encoded``/``detail``) and carries the exact ffmpeg
    command for out-of-band encoding when it didn't.
    """
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
    events = [
        {"kind": e.kind, "ts": e.ts, "agent": e.agent, "content": e.content}
        for e in w.goal_events(goal_id, limit=5000)
    ]
    if not events:
        raise HTTPException(
            status_code=400,
            detail="no events recorded for this goal — run it first, then export",
        )
    from maverick.replay.video import render, storyboard
    out_dir = _walkthroughs_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = storyboard(goal_id, events=events)
    vtt_name = f"goal-{goal_id}.vtt"
    (out_dir / vtt_name).write_text(_vtt_for_frames(frames), encoding="utf-8")
    try:
        from maverick.sandbox import build_sandbox
        sandbox = build_sandbox()
    except Exception:  # render falls back to the scrubbed-env runner
        sandbox = None
    out_path = out_dir / f"goal-{goal_id}.mp4"
    result = await run_in_threadpool(
        render, goal_id, out_path, sandbox=sandbox, events=events,
    )
    return {
        "goal_id": goal_id,
        "frames": result.frames,
        "encoded": result.encoded,
        "detail": result.detail,
        "video": out_path.name if result.encoded else None,
        "captions": vtt_name,
        "ffmpeg_command": result.command,
    }


@router.post("/goals/{goal_id}/resume", status_code=204)
async def resume_goal(goal_id: int, request: Request, bg: BackgroundTasks) -> None:
    """Resume a blocked / cancelled goal.

    Capabilities-seat finding: the CLI has ``maverick resume`` but the
    dashboard's only way to flip a cancelled goal back was to start a
    brand-new one. This route flips status back to 'pending' and
    re-queues the runner, so the next goal-event poll picks it back up.
    """
    require_permission(request, "operate")
    w = _world()
    g = w.get_goal(goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, g)
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
            allowed_suites=allowed_suites,
        )
    else:
        bg.add_task(
            run_goal_in_background_async,
            goal_id,
            allowed_suites=allowed_suites,
        )


# ---------------------------------------------------------------------------
# Workforce packaging: departments, outcomes, marketplace, reviews.
#
# Read-only JSON over capabilities that already ship (maverick.departments /
# .outcomes / .worker_review / .marketplace.storefront). These present the
# 1,000+ specialist packs as buyable teams with delivery and a governed review
# — the buyer-facing surfaces, not new platform. No mutation, so no extra
# permission gate beyond the dashboard's read access (cf. /overview).
# ---------------------------------------------------------------------------
def _department_request_tenant() -> str | None:
    """Tenant id for department entitlement checks in dashboard requests."""
    try:
        from maverick.paths import current_tenant_id
        return current_tenant_id()
    except Exception:  # pragma: no cover - tenant lookup must not break self-host
        return None


def _has_managed_tenant_roster() -> bool:
    """Whether this install has provisioned tenants but no request tenant."""
    try:
        from maverick.tenant import registry as tenant_registry
        return bool(tenant_registry.list_tenants())
    except Exception:  # pragma: no cover - registry lookup must not break self-host
        return False


def _department_entitled_for_request(key: str) -> bool:
    """Department entitlement for this dashboard request.

    No active tenant remains fail-open for legacy self-host installs. Once a
    tenant roster exists, however, a dashboard request with no pinned tenant is
    an ambiguous managed request; report the paid add-on as unavailable rather
    than letting the core billing gate treat it as self-hosted.
    """
    from maverick.departments import department_entitled
    tenant = _department_request_tenant()
    if tenant is None and _has_managed_tenant_roster():
        return False
    return department_entitled(key, tenant=tenant)


@router.get("/departments")
async def list_departments_api(request: Request) -> list[dict]:
    """Departments (suites) as deployable teams: title, charter, headcount.

    Each entry carries ``entitled`` — whether the active tenant's plan includes
    the paid ``departments`` add-on, so the UI shows Deploy vs. Add-on-required.
    Suite-scoped (job function): a caller with a department grant sees only
    their departments; unscoped callers (auth off / admin / no grant) see all.
    """
    from maverick.departments import list_departments
    allowed = caller_suites(request)
    out = []
    for d in list_departments():
        if allowed is not None and d.key not in allowed:
            continue
        row = d.to_dict()
        row["entitled"] = _department_entitled_for_request(d.key)
        out.append(row)
    return out


@router.get("/departments/{key}")
async def get_department_api(request: Request, key: str) -> dict:
    """One department with its specialist roster (name + description + risk)."""
    from maverick.departments import get_department, roster
    dept = get_department(key)
    if dept is None:
        raise HTTPException(status_code=404, detail="no such department")
    # Job-function scoping: 403 (not 404) — the catalog is public product
    # surface, the denial is about THIS user's grant, mirroring role gates.
    require_suite(request, key)
    out = dept.to_dict()
    out["entitled"] = _department_entitled_for_request(key)
    out["roster"] = [
        {"name": p.name, "description": p.description or "",
         "max_risk": p.max_risk or "low"}
        for p in roster(key)
    ]
    return out


@router.post("/departments/{key}/deploy", status_code=201)
async def deploy_department_api(request: Request, key: str) -> dict:
    """Deploy a department as a fleet of its specialists — a PAID ADD-ON.

    Gated four ways: ``operate`` RBAC (a viewer is 403'd), the caller's
    department grant (a finance-scoped user deploying Legal is 403'd),
    owner-scoping (you cannot clobber another owner's fleet — that 404s), and
    the ``departments`` entitlement (a tenant without the add-on is 402'd).
    Mirrors ``maverick.departments.deploy_department`` so the CLI enforces the
    same entitlement gate.
    """
    require_permission(request, "operate")
    from maverick.departments import (
        EntitlementError,
        deploy_department,
        fleet_name_for,
        get_department,
    )
    from maverick.fleet import load_fleet
    from maverick.suite_grants import DepartmentAccessError

    if get_department(key) is None:
        raise HTTPException(status_code=404, detail="no such department")
    require_suite(request, key)

    principal = caller_principal(request)
    owner = principal or ""
    fleet_name = fleet_name_for(key, owner)
    existing = load_fleet(fleet_name)
    if (
        existing is not None and existing.owner != owner
        and principal is not None and not is_dashboard_admin(principal)
    ):
        raise HTTPException(status_code=404, detail="no such fleet")

    tenant = _department_request_tenant()
    if tenant is None and _has_managed_tenant_roster():
        raise HTTPException(
            status_code=402,
            detail="departments add-on requires an active tenant",
        )

    try:
        fleet = deploy_department(key, owner, tenant=tenant)
    except EntitlementError as e:
        raise HTTPException(status_code=402, detail=str(e)) from e
    except DepartmentAccessError as e:
        # Kernel backstop (maverick.departments enforces the grant for every
        # caller); the require_suite gate above normally 403s first.
        raise HTTPException(
            status_code=403,
            detail="insufficient department access for this action") from e
    if fleet is None:  # suite disabled between the check and the deploy
        raise HTTPException(status_code=404, detail="no such department")
    return {"fleet": fleet.to_dict()}


@router.get("/departments/{key}/review")
async def department_review_api(request: Request, key: str) -> dict:
    """A governed performance review: delivery + authority + learning."""
    require_suite(request, key)
    from maverick.worker_review import review
    r = review(_world(), key, owner=goal_owner_filter(request))
    if r is None:
        raise HTTPException(status_code=404, detail="no such department")
    return r


@router.get("/outcomes")
async def outcomes_api(request: Request, top: int = 0) -> dict:
    """Per-worker delivery cards + a firm-wide rollup, from the Operating Record."""
    from maverick.operating_record import assemble
    from maverick.outcomes import firm_totals, worker_cards
    w = _world()
    owner = goal_owner_filter(request)
    cards = worker_cards(w, top=(max(0, int(top)) or None), owner=owner)
    return {
        "firm": firm_totals(assemble(w, owner=owner)).to_dict(),
        "workers": [c.to_dict() for c in cards],
    }


def _learning_snapshot(request: Request) -> dict:
    """Learning-engine status + accumulated moat. Shared by the JSON endpoint
    and the /learning page so both render identical numbers."""
    from maverick import (
        consequence,
        credit,
        data_engine,
        dreaming,
        evaluator_evolution,
        experience,
        factory_learning,
        failure_telemetry,
        jit_rl,
        operations_scientist,
        prm_guidance,
        reasoning_reward,
        reflexion,
        rehearsal,
        self_harness,
        self_improvement,
        self_learning,
        self_tuning_budget,
        trajectory_store,
    )
    from maverick.config import get_self_improvement
    from maverick.operating_record import assemble, stats
    from maverick.skill import distillation_local, synthesis

    from maverick_dashboard.settings_store import LEARNING_SUBSYSTEMS

    def _causal_promotion_on() -> bool:
        try:
            return bool(
                self_improvement.enabled()
                and get_self_improvement().get("causal_promotion", False)
            )
        except Exception:  # pragma: no cover - config never blocks the view
            return False

    # Live enabled() per subsystem key -- paired with the shared registry so the
    # label/how/config metadata stays single-sourced in settings_store.
    on_by_key = {
        "self_learning": self_learning.enabled(),
        "distill_local": distillation_local.enabled(),
        "failure_telemetry": failure_telemetry.enabled(),
        "budget_tuning": self_tuning_budget.enabled(),
        "consequence": consequence.enabled(),
        "reflexion": reflexion.enabled(),
        "dreaming": dreaming.enabled(),
        "experience": experience.enabled(),
        "skill_synthesis": synthesis.enabled(),
        "self_improvement": self_improvement.enabled(),
        "prm_guidance": prm_guidance.enabled(),
        "capture": trajectory_store.enabled(),
        "causal_promotion": _causal_promotion_on(),
        "factory_learning": factory_learning.enabled(),
        "evaluator_evolution": evaluator_evolution.enabled(),
        "self_harness": self_harness.enabled(),
        "data_engine": data_engine.enabled(),
        "operations_scientist": operations_scientist.enabled(),
        "rehearsal": rehearsal.enabled(),
        "credit": credit.enabled(),
        "reasoning_reward": reasoning_reward.enabled(),
        "reward_audit": reasoning_reward.audit_rewards_enabled(),
        "jit_rl": jit_rl.enabled(),
    }
    components = [
        {"key": s["key"], "label": s["label"],
         "on": on_by_key.get(s["key"], False), "how": s["how"]}
        for s in LEARNING_SUBSYSTEMS
    ]

    w = _world()
    owner = goal_owner_filter(request)
    st = stats(assemble(w, owner=owner))
    accumulated = {
        "decisions": st.n_records,
        "human_decisions": st.n_human_decisions,
        "approvals": st.n_approvals,
        "departments": len(st.departments),
    }
    # Learned skills and grounded outcomes are deployment-level assets (neither is
    # stored per-tenant), so only surface them on an unscoped/admin view -- a
    # single tenant must never see deployment-wide totals as if they were its own.
    if owner is None:
        try:
            # Count the ledger directly -- _learned_snapshot would also build
            # the whole harness/provenance/pending view just to be discarded.
            # The stat is a TOTAL, so don't inherit the page's display limit
            # (the old path silently capped the count at 50).
            from maverick import self_learning
            learned_n = len(self_learning.history(
                limit=1_000_000, path=self_learning.LEARNED_PATH))
        except Exception:  # pragma: no cover - the moat view never 500s on a sub-read
            learned_n = 0
        accumulated["grounded_outcomes"] = consequence.count()
        accumulated["learned_capabilities"] = learned_n
        # Flows that improved themselves -- forward self-rewrites the loop enacted,
        # a tangible "your automations got better on their own" moat number.
        try:
            from maverick.flow import evolution_log
            improvements = evolution_log.improvement_count()
        except Exception:  # pragma: no cover -- the moat view never 500s on a sub-read
            improvements = 0
        if improvements:
            accumulated["flow_self_improvements"] = improvements
    # Autonomous flow self-improvement: its own opt-in, deliberately NOT folded
    # into the blanket "turn learning on" button -- autonomously rewriting a live
    # workflow is a heavier consequence than passive learning. Admin/unscoped view.
    flow_autonomy = None
    if owner is None:
        try:
            from maverick import flow as _flow_mod
            flow_autonomy = {
                "engine_on": _flow_mod.enabled(),
                "auto_evolve": _flow_mod.auto_evolve_enabled(),   # revert regressions
                "auto_apply": _flow_mod.auto_apply_enabled(),     # apply improvements
            }
        except Exception:  # pragma: no cover -- the moat view never 500s on a sub-read
            flow_autonomy = None
    dgm = None
    if owner is None:
        try:
            from maverick import self_modify
            dgm = self_modify.production_status()
        except Exception:  # pragma: no cover -- status is fail-closed in core
            dgm = None
    return {
        "components": components,
        "dgm": dgm,
        "flow_autonomy": flow_autonomy,
        "accumulated": accumulated,
        "components_on": sum(1 for c in components if c["on"]),
        "components_total": len(components),
    }


@router.get("/learning")
async def learning_api(request: Request) -> dict:
    """Learning-engine status + the durable, per-tenant value it has accumulated.

    ``components`` reflect each subsystem's own ``enabled()`` (the source of
    truth). ``accumulated`` is the non-portable moat: decisions and human
    judgements on the Operating Record, capabilities the workforce taught
    itself, and the grounded outcomes it has learned from -- what a customer
    would forfeit by leaving. Read-only and owner-scoped."""
    return _learning_snapshot(request)


@router.post("/learning")
async def set_learning_endpoint(request: Request, payload: LearningToggleIn) -> dict:
    """Turn the whole self-learning loop on or off in one action (writes the
    learning knobs to the config overlay). Returns the refreshed snapshot so the
    caller sees the new state.

    Admin-gated: this writes the deployment-wide dashboard config overlay (the
    same one [capabilities]/[features]/providers use, all admin-gated), which
    ``load_config`` merges for every tenant and worker -- not an owner-scoped
    action, so an operator must not flip it (e.g. force-enable trajectory
    capture deployment-wide, or disable the whole learning loop)."""
    require_global_permission(request, "admin")
    from maverick_dashboard import settings_store
    try:
        settings_store.set_learning(
            payload.enabled,
            actor=caller_principal(request) or "local",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    from maverick.config import reset_config_cache
    reset_config_cache()
    return _learning_snapshot(request)


@router.post("/learning/dgm")
async def set_dgm_endpoint(request: Request, payload: DgmToggleIn) -> dict:
    """Arm/disarm research-only DGM without starting a cycle.

    This deployment-global control changes only ``[self_modify] enable``. The
    runner independently attests its editable surface, challenge corpus,
    budget, Git snapshot, HALT and evaluator before model execution, and has no
    live code-adoption path.
    """
    require_global_permission(request, "admin")
    if payload.enabled and not payload.acknowledge_research_only:
        raise HTTPException(
            status_code=400,
            detail="acknowledge_research_only=true is required to enable DGM",
        )
    from maverick import self_modify
    from maverick.config import reset_config_cache
    before = self_modify.production_status()
    blocker_codes = {item["code"] for item in before["blockers"]}
    if "config_source_error" in blocker_codes:
        raise HTTPException(
            status_code=409,
            detail="DGM control refused while an active global config source is invalid",
        )
    if before["control_managed"]:
        owner = {
            "environment": "MAVERICK_SELF_MODIFY in the deployment environment",
            "config_overlay": "MAVERICK_CONFIG_OVERLAY",
        }.get(before["managed_by"], "deployment policy")
        raise HTTPException(
            status_code=409,
            detail=f"DGM is managed by higher-precedence {owner}",
        )
    if payload.enabled and any(code.startswith("invalid_") for code in blocker_codes):
        raise HTTPException(
            status_code=409,
            detail="DGM enable refused until its boolean configuration is valid",
        )
    from maverick_dashboard import settings_store
    try:
        settings_store.set_dgm(
            payload.enabled,
            actor=caller_principal(request) or "local",
            acknowledged=payload.acknowledge_research_only,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    reset_config_cache()
    return _learning_snapshot(request)


@router.post("/learning/flow-autonomy")
async def set_flow_autonomy_endpoint(request: Request, payload: FlowAutonomyIn) -> dict:
    """Turn the AUTONOMOUS flow self-improvement loop on/off -- its own opt-in,
    kept off the blanket learning button because autonomously rewriting a live
    workflow is a heavier consequence than passive learning.

    Two independent knobs: ``auto_evolve`` lets the loop revert a rewrite it
    measures as a regression; ``auto_apply`` (which needs auto_evolve on for the
    revert safety net) lets it apply an improvement forward without a human. Same
    admin gate + deployment-wide overlay as the blanket toggle."""
    require_global_permission(request, "admin")
    from maverick_dashboard import settings_store
    settings_store.set_flow_autonomy(auto_evolve=payload.auto_evolve,
                                     auto_apply=payload.auto_apply)
    from maverick.config import reset_config_cache
    reset_config_cache()   # so the refreshed snapshot reflects the new overlay
    return _learning_snapshot(request)


@router.get("/marketplace/packs")
async def marketplace_packs_api(request: Request, q: str = "") -> dict:
    """Pack marketplace grouped by department, or a flat search when ``q`` set.

    Suite-scoped like /departments: a caller with a department grant browses
    only their departments' shelves; unscoped callers see the full store.
    """
    from maverick.marketplace.storefront import pack_marketplace, search_packs
    allowed = caller_suites(request)
    query = (q or "").strip()
    if query:
        results = search_packs(query)
        if allowed is not None:
            results = [r for r in results if r.get("suite") in allowed]
        return {"query": query, "results": results}
    departments = pack_marketplace()
    if allowed is not None:
        departments = [d for d in departments if d.get("key") in allowed]
    return {"departments": departments}


@router.get("/run-tree/{goal_id}")
async def run_tree_api(request: Request, goal_id: int) -> dict:
    """One run's fork tree: the branch that shipped and the counterfactuals
    recorded beside it, nested under their common root.

    Forks inherit the parent's owner, so the tree is returned from the run's
    root and BOTH ends are access-checked — a caller who may not read the root
    gets the subtree they asked for instead. Missing lineage is not an error:
    an un-forked run answers with itself and no children.
    """
    from maverick import session_tree
    world = _world()
    goal = world.get_goal(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="no such goal")
    assert_goal_access(request, goal)
    lineage = session_tree.lineage(goal_id)
    root = lineage["root"]
    if root != goal_id:
        root_goal = world.get_goal(root)
        if root_goal is None or not can_access_goal(request, root_goal):
            root = goal_id
    return {"enabled": session_tree.enabled(), "goal_id": goal_id,
            "root": root, "lineage": lineage, "tree": session_tree.tree(root)}


@router.get("/marketplace/connectors")
async def marketplace_connectors_api(request: Request, q: str = "") -> dict:
    """Connector marketplace: honest total + optional substring search."""
    from maverick.marketplace.storefront import connector_marketplace
    return connector_marketplace(q or None)


# Department router: security/GRC records and both defensive hunters. Imported
# at the end so its lazy actor resolver can call this module's strict
# ``_request_actor`` without a circular import during module initialization.
from .finance_api import router as finance_operations_router  # noqa: E402
from .security_api import router as security_router  # noqa: E402

router.include_router(security_router)
router.include_router(finance_operations_router)
