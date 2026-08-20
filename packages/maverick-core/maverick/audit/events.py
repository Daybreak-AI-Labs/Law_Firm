"""Audit event schema. Versioned, additive-only.

To add a new event kind:
  1. Add it to ``EventKind`` here.
  2. Bump ``SCHEMA_VERSION`` if the payload shape changes for an
     EXISTING kind (new kinds are additive — no bump needed).
  3. Document the payload shape in this file's module docstring.

Payload shapes (kind -> required fields, all events also carry
``ts``, ``goal_id``, ``agent``, ``kind``):

  goal_start:        title:str, description:str|None
  goal_end:          status:str (succeeded|failed|cancelled), result:str|None
  episode_start:     attempt:int, model:str
  episode_end:       outcome:str, cost_dollars:float, in_tok:int, out_tok:int
  tool_call:         name:str, input_summary:str (truncated)
  tool_result:       name:str, status:str, output_summary:str
  shield_block:      stage:str (input|tool|output), reason:str, score:float|None
  capability_denied: tool:str, principal:str, channel:str|None, user_id:str|None
  egress_blocked:    provider:str (enterprise-mode egress lock denial)
  knowledge_egress:  provider:str, host:str, model:str, chunks:int, bytes:int,
                     content_sha256:str — a batch of document text was sent to a
                     third-party embedding vendor. Bounded metadata and a content
                     commitment only; never the chunk text itself.
  consent_prompt:    action:str, risk:str (low|medium|high|critical)
  consent_result:    decision:str (approve|deny|timeout)
  secret_redacted:   tool_name:str, pattern:str, count:int
  erase:             channel:str, erasure_id:str (random token, never subject-derived)
  halt:              source:str (file|signal|manual), detail:str|None
  privacy_record_changed: event_id:str, occurred_at:float, actor:str, tenant:str,
                     record_type:str, action:str, record_id:str, revision:int,
                     status:str, record_sha256:str (opaque control metadata and
                     a content commitment only; never DPA text, subject ids,
                     RoPA content, or export bodies)
  threat_hunt_record_changed: event_id:str, record_type:str, record_id:str,
                     revision:int, action:str, actor:str, record_sha256:str,
                     status:str, occurred_at:float
  matter_intake:     actor:str, operation:str
                     (preflight|open_matter|add_party), candidate_count:int,
                     matter_id:int|None, client_id:int|None. Never party names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_valid_day(day: Any) -> bool:
    """True iff ``day`` is a real ``YYYY-MM-DD`` calendar date.

    An audit ``day`` becomes a filesystem path component
    (``<audit_dir>/<day>.ndjson``), so anything that isn't this exact shape
    -- ``..``, a path separator, an absolute path, a NUL -- could escape the
    audit dir. The anchored shape check is the path-safety gate; on top of it
    we require a genuine calendar date, so a typo'd ``--day`` (``2026-13-99``,
    ``2026-02-30``) is a friendly error instead of a misleading "no entries /
    OK" on a day-file that could never exist (UX finding). Every code path that
    builds a day-file path from an untrusted ``day`` must gate on this first.
    (Mirrors the dashboard's own ``safe_audit_day`` HTTP-boundary guard.)
    """
    if not (isinstance(day, str) and _DAY_RE.match(day)):
        return False
    import datetime
    try:
        datetime.date.fromisoformat(day)
    except ValueError:
        return False
    return True


class EventKind:
    """Stringly-typed event kinds. Use these constants, not bare strings."""
    GOAL_START      = "goal_start"
    GOAL_END        = "goal_end"
    EPISODE_START   = "episode_start"
    EPISODE_END     = "episode_end"
    TOOL_CALL       = "tool_call"
    TOOL_RESULT     = "tool_result"
    SHIELD_BLOCK    = "shield_block"
    CAPABILITY_DENIED = "capability_denied"
    SANDBOX_DENIED = "sandbox_denied"
    # Per-call token exchange (zero-trust): one row per minted, single-tool,
    # short-lived capability token, so the Operating Record shows the scoped
    # credential each tool call actually ran under -- not just the run-long grant.
    TOKEN_EXCHANGE = "token_exchange"
    GOVERNANCE_DENIED = "governance_denied"
    AUTONOMY_ESCALATED = "autonomy_escalated"
    AUTONOMY_GATED = "autonomy_gated"
    EGRESS_BLOCKED  = "egress_blocked"
    # Document text left the deployment for a third-party embedding vendor.
    # Indexing a matter ships the documents themselves, not a prompt about
    # them, so this is the one egress a privilege log has to be able to show.
    # Bounded metadata and a content commitment only -- never the chunk text.
    # payload: provider, host, model, chunks:int, bytes:int, content_sha256.
    KNOWLEDGE_EGRESS = "knowledge_egress"
    CONSENT_PROMPT  = "consent_prompt"
    CONSENT_RESULT  = "consent_result"
    SECRET_REDACTED = "secret_redacted"
    ERASE           = "erase"
    # Data-retention enforcement deleted aged audit day-files / world rows. One
    # signed row per non-dry-run purge so a gap in the day-files (or shrunken
    # episode/event tables) is provably policy-driven retention, not tampering.
    # payload: audit_files_removed:int, audit_files:list[str], audit_cutoff_day,
    # episodes_deleted:int, goal_events_deleted:int, usage_buckets_removed:int.
    RETENTION_PURGE = "retention_purge"
    HALT            = "halt"
    CONFIG_REMEDIATED = "config_remediated"
    # A tenant's billing terms changed (plan or daily spend cap). One signed row
    # per change so a plan upgrade / cap change is a provable control-plane act,
    # not a silent edit. payload: tenant, field ("plan"|"quota"), old, new.
    TENANT_PLAN_CHANGED = "tenant_plan_changed"
    TENANT_QUOTA_CHANGED = "tenant_quota_changed"
    # A user's dashboard access changed: privilege role (global or per-tenant),
    # department (suite) grant, or one atomic SCIM group-authority mutation.
    # One row per change makes who-granted-whom-what-when provable instead of a
    # silent JSON edit. SCIM batch rows carry a stable scim_group_event_id,
    # old/new names, and complete member-set deltas or bounded commitments.
    ACCESS_GRANT_CHANGED = "access_grant_changed"
    # A qualified matter attorney approved or rejected one exact deliverable.
    # Queued transactionally with the DB signoff and release-blocking until
    # delivered. Payload: event_id, matter_id, decision, decided_by,
    # deliverable_updated_at, deliverable_sha256 (never note/result text).
    LEGAL_SIGNOFF_DECISION = "legal_signoff_decision"
    # Actual release of approved client work, distinct from the review itself.
    # Payload is metadata-only: actor, matter/goal ids, action/destination
    # class, exact version/digest, and optional opaque share id/expiry.
    LEGAL_RELEASE = "legal_release"
    # Conflict-oracle reads and client/matter/party mutations.  This is an
    # audit-before-read/write intent, so a rejected conflict check is still
    # visible. Payload is privacy-minimized: actor, operation, candidate count,
    # and an already-known opaque matter/client id only; never a person's or
    # entity's name.
    MATTER_INTAKE = "matter_intake"
    # A run forked from another at a specific point in its event trail, so the
    # Operating Record can hold counterfactuals side by side.
    SESSION_FORKED = "session_forked"
    # Learning governance: one row per dream cycle (what the learning system
    # wrote/retired/quarantined) so `maverick audit verify` covers learned
    # state the same way it covers tool calls.
    LEARNING_UPDATE = "learning_update"
    # Deployment-global operator control over learning gates. Kept distinct
    # from learned-state writes so an auditor can identify who armed DGM.
    # payload: control, enabled, actor, acknowledged, previous_requested,
    # effective, state, blockers.
    LEARNING_CONTROL_CHANGED = "learning_control_changed"
    # Ekko work-discovery governance.  These events deliberately carry only
    # policy hashes, opaque ids, counts and decisions -- never window titles,
    # file names, URLs, application content, or other observed work data.
    # Background work observation is a separate authority from ordinary
    # self-learning/DGM and therefore has its own auditable lifecycle.
    # Provable learning: one signed row per structured verification reward
    # (maverick.reasoning_reward), so the EVIDENCE the system learns from -- the
    # per-dimension rubric, the holistic score, and whether a facet vetoed -- is
    # tamper-evident in the chain, not just a mutable in-memory verdict. Default-on
    # ([reasoning_reward] audit_rewards); payload is to_audit_summary(): score,
    # confidence, accepts, vetoed, and per-dimension name/score/veto.
    VERIFICATION_REWARD = "verification_reward"
    # Memory governance (OWASP ASI06): a memory write was stamped/screened, or a
    # trust-aware retrieval pass filtered low-trust memory out of the brief.
    # payload: action:str (write|write_blocked|recall_filter), plus key/source/
    # trust/sensitivity/reason/markers (writes) or kept/dropped/min_trust (recall).
    MEMORY_GUARD = "memory_guard"
    # Matter intake recorded a bounded evidence collection in local knowledge.
    # Payload carries the matter id, a collection-key digest, and document/chunk
    # counts; it never includes client text or a source filesystem path.
    EVIDENCE_CAPTURE = "evidence_capture"
    # Human-oversight decision on a parked approval (approve/deny). Anchors the
    # who-decided-what in the signed chain so the integrity of the decision does
    # not rest on the mutable world-model `approvals` row alone (audit H28).
    # payload: approval_event_id:str (stable transactional-outbox identity),
    # approval_id:int, status:str (approved|denied), decided_by:str,
    # occurred_at:float (when the vote and outbox row committed).
    APPROVAL_DECISION = "approval_decision"
    # AI system lifecycle: an AI system/component was retired (decommissioned).
    # Closes the ISO/IEC 42001 A.6.2 lifecycle at the far end — the signed chain
    # records the deliberate end-of-life the same way it records learning
    # promotions, so a system that stops appearing is provably retired, not
    # silently dropped. payload: system_id:str, reason:str, decided_by:str,
    # data_disposition:str (retain|archive|erase), archived:bool, erased:bool,
    # disposal_detail:dict (counts of erased world facts / audit events).
    AI_SYSTEM_RETIRED = "ai_system_retired"
    # Continuous group-fairness monitoring (ISO/IEC 42001 A.6.2.6): the rolling
    # window breached the four-fifths rule or drifted below its baseline, so a
    # fairness regression is anchored in the signed chain instead of passing
    # silently. payload: reason:str (adverse_impact|drift|adverse_impact+drift),
    # min_impact_ratio:float, demographic_parity_diff:float,
    # equal_opportunity_diff:float|None, failing_groups:list[str], samples:int,
    # threshold:float.
    FAIRNESS_ALERT = "fairness_alert"
    # Governance assessment sign-off: a reviewer accepted (or sent back) one lens
    # of a privacy/security/AI-risk assessment. Anchoring the decision in the
    # signed chain means a "reviewed" governance record can't rest on the mutable
    # JSON register alone. payload: subject_kind:str, subject:str, lens:str,
    # decision:str (accepted|needs_work), reviewer:str, result_status:str.
    ASSESSMENT_REVIEW = "assessment_review"
    # A privacy record mutation. The originating record retains a durable
    # audit-outbox receipt until this event is accepted by the signed chain.
    PRIVACY_RECORD_CHANGED = "privacy_record_changed"
    THREAT_HUNT_RECORD_CHANGED = "threat_hunt_record_changed"


@dataclass
class AuditEvent:
    """One audit log row. ``payload`` is event-specific (see module doc)."""
    ts: float
    kind: str
    agent: str = "system"
    goal_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        # Strip reserved keys from payload before the spread: a payload key
        # named v/ts/kind/agent/goal_id would otherwise clobber the canonical
        # structural field, losing it and corrupting the signed-hash input.
        _reserved = {"v", "ts", "kind", "agent", "goal_id"}
        safe_payload = {k: val for k, val in self.payload.items() if k not in _reserved}
        return {
            "v": self.schema_version,
            "ts": self.ts,
            "kind": self.kind,
            "agent": self.agent,
            "goal_id": self.goal_id,
            **safe_payload,
        }
