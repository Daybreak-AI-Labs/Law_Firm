"""Pydantic request/response models for the dashboard REST API.

Extracted verbatim from api.py to keep that module focused on routing.
Importing here changes no behavior: these are pure data schemas.
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, model_validator

_HUNTER_BATCH_BYTES = 16 * 1024 * 1024
_HUNTER_EVENT_BYTES = 256 * 1024
_HUNTER_MAX_DEPTH = 12
_HUNTER_MAX_CONTAINER_ITEMS = 2_048


def _validate_hunter_events(events: list[dict], *, label: str) -> int:
    """Bound serialized size and structural complexity of telemetry events."""
    total = 0
    for index, event in enumerate(events):
        try:
            encoded = json.dumps(
                event, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} event {index} is not valid JSON") from exc
        if len(encoded) > _HUNTER_EVENT_BYTES:
            raise ValueError(
                f"{label} event {index} exceeds the 256 KiB per-event limit"
            )
        total += len(encoded)
        if total > _HUNTER_BATCH_BYTES:
            raise ValueError(f"{label} events exceed the 16 MiB aggregate limit")
        stack: list[tuple[object, int]] = [(event, 0)]
        nodes = 0
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if depth > _HUNTER_MAX_DEPTH:
                raise ValueError(
                    f"{label} event {index} exceeds the maximum nesting depth"
                )
            if nodes > 20_000:
                raise ValueError(f"{label} event {index} is too structurally complex")
            if isinstance(value, dict):
                if len(value) > _HUNTER_MAX_CONTAINER_ITEMS:
                    raise ValueError(f"{label} event {index} has too many object keys")
                stack.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                if len(value) > _HUNTER_MAX_CONTAINER_ITEMS:
                    raise ValueError(f"{label} event {index} has an oversized array")
                stack.extend((item, depth + 1) for item in value)
    return total


class WorkflowStepIn(BaseModel):
    name: str = Field(..., max_length=80)
    instruction: str = ""
    tools: list[str] = Field(default_factory=list)
    gate: str | None = None


class AgentOverrideIn(BaseModel):
    """A tenant override patch for a domain pack (agent). Every field is
    optional: only the ones the client actually sets are persisted, so the
    pack inherits everything else from its built-in base. ``extends`` forks a
    base under a new name; omit it to customize the pack in place."""
    description: str | None = None
    persona: str | None = None
    allow_tools: list[str] | None = None
    deny_tools: list[str] | None = None
    max_risk: str | None = None
    knowledge_sources: list[str] | None = None
    models: dict[str, str] | None = None
    workflow: list[WorkflowStepIn] | None = None
    compartment: str | None = None
    extends: str | None = None


class JDMatchIn(BaseModel):
    """A job description to rank the specialist roster against. Bounded like a
    goal description: JD text flows into scoring and draft prompts."""
    jd_text: str = Field(..., min_length=1, max_length=16000)
    k: int = Field(5, ge=1, le=20)


class JDDraftIn(BaseModel):
    """Draft (not save) a new specialist pack from a job description. The
    result prefills the pack editor; persisting stays the editor's existing
    human-approved save."""
    role_title: str = Field(..., min_length=1, max_length=120)
    jd_text: str = Field(..., min_length=1, max_length=16000)
    industry: str = Field("", max_length=120)


class FleetAgentAddIn(BaseModel):
    """Add one specialist pack to a fleet roster (the JD-hire deploy step)."""
    pack: str = Field(..., min_length=1, max_length=120)
    role: str | None = Field(default=None, max_length=64)


class AssessmentRefreshIn(BaseModel):
    """Auto-draft (or re-draft) a governance assessment. ``template`` selects
    which lenses to run; ``None`` keeps the existing/default one."""
    template: str | None = Field(default=None, max_length=64)


class AssessmentReviewIn(BaseModel):
    """A reviewer's decision on one lens of an assessment."""
    lens: str = Field(..., pattern="^(privacy|security|ai_risk)$")
    decision: str = Field(..., pattern="^(accepted|needs_work)$")
    note: str | None = Field(default=None, max_length=4000)
    cadence_days: int | None = Field(default=None, ge=0, le=3650)


class AssessmentEvidenceIn(BaseModel):
    """An evidence note/link attached to a specific finding."""
    lens: str = Field(..., pattern="^(privacy|security|ai_risk)$")
    control: str = Field(..., max_length=200)
    evidence: str = Field(..., max_length=2000)


class SignoffIn(BaseModel):
    """A human's sign-off on a finished, gated deliverable -- the certify/reject
    decision the pack's output gate calls for, bound to the version the client
    actually rendered, with an optional review note."""
    decision: str = Field(..., pattern="^(approved|rejected)$")
    expected_updated_at: float = Field(..., gt=0)
    note: str | None = Field(default=None, max_length=2000)


class RoleOverrideIn(BaseModel):
    """A per-tenant override for a core agent role: a system-prompt addendum
    appended to the role's base template, plus optional model/effort overrides
    that win over the global [models]/[effort] config. Empty fields clear; an
    all-empty patch clears the role's override."""
    system_addendum: str | None = None
    model: str | None = None
    effort: str | None = None


class GoalIn(BaseModel):
    title: str = Field(..., max_length=200)
    # Bound the description like the title: it flows into SQLite, the at-rest
    # seal, and every downstream prompt, so an unbounded value from an untrusted
    # REST caller is a cost / DB-bloat amplification vector. 16k chars is ample
    # for a goal brief; oversized payloads get a 422 rather than silent bloat.
    description: str = Field("", max_length=16000)
    max_dollars: float = Field(5.0, ge=0.0, le=100.0)
    max_wall_seconds: float = Field(3600.0, ge=1.0, le=86400.0)
    max_depth: int = Field(3, ge=1, le=5)
    template: str | None = None
    params: dict[str, str] | None = None
    # Run the goal AS a specialist pack: the orchestrator inherits a domain
    # stamped on the goal row (persona + capability envelope + compartment).
    domain: str | None = Field(None, max_length=120)


class GoalOut(BaseModel):
    id: int
    status: str
    title: str
    description: str | None = None
    result: str | None = None


class ScheduleIn(BaseModel):
    """Arm a recurring run on a 5-field cron expression. Provide either a saved
    ``template`` (rendered with ``params`` each fire) or a raw ``text`` prompt.
    Executed by ``maverick worker`` as a ``start_goal`` job."""
    cron: str = Field(..., max_length=120)
    template: str | None = None
    params: dict[str, str] | None = None
    text: str | None = None
    title: str | None = Field(None, max_length=200)


class ScheduleOut(BaseModel):
    id: int
    cron: str
    kind: str
    title: str
    next_run: float
    # Stable across cron re-arms (the job id changes each occurrence); used to
    # group a schedule's run history. Empty for schedules armed before v15.
    schedule_id: str = ""


class ImportRunIn(BaseModel):
    """Import automations from an external platform into Maverick templates.

    ``source`` is one of the registered importers (n8n/make/.../zapier). Provide
    ``definitions`` (exported definition JSON objects) for an offline/connect
    import, or omit it to live-fetch from the platform's API (server env creds).
    ``dry_run`` previews without writing; ``activate_schedules`` auto-creates
    schedules for recovered cron triggers (``None`` = fall back to the
    ``[automation_import] create_schedules`` config default);
    ``create_webhook_triggers`` wires an inbound webhook trigger for each
    webhook-triggered automation.
    """
    source: str = Field(..., max_length=40)
    definitions: list[dict] | None = Field(default=None, max_length=25)
    dry_run: bool = False
    activate_schedules: bool | None = None
    create_webhook_triggers: bool = False
    # Also lower each automation to a real flow GRAPH (branch/loop structure
    # preserved where the source exposes it) and save it, returning the per-step
    # migration fidelity report. Requires the flow engine.
    as_flows: bool = False


class ImportResultOut(BaseModel):
    source: str
    name: str
    template: str
    created: bool
    trigger: str
    webhook_trigger: str | None = None
    schedule: dict | None = None
    tools: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # Set when as_flows: the saved flow's id + the per-step fidelity log
    # ({step, node, kind, fidelity: preserved|approximated, note}).
    flow: str | None = None
    fidelity: list[dict] = Field(default_factory=list)


class ImportRunOut(BaseModel):
    imported: list[ImportResultOut]
    dry_run: bool
    webhook_url: str
    secret_configured: bool
    # How many supplied definitions could not be translated (malformed / an
    # unsupported shape) and were dropped. Surfaced so a partial import doesn't
    # look like a clean success.
    skipped: int = 0


class TriggerIn(BaseModel):
    """Bind a saved template OR flow to an inbound webhook. Set exactly one of
    ``template`` / ``flow`` (a flow receives the webhook ``data`` as its run
    data). ``params`` are the operator's default values (template targets only);
    an HMAC-signed POST to /webhook/run may override declared params at fire
    time. ``name`` defaults to the target name (slugified)."""
    template: str = Field("", max_length=80)
    flow: str = Field("", max_length=120)
    params: dict[str, str] | None = None
    name: str | None = Field(None, max_length=48)


class TriggerOut(BaseModel):
    name: str
    template: str
    flow: str = ""
    params: dict[str, str] = Field(default_factory=dict)
    webhook_url: str
    secret_configured: bool


class EventTriggerIn(BaseModel):
    """Bind a saved template OR flow to a polled event source ("when X appears,
    run this"). ``source`` is a registered EventSource; ``config`` is its settings
    (e.g. the ``url`` for http_json). ``params`` are default values; each polled
    event's fields fill declared params at fire time. Set exactly one of
    ``template`` / ``flow`` (a flow receives the event fields as its run data).
    ``name`` defaults to the target name (slugified)."""
    template: str = Field("", max_length=80)
    flow: str = Field("", max_length=120)
    source: str = Field(..., max_length=40)
    config: dict = Field(default_factory=dict)
    params: dict[str, str] | None = None
    name: str | None = Field(None, max_length=48)
    # Per-trigger poll cadence in seconds (0 = the scheduler default). Floored at
    # 60s by the scheduler; a day cap keeps a fat-fingered value sane.
    interval_seconds: int = Field(0, ge=0, le=86_400)


class EventTriggerOut(BaseModel):
    name: str
    template: str
    flow: str = ""
    source: str
    config: dict = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    cursor: str = ""
    interval_seconds: int = 0


class LearningToggleIn(BaseModel):
    """Turn the whole self-learning loop on/off from the dashboard (writes the
    learning knobs to the config overlay)."""
    enabled: bool


class FlowAutonomyIn(BaseModel):
    """Toggle the autonomous flow self-improvement loop ([flows] auto_evolve /
    auto_apply). Its own opt-in, distinct from the blanket learning button. Only
    the fields provided are changed; ``auto_apply`` needs ``auto_evolve`` on."""
    auto_evolve: bool | None = None
    auto_apply: bool | None = None


class GoalEventOut(BaseModel):
    id: int
    agent: str
    kind: str
    content: str
    ts: float


class GoalEventsResponse(BaseModel):
    status: str
    result: str | None
    next_id: int
    events: list[GoalEventOut]


class FactIn(BaseModel):
    key: str
    value: str


class OutcomeIn(BaseModel):
    """A real downstream outcome for a past episode (the Consequence Engine)."""
    goal_id: int
    episode_id: int
    value: float
    kind: str = ""


class FeedbackIn(BaseModel):
    """A human thumbs-up/down on a goal's result -- the always-available verdict
    (unlike the gated sign-off). Grounds a real outcome into the learning loop."""
    rating: str = Field(..., pattern="^(up|down)$")
    note: str | None = Field(default=None, max_length=2000)


class DeliverableEditIn(BaseModel):
    """A human's revised version of a goal's deliverable. How much they changed it
    (vs. the agent's draft) grounds a graded outcome -- a verbatim keep is a strong
    positive, a heavy rewrite is graded-negative ground truth."""
    text: str = Field(..., max_length=200_000)


class OutcomeLinkIn(BaseModel):
    """Link an external business key (``invoice:INV-42``) to the episode that
    acted on it, so a later outcome reported against that key can be grounded."""
    goal_id: int
    episode_id: int
    key: str = Field(..., min_length=1, max_length=256)


class OutcomeByKeyIn(BaseModel):
    """A real downstream outcome reported by a system of record using only the
    business key it owns -- resolved to the episode that acted via the link."""
    key: str = Field(..., min_length=1, max_length=256)
    value: float
    kind: str = ""


class OAuthAuthorizeIn(BaseModel):
    """Start a turnkey 'connect this account' flow for a preset provider: build
    the consent URL. The returned PKCE verifier is kept by the (admin) operator
    and passed back to /exchange -- no server-side state."""
    redirect_uri: str = Field(..., min_length=1, max_length=2048)
    scopes: list[str] | None = None
    client_id: str | None = Field(default=None, max_length=512)


class OAuthExchangeIn(BaseModel):
    """Finish the connect flow: swap the authorization code for tokens and seal
    them in the per-tenant vault under the provider name."""
    code: str = Field(..., min_length=1, max_length=4096)
    redirect_uri: str = Field(..., min_length=1, max_length=2048)
    verifier: str = Field(default="", max_length=256)
    client_id: str | None = Field(default=None, max_length=512)


class ConnectionIn(BaseModel):
    """Create/replace a named SaaS connection: the connector it credentials, its
    base URL, and an API token (sealed at rest, never returned). ``name`` lets
    one connector have several accounts (salesforce / salesforce-eu)."""
    name: str = Field(..., min_length=1, max_length=64)
    connector: str = Field("", max_length=64)
    base_url: str = Field("", max_length=2048)
    token: str = Field("", max_length=8192)
    # Credential use is separate from credential management. Owner is the
    # least-privilege default; tenant sharing must be explicit, and named grants
    # can admit selected principals without exposing the token itself.
    access: str = Field("owner", pattern="^(owner|tenant)$")
    allowed_principals: list[str] = Field(default_factory=list, max_length=100)


class FlowSaveIn(BaseModel):
    """A draft flow definition (a graph of typed nodes) to create or replace."""
    flow: dict


class FlowDryRunIn(BaseModel):
    """Safely execute the submitted unsaved draft with mock runners.

    The graph is part of the request on purpose: a dry run must exercise what is
    currently on the canvas, not save it and then rediscover it from mutable
    definition state.
    """
    flow: dict
    data: dict = Field(default_factory=dict)


class FlowPublishIn(BaseModel):
    """Activate the exact draft revision the operator reviewed."""
    version: int = Field(..., ge=1)
    revision: str = Field(..., min_length=1, max_length=128)


class FlowRunIn(BaseModel):
    """Start a real run of the exact published release with optional initial data.

    ``dry_run`` is retained only for compatibility and is rejected by this
    route. Safe tests submit the unsaved graph itself to ``/flows/dry-run`` so
    testing cannot accidentally save or activate canvas state."""
    data: dict = Field(default_factory=dict)
    dry_run: bool = False
    # Optional caller-supplied dedup key: a repeat POST with the same key returns
    # the existing run instead of starting a second one (double-click / retry safe).
    idempotency_key: str = Field(default="", max_length=200)


class FlowResumeIn(BaseModel):
    """Resume a paused flow run: the human verdict on the approval node, plus any
    free-form inputs to merge into the run data (so an approval can also collect
    a corrected value or a note, not just yes/no). Beyond approved/rejected the
    verdict may be one of the node's declared ``choices`` (the runner validates
    against them; an unlisted verdict pauses again). A paused_event run ignores
    the decision -- the event's ``inputs`` are the point."""
    # Empty remains representable for non-approval pauses, but approval routes
    # reject it explicitly.  Never turn a missing request field into consent.
    decision: str = Field(default="", max_length=40)
    inputs: dict = Field(default_factory=dict)


class FlowDraftIn(BaseModel):
    """Draft a flow from a plain-English description (the designer's ✨ button)."""
    description: str = Field(..., min_length=1, max_length=4000)
    flow_id: str = Field(default="", max_length=120)


class FlowChatIn(BaseModel):
    """One copilot turn in the designer's chat panel: the user's message, the
    CURRENT canvas graph (not necessarily saved), the recent transcript, and
    optionally a run id to ground diagnose/repair questions in a real trace.
    The reply may carry a patched flow -- the canvas applies it; nothing is
    saved server-side until the user saves."""
    message: str = Field(..., min_length=1, max_length=4000)
    flow: dict = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list, max_length=24)
    run_id: str = Field(default="", max_length=64)


class FlowApplyIn(BaseModel):
    """Apply a self-rewrite proposal: swap one node between agent and action.

    Hardening to an action needs ``tool`` and an explicit reviewed ``params``
    binding (including ``{}`` for a genuinely no-argument action); softening to
    an agent needs ``brief`` (else the existing brief/label is used)."""
    node_id: str = Field(..., min_length=1, max_length=120)
    to_kind: str = Field(..., pattern="^(agent|action)$")
    tool: str = Field(default="", max_length=120)
    # ``None`` means no binding evidence was supplied. An explicit ``{}`` is a
    # reviewed no-argument binding and remains distinguishable from omission.
    params: dict | None = None
    brief: str = Field(default="", max_length=4000)
    version: int | None = Field(default=None, ge=1)
    revision: str = Field(default="", max_length=128)


class FlowRollbackIn(BaseModel):
    """Roll a flow back to a prior version (default: the immediately previous)."""
    version: int | None = Field(default=None, ge=1)
    current_version: int | None = Field(default=None, ge=1)
    revision: str = Field(default="", max_length=128)


class AnswerIn(BaseModel):
    question_id: int
    answer: str


class SpeakIn(BaseModel):
    """Text for the dashboard read-aloud endpoint (POST /voice/speak)."""
    text: str
    voice: str | None = None


class SkillInstallIn(BaseModel):
    source: str = Field(..., description="https://... or gh:org/repo[:path]")


class SkillCreateIn(BaseModel):
    """Author a skill in the dashboard: a name, the trigger phrases that
    activate it, the tools it needs, and the instructions (markdown body)."""
    name: str = Field(..., max_length=80)
    instructions: str = Field(..., max_length=20000)
    triggers: list[str] = Field(default_factory=list)
    tools_needed: list[str] = Field(default_factory=list)


class SkillOut(BaseModel):
    name: str
    triggers: list[str]
    tools_needed: list[str]


class AttachmentOut(BaseModel):
    id: int
    filename: str
    mime: str
    size_bytes: int
    sha256: str


class CatalogInstallIn(BaseModel):
    name: str = Field(..., max_length=200)


class HaltIn(BaseModel):
    reason: str = Field("manual via dashboard", max_length=200)


class FleetRunIn(BaseModel):
    agent: str = Field(..., max_length=64)
    prompt: str = Field(..., max_length=8000)
    max_dollars: float | None = Field(None, ge=0.0, le=100.0)


class FleetAgentIn(BaseModel):
    name: str = Field(..., max_length=64)
    role: str = Field("", max_length=64)
    description: str = Field("", max_length=500)


class FleetCreateIn(BaseModel):
    name: str = Field(..., max_length=64)
    agents: list[FleetAgentIn] = Field(default_factory=list)


class RedactIn(BaseModel):
    text: str = Field(max_length=200_000)
    kinds: list[str] = Field(default_factory=list)  # empty = all kinds


class CachePurgeIn(BaseModel):
    scopes: list[str] = Field(default_factory=lambda: ["all"])


class RetitleIn(BaseModel):
    title: str = Field(..., max_length=200)


class ReparentIn(BaseModel):
    parent_id: int | None = None


class ChildIn(BaseModel):
    title: str = Field(..., max_length=200)
    description: str = ""


class ComposeIn(BaseModel):
    title: str = Field(..., max_length=200)
    steps: list[str] = Field(default_factory=list)
    budget_dollars: float | None = Field(None, ge=0.0, le=100.0)
    channel: str | None = Field(None, max_length=64)
    priority: str | None = Field(None, max_length=16)


class WorkflowDraftIn(BaseModel):
    """Chat-path drafting: a natural-language brief. ``form`` selects the
    artifact — a reusable ``"template"`` (default) or an agent ``"playbook"``."""
    description: str = Field("", max_length=8000)
    form: str = Field("template", max_length=16)


class WorkflowRefineIn(BaseModel):
    """Refine an existing draft with a follow-up instruction. ``current`` is the
    draft as last shown/edited; ``form`` selects template vs playbook parsing."""
    form: str = Field("template", max_length=16)
    instruction: str = Field(..., max_length=2000)
    current: dict = Field(default_factory=dict)


class WorkflowSaveIn(BaseModel):
    """Persist an (AI-drafted, possibly edited) workflow as a user template.

    ``overwrite`` must be true to replace an existing template of the same name
    (the ``?edit=`` flow); a fresh save leaves it false so an accidental name
    collision returns 400 instead of silently clobbering another template."""
    name: str = Field(..., max_length=48)
    title: str = Field(..., max_length=200)
    body: str = Field(..., max_length=20000)
    params: list[str] = Field(default_factory=list)
    budget_dollars: float = Field(5.0, ge=0.0, le=100.0)
    budget_wall_seconds: float = Field(3600.0, ge=1.0, le=86400.0)
    overwrite: bool = False
    expected_generation: int | None = Field(None, ge=0)


class TenantCreateIn(BaseModel):
    """Provision a new tenant (admin only)."""
    id: str = Field(..., max_length=128)
    plan: str = Field("free", pattern="^(free|pro|enterprise)$")
    display_name: str = Field("", max_length=200)
    max_daily_dollars: float = Field(0.0, ge=0.0)


class TenantPlanIn(BaseModel):
    plan: str = Field(..., pattern="^(free|pro|enterprise)$")


class TenantQuotaIn(BaseModel):
    max_daily_dollars: float = Field(..., ge=0.0)


class TenantOut(BaseModel):
    """A provisioned tenant. ``config_path`` is where to drop this tenant's own
    config.toml (its provider keys / model choices / budget overlay)."""
    id: str
    status: str
    plan: str
    display_name: str
    max_daily_dollars: float
    created_at: float
    updated_at: float
    config_path: str


class TenantRoleIn(BaseModel):
    """Grant a principal a role within one tenant (per-tenant RBAC)."""
    role: str = Field(..., pattern="^(admin|operator|auditor|viewer)$")


class UserSuitesIn(BaseModel):
    """Scope a principal to a set of department suites (job function)."""
    suites: list[str] = Field(default_factory=list, max_length=64)


class ClausePlaybookIn(BaseModel):
    """This deployment's own standard clause positions.

    Keys are clause ids from the Art. 28 / CCPA checklists; values are the
    exact language a vendor's paper gets redlined to. Omitted clauses keep the
    shipped position, so a partial playbook is a valid one."""
    positions: dict[str, str] = Field(default_factory=dict)


class DpaReviewIn(BaseModel):
    """Review a Data Processing Agreement against the Art. 28(3) checklist.
    ``text`` is the agreement body (pasted or extracted); bounded like other
    document inputs."""
    vendor: str = Field(..., min_length=1, max_length=200)
    document_name: str = Field("", max_length=300)
    text: str = Field(..., min_length=1, max_length=2_000_000)


class AiSystemIn(BaseModel):
    """Register an AI system in the governance registry; it is classified
    against the EU AI Act's tiers on the way in."""
    name: str = Field(..., min_length=1, max_length=200)
    purpose: str = Field(..., min_length=1, max_length=2000)
    provider: str = Field("", max_length=200)
    owner: str = Field("", max_length=200)
    assessment_id: str = Field("", max_length=64)


class RopaIn(BaseModel):
    """Create or update one Art. 30(1) record of processing activity. Only
    fields present are changed on update."""
    ropa_id: str = Field("", max_length=64)
    revision: int | None = Field(None, ge=1)
    activity: str | None = Field(None, max_length=2000)
    purpose: str | None = Field(None, max_length=2000)
    controller: str | None = Field(None, max_length=2000)
    data_categories: str | None = Field(None, max_length=2000)
    data_subjects: str | None = Field(None, max_length=2000)
    recipients: str | None = Field(None, max_length=2000)
    transfers: str | None = Field(None, max_length=2000)
    retention: str | None = Field(None, max_length=2000)
    security_measures: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def require_update_revision(self):
        if self.ropa_id and self.revision is None:
            raise ValueError("revision is required when ropa_id is provided")
        return self
    assessment_id: str = Field("", max_length=64)


class DpaFromDocumentIn(BaseModel):
    """Review a DPA fetched straight from a connected source (the hit comes
    from the dpa-documents search); ``ref`` is the opaque hit reference the
    source returned."""
    vendor: str = Field(..., min_length=1, max_length=200)
    source: str = Field(..., min_length=1, max_length=32)
    doc_id: str = Field(..., min_length=1, max_length=300)
    ref: dict | None = None
    document_name: str = Field("", max_length=300)


class OneTrustImportIn(BaseModel):
    """OneTrust RoPA / Data Mapping CSV export to import into the Art. 30
    register; bounded like other document inputs."""
    csv_text: str = Field(..., min_length=1, max_length=2_000_000)


class DsarIn(BaseModel):
    """Open a data-subject request with its statutory due date."""
    subject_id: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(..., pattern="^(access|portability|erasure)$")
    channel: str = Field("", max_length=64)


class IncidentIn(BaseModel):
    """Open a privacy-incident record; the Art. 33 72-hour clock starts at
    discovery. Whether it is a notifiable breach stays a separate, human
    decision."""
    title: str = Field(..., min_length=1, max_length=200)
    severity: str = Field("medium", pattern="^(low|medium|high)$")
    description: str = Field("", max_length=4000)
    categories: str = Field("", max_length=500)
    affected_estimate: str = Field("", max_length=100)


class IncidentNotifyIn(BaseModel):
    """The documented human call on Art. 33/34 notification -- both
    directions require a rationale (Art. 33(5) covers the 'no' too)."""
    notifiable: bool
    rationale: str = Field(..., min_length=1, max_length=2000)


class DsarFromMessageIn(BaseModel):
    """An inbound message (email/chat/web form) to triage into a tracked
    data-subject request. Detection is deterministic; no request is opened
    unless the message reads as one and names a subject."""
    text: str = Field(..., min_length=1, max_length=20000)
    sender: str = Field("", max_length=200)
    channel: str = Field("", max_length=64)


class FollowupsIn(BaseModel):
    """Reviewer follow-up questions to send back to an assessment's
    respondent (the pop-out's 'needs more detail' path)."""
    questions: list[str] = Field(..., min_length=1, max_length=20)
    expected_revision: int = Field(..., ge=0)


class TemplateQuestionIn(BaseModel):
    """One questionnaire question in a custom template."""
    id: str = Field("", max_length=60)
    section: str = Field("", max_length=80)
    text: str = Field(..., min_length=3, max_length=500)
    risk_answer: str = Field("no", pattern="^(yes|no)$")
    severity: str = Field("medium", pattern="^(low|medium|high)$")
    guidance: str = Field("", max_length=500)


class TemplateIn(BaseModel):
    """Publish an immutable assessment-questionnaire release.

    The revision+digest pair binds the admin's write to the exact release they
    reviewed and prevents stale/ABA publication.
    """
    title: str = Field(..., min_length=1, max_length=120)
    framework: str = Field(..., min_length=1, max_length=200)
    department: str = Field("privacy", pattern="^(privacy|finance|security)$")
    description: str = Field("", max_length=500)
    # ISO/IEC 27001's original-paraphrase built-in currently has 93 questions.
    # Keep publication bounded while allowing built-ins to round-trip through
    # the shared editor without an unavoidable 422.
    questions: list[TemplateQuestionIn] = Field(..., min_length=1,
                                                max_length=256)
    expected_revision: int = Field(..., ge=0)
    expected_digest: str = Field(..., max_length=64,
                                 pattern="^([0-9a-f]{64})?$")


class AssessmentAssignIn(BaseModel):
    """Route an assessment to a reviewer. An empty ``assignee`` unassigns it
    and returns it to the pool."""
    assignee: str = Field("", max_length=200)
    expected_revision: int = Field(..., ge=0)


class AssessmentDecideIn(BaseModel):
    """The reviewer's decision on a saved assessment. An approval with a
    cadence schedules the next review (the record comes due); 0/None means
    no re-review date."""
    decision: str = Field(..., pattern="^(approved|rejected)$")
    cadence_days: int | None = Field(default=365, ge=0, le=3650)
    note: str = Field("", max_length=4000)
    expected_revision: int = Field(..., ge=0)


class RiskAcceptIn(BaseModel):
    """Formally accept the residual risk on an assessment with a named owner,
    a rationale, and an expiry (days). Comes due again when it expires."""
    rationale: str = Field(..., min_length=1, max_length=4000)
    expires_days: int = Field(365, ge=1, le=3650)
    expected_revision: int = Field(..., ge=0)


class ReviewTriggerIn(BaseModel):
    """Force an assessment due for re-review now (a contract renewal, a new
    sub-processor, a material change) -- optionally with a renewal date that
    flips it due automatically when reached."""
    reason: str = Field("", max_length=400)
    renewal_at: float | None = Field(default=None, ge=0)
    expected_revision: int = Field(..., ge=0)


class BulkAssessmentImportIn(BaseModel):
    """Queue a batch of assessments from a pasted CSV (subject,type[,note])
    -- onboarding a whole vendor list without one-by-one intake."""
    csv: str = Field(..., min_length=1, max_length=200_000)


class DocDiscoverIn(BaseModel):
    """Search connected document sources (Microsoft Graph / Slack / Google
    Drive) for documents related to an assessment subject -- the SOW,
    contract, DPA a respondent would otherwise hunt for by hand."""
    subject: str = Field(..., min_length=1, max_length=300)
    sources: list[str] | None = Field(default=None, max_length=8)
    limit: int = Field(8, ge=1, le=25)


class SourceAttachIn(BaseModel):
    """Fetch one discovered document from its source and attach it to a goal
    as evidence (the one-click 'attach it for me'). ``ref`` round-trips the
    opaque fetch details discovery returned for the hit."""
    source: str = Field(..., min_length=1, max_length=40)
    doc_id: str = Field(..., min_length=1, max_length=500)
    name: str = Field("", max_length=300)
    ref: dict = Field(default_factory=dict)


class ValueDeptIn(BaseModel):
    """One department's cost/value override on the Savings page. Both fields
    optional so a client can override just the rate; an all-None row clears
    the override (reverts the department to the global assumption)."""
    hourly_rate: float | None = Field(None, ge=0.0, le=100_000.0)
    hours_per_task: float | None = Field(None, ge=0.0, le=10_000.0)


class ValueAssumptionsIn(BaseModel):
    """The client's own cost/value inputs behind the Savings report: what one
    hour of comparable human work costs them (fully loaded) and how many human
    hours one deliverable would take by hand. Bounds are sanity rails, not
    policy -- the number is theirs. Only fields present are changed."""
    hourly_rate: float | None = Field(None, ge=0.0, le=100_000.0)
    hours_per_task: float | None = Field(None, ge=0.0, le=10_000.0)
    currency: str | None = Field(None, max_length=8, pattern="^[A-Za-z]{3,8}$")
    departments: dict[str, ValueDeptIn | None] = Field(default_factory=dict)


# Security & GRC ------------------------------------------------------------

class SecurityControlIn(BaseModel):
    framework: str = Field(..., min_length=1, max_length=120)
    control_id: str = Field(..., min_length=1, max_length=160)
    title: str = Field(..., min_length=1, max_length=500)
    implementation_status: str = Field(
        "not_started",
        pattern="^(not_started|not_implemented|planned|not_applicable)$",
    )
    owner: str = Field(..., min_length=1, max_length=200)
    applicable: bool = True
    applicability_rationale: str = Field("", max_length=2000)
    crosswalk: list[str] = Field(default_factory=list, max_length=256)
    evidence_ids: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def require_nonblank_owner(self):
        self.owner = self.owner.strip()
        if not self.owner:
            raise ValueError("control owner must not be blank")
        return self


class SecurityControlUpdateIn(BaseModel):
    expected_revision: int = Field(..., ge=1)
    title: str | None = Field(None, min_length=1, max_length=500)
    implementation_status: str | None = Field(
        None,
        pattern="^(not_started|not_implemented|planned|not_applicable)$",
    )
    owner: str | None = Field(None, min_length=1, max_length=200)
    applicable: bool | None = None
    applicability_rationale: str | None = Field(None, max_length=2000)
    crosswalk: list[str] | None = Field(None, max_length=256)
    evidence_ids: list[str] | None = Field(None, max_length=256)

    @model_validator(mode="after")
    def reject_blank_owner(self):
        if self.owner is not None:
            self.owner = self.owner.strip()
            if not self.owner:
                raise ValueError("control owner must not be blank")
        return self


class SecurityEvidenceIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    text: str = Field(..., min_length=1, max_length=2_000_000)
    source: str = Field("paste", max_length=80)
    control_ids: list[str] = Field(default_factory=list, max_length=256)


class SecurityEvidenceFromDocumentIn(BaseModel):
    """Map one connector-fetched document as untrusted, review-gated evidence."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=300)
    source: str = Field(..., min_length=1, max_length=32)
    doc_id: str = Field(..., min_length=1, max_length=300)
    ref: dict | None = None
    control_ids: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def bound_opaque_reference_and_control_ids(self):
        if self.ref is not None:
            _validate_hunter_events([self.ref], label="document reference")
            encoded = json.dumps(
                self.ref,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > 64 * 1024:
                raise ValueError("document reference exceeds the 64 KiB limit")
        cleaned = []
        for control_id in self.control_ids:
            value = str(control_id).strip()
            if not value or len(value) > 200:
                raise ValueError("control IDs must be nonblank and at most 200 characters")
            cleaned.append(value)
        self.control_ids = cleaned
        return self


class SecurityEvidenceDecisionIn(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected)$")
    rationale: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=1)


class SecurityEvidenceApplyIn(BaseModel):
    evidence_id: str = Field(..., min_length=1, max_length=80)
    implementation_status: str = Field(
        ..., pattern="^(partial|implemented)$"
    )
    expected_revision: int = Field(..., ge=1)


class SecurityRiskIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    likelihood: int = Field(..., ge=1, le=5)
    impact: int = Field(..., ge=1, le=5)
    likelihood_rationale: str = Field(..., min_length=1, max_length=4000)
    impact_rationale: str = Field(..., min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(..., min_length=1, max_length=256)
    owner: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=4000)
    control_ids: list[str] = Field(default_factory=list, max_length=256)


class SecurityRiskTreatmentIn(BaseModel):
    treatment: str = Field(..., pattern="^(accept|mitigate|transfer|avoid)$")
    plan: str = Field(..., min_length=1, max_length=4000)
    residual_likelihood: int = Field(..., ge=1, le=5)
    residual_impact: int = Field(..., ge=1, le=5)
    residual_likelihood_rationale: str = Field(..., min_length=1, max_length=4000)
    residual_impact_rationale: str = Field(..., min_length=1, max_length=4000)
    evidence_ids: list[str] = Field(..., min_length=1, max_length=256)
    owner: str = Field(..., min_length=1, max_length=200)
    expected_revision: int = Field(..., ge=1)


class SecurityRiskExceptionIn(BaseModel):
    owner: str = Field(..., min_length=1, max_length=200)
    rationale: str = Field(..., min_length=1, max_length=4000)
    expires_at: float = Field(..., gt=0)
    expected_revision: int = Field(..., ge=1)


class SecurityCloseIn(BaseModel):
    rationale: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=1)


class SecurityPoamIn(BaseModel):
    finding: str = Field(..., min_length=1, max_length=2000)
    owner: str = Field(..., min_length=1, max_length=200)
    due_at: float = Field(..., gt=0)
    control_ids: list[str] = Field(default_factory=list, max_length=256)
    milestones: list[dict] = Field(default_factory=list, max_length=100)


class SecurityPoamUpdateIn(BaseModel):
    status: str = Field(
        ..., pattern="^(open|in_progress|blocked|complete|closed|accepted)$"
    )
    owner: str | None = Field(None, max_length=200)
    due_at: float | None = Field(None, gt=0)
    milestones: list[dict] | None = Field(None, max_length=100)
    note: str = Field("", max_length=2000)
    expected_revision: int = Field(..., ge=1)


class SecurityReviewTriggerIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    expected_revision: int = Field(..., ge=1)


class SecurityVendorIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=300)
    posture: dict = Field(default_factory=dict)
    criticality: str = Field("medium", pattern="^(low|medium|high|critical)$")
    owner: str = Field("", max_length=200)
    services: str = Field("", max_length=2000)
    assessment_id: str = Field("", max_length=80)
    carry_forward_from: str = Field("", max_length=80)


class SecurityDecisionIn(BaseModel):
    decision: str = Field(..., pattern="^(approved|needs_work|rejected)$")
    rationale: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=1)


class SecurityPolicyIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    owner: str = Field(..., min_length=1, max_length=200)
    content_ref: str = Field("", max_length=2000)
    review_cadence_days: int = Field(365, ge=1, le=3650)


class SecurityPolicyTransitionIn(BaseModel):
    target_status: str = Field(
        ..., pattern="^(draft|review|approved|attested|retired)$"
    )
    note: str = Field("", max_length=2000)
    expected_revision: int = Field(..., ge=1)


class SecurityPolicyAttestIn(BaseModel):
    subject: str = Field(..., min_length=1, max_length=300)
    statement: str = Field(..., min_length=1, max_length=2000)
    expected_revision: int = Field(..., ge=1)


class SecurityIncidentIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    severity: str = Field("medium", pattern="^(low|medium|high|critical)$")
    description: str = Field("", max_length=8000)
    mitre_techniques: list[str] = Field(default_factory=list, max_length=128)
    clock_ids: list[str] = Field(default_factory=list, max_length=64)
    custom_clocks: list[dict] = Field(default_factory=list, max_length=32)
    discovered_at: float | None = Field(None, gt=0)


class SecurityIncidentPhaseIn(BaseModel):
    phase: str = Field(..., pattern="^(containment|eradication|recovery)$")
    note: str = Field(..., min_length=1, max_length=4000)
    occurred_at: float | None = Field(None, gt=0)
    expected_revision: int = Field(..., ge=1)


class SecurityIncidentClockIn(BaseModel):
    clock_id: str = Field(..., min_length=1, max_length=120)
    anchor_at: float = Field(..., gt=0)
    expected_revision: int = Field(..., ge=1)


class SecurityIncidentNotifyIn(BaseModel):
    clock_id: str = Field(..., min_length=1, max_length=120)
    notifiable: bool
    rationale: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=1)


class SecurityIncidentCloseIn(BaseModel):
    summary: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=1)


class SecurityClockIn(BaseModel):
    clock_id: str = Field("", max_length=120)
    title: str = Field(..., min_length=1, max_length=300)
    jurisdiction: str = Field(..., min_length=1, max_length=120)
    source_url: str = Field(..., min_length=1, max_length=2000)
    source_title: str = Field(..., min_length=1, max_length=500)
    anchor: str = Field(..., min_length=1, max_length=500)
    deadline_seconds: int | None = Field(None, ge=1)
    deadline_kind: str = Field("elapsed", pattern="^(elapsed|calendar|business|asap)$")
    status: str = Field("current", pattern="^(current|proposed|superseded|custom)$")
    effective: str = Field("", max_length=80)
    expected_revision: int | None = Field(None, ge=1)


class SecurityEngagementIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=300)
    framework: str = Field(..., min_length=1, max_length=120)
    scope: str = Field(..., min_length=1, max_length=4000)
    owner: str = Field(..., min_length=1, max_length=200)
    due_at: float | None = Field(None, gt=0)


class SecurityEvidenceRequestIn(BaseModel):
    description: str = Field(..., min_length=1, max_length=2000)
    owner: str = Field(..., min_length=1, max_length=200)
    due_at: float = Field(..., gt=0)
    control_ids: list[str] = Field(default_factory=list, max_length=256)
    expected_revision: int = Field(..., ge=1)


class SecurityEvidenceRequestUpdateIn(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=120)
    status: str = Field(..., pattern="^(open|submitted|accepted|rejected|closed)$")
    evidence_ids: list[str] = Field(default_factory=list, max_length=256)
    expected_revision: int = Field(..., ge=1)


class SecurityControlTestIn(BaseModel):
    control_id: str = Field(..., min_length=1, max_length=160)
    procedure: str = Field(..., min_length=1, max_length=4000)
    result: str = Field(..., pattern="^(pass|fail|needs_review)$")
    evidence_ids: list[str] = Field(default_factory=list, max_length=256)
    expected_revision: int = Field(..., ge=1)


class SecurityAuditFindingIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    severity: str = Field(..., pattern="^(low|medium|high|critical)$")
    control_ids: list[str] = Field(default_factory=list, max_length=256)
    owner: str = Field("", max_length=200)
    due_at: float | None = Field(None, gt=0)
    expected_revision: int = Field(..., ge=1)


class SecurityAuditFindingUpdateIn(BaseModel):
    finding_id: str = Field(..., min_length=1, max_length=120)
    status: str = Field(
        ..., pattern="^(open|in_progress|resolved|remediated|accepted|closed)$"
    )
    expected_revision: int = Field(..., ge=1)


class SecurityEngagementStatusIn(BaseModel):
    status: str = Field(
        ..., pattern="^(planning|planned|fieldwork|review|complete|closed)$"
    )
    expected_revision: int = Field(..., ge=1)


# Defensive hunters ---------------------------------------------------------

class PlatformHuntScanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    since_hours: int = Field(24, ge=1, le=24 * 365)


class HuntRecordUpdateIn(BaseModel):
    changes: dict = Field(..., min_length=1, max_length=64)
    expected_revision: int = Field(..., ge=1)


class PlatformInvestigationIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    finding_ids: list[str] = Field(..., min_length=1, max_length=100)
    summary: str = Field(..., min_length=1, max_length=8000)


class PlatformContainmentIn(BaseModel):
    action: str = Field(..., min_length=1, max_length=300)
    scope: str = Field(..., min_length=1, max_length=1000)
    reason: str = Field(..., min_length=1, max_length=4000)
    expected_revision: int = Field(..., ge=1)


class EnvironmentIngestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector: str = Field(
        ...,
        pattern="^(cloudtrail|guardduty|syslog|edr|splunk|elastic|sentinel|kubernetes_audit|okta|entra)$",
    )
    events: list[dict] | None = Field(None, min_length=1, max_length=10_000)
    start: float | None = Field(None, ge=0)
    end: float | None = Field(None, ge=0)
    query: str = Field("*", min_length=1, max_length=4096)
    filters: dict = Field(default_factory=dict, max_length=32)
    limit: int = Field(1000, ge=1, le=1000)
    sigma_rules: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def bound_sigma_content(self):
        if self.events is None:
            if self.start is None or self.end is None:
                raise ValueError("registered connector queries require start and end")
        elif self.start is not None or self.end is not None or self.filters or self.query != "*":
            raise ValueError("push ingestion cannot include registered connector query fields")
        event_bytes = _validate_hunter_events(self.events or [], label="environment")
        if any(len(text.encode("utf-8")) > 1_048_576 for text in self.sigma_rules):
            raise ValueError("each Sigma document must be at most 1 MiB")
        sigma_bytes = sum(len(text.encode("utf-8")) for text in self.sigma_rules)
        if event_bytes + sigma_bytes > _HUNTER_BATCH_BYTES:
            raise ValueError("environment telemetry exceeds the 16 MiB aggregate limit")
        return self


class EnvironmentResponseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        ...,
        pattern="^(isolate_host|disable_identity|disable_key|rotate_credential|block_destination|revoke_session)$",
    )
    target: str = Field(..., min_length=1, max_length=1000)
    reason: str = Field(..., min_length=1, max_length=4000)
    finding_id: str = Field(..., min_length=1, max_length=120)
    investigation_id: str = Field(..., min_length=1, max_length=120)
    expected_revision: int = Field(..., ge=1)
    executor: str = Field(..., pattern="^[a-z][a-z0-9_.-]{0,119}$")
    parameters: dict = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def bound_response_parameters(self):
        from maverick.env_hunt import validate_response_parameters

        self.parameters = validate_response_parameters(self.parameters)
        return self


class EnvironmentExecuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: str = Field(..., min_length=1, max_length=120)
    proposal_id: str = Field(..., min_length=1, max_length=120)
    approval_id: int = Field(..., ge=1)
    executor: str = Field(..., pattern="^[a-z][a-z0-9_.-]{0,119}$")
    signature: str = Field(..., min_length=128, max_length=128, pattern="^[0-9a-fA-F]+$")
    expected_revision: int = Field(..., ge=1)


class ModelCostTierIn(BaseModel):
    model: str = Field(..., min_length=1, max_length=200)
    band: str | None = Field(None, pattern="^(low|medium|high|very_high)$")


class LicenseIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    jurisdiction: str = Field(..., min_length=1, max_length=120)
    authority: str = Field(..., min_length=1, max_length=200)
    license_number: str = Field(..., min_length=1, max_length=120)
    holder: str | None = Field(None, max_length=200)
    status: str = Field("active",
                        pattern="^(active|pending|lapsed|surrendered)$")
    renewal_at: float | None = Field(None, ge=0)
    cadence_days: int = Field(365, ge=1, le=3650)


class LicenseNoteIn(BaseModel):
    note: str | None = Field(None, max_length=2000)
    filename: str | None = Field(None, max_length=300)


class PartnerTenantIn(BaseModel):
    """One client deployment in the partner fleet registry. The base URL is
    the agent's root (its /health and /value.json hang off it); the optional
    bearer token is stored server-side and never echoed back."""
    name: str = Field(..., min_length=1, max_length=120)
    base_url: str = Field(..., min_length=8, max_length=300,
                          pattern="^https?://")
    token: str = Field("", max_length=300)
    theme: str = Field("", max_length=60)
    notes: str = Field("", max_length=500)


class FeatureSwitchIn(BaseModel):
    """Flip one allowlisted platform system on/off from inside the app."""
    section: str
    enabled: bool


class CopilotIn(BaseModel):
    """One turn of the platform copilot (the lower-right helper)."""
    message: str
    page: str = ""
    history: list = []
