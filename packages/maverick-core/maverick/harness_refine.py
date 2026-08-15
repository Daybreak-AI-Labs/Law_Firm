"""Governed harness self-refinement: an agent PROPOSES a change to its own
operating instructions from an observed failure; a human APPLIES it.

The capability itself is unremarkable — read a failure, decide what guidance
would have avoided it, write that guidance down. What makes it sellable is
that none of the writing happens on the agent's own authority:

* **A proposal is never self-applied.** ``propose`` parks a real world
  approval (dual-control quorum at "high" risk) bound to exactly this
  target + name, and ``apply`` refuses until a decision-maker approves it.
  The approval is one-shot: one decision authorizes one refinement.
* **Applying is snapshotted and reversible.** The refinement overlay is
  snapshotted through the existing learned-state machinery
  (:func:`maverick.dreaming.snapshot_learning_state`) BEFORE the change
  lands, so :func:`revert` restores real prior bytes rather than replaying
  an inverse. An apply that fails mid-way restores the snapshot instead of
  leaving the harness half-written.
* **It lands in the signed learning audit.** Every apply emits
  ``HARNESS_REFINEMENT_APPLIED`` *and* ``LEARNING_UPDATE``, so learned-state
  verification covers a self-refinement exactly like a dream cycle.
* **The observation is untrusted input.** The failure text can come from a
  run an attacker influenced, so every free-text field is screened for
  injection tripwires (a hit REFUSES — it is not quarantined and retried)
  and secret-redacted before it is ever stored or audited.

Everything is OFF by default (``[harness_refine] enable``), ``require_approval``
defaults ON, a malformed knob fails closed, and every refusal path leaves the
harness untouched.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("maverick.harness_refine")

#: What one refinement may touch. ``prompt`` is standing operating guidance,
#: ``skill`` a named procedure, ``memory`` a durable belief — the three things
#: Prime-Agent-style ``/refine`` rewrites, kept as an explicit closed set so a
#: proposal can never widen its own blast radius by naming a new target.
TARGETS = ("prompt", "skill", "memory")

#: Approval provenance. Trusted caller-supplied metadata (never inferred from
#: ``detail``, which carries model-authored text) — the operator UI and the
#: one-shot consumption check both key off it.
PROVENANCE = "harness_refine"

_MAX_TEXT = 2000
_MAX_NAME = 96
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: Retained snapshots. A revert whose snapshot has aged out refuses rather
#: than restoring the wrong generation; keep enough that the whole pending
#: queue can be applied and unwound.
_KEEP_SNAPSHOTS = 50
#: Remembered spent approval ids (newest kept). Beyond this the highest
#: EVICTED id becomes a floor, so forgetting can only ever refuse — never
#: let an aged-out approval authorize a second refinement.
_MAX_CONSUMED_APPROVALS = 200
_STORE_VERSION = 1


class RefineError(Exception):
    """Operator-facing refusal (disabled, bad observation, missing approval,
    tampered proposal, unreversible apply)."""


# ---- settings -------------------------------------------------------------

def _settings() -> dict:
    """Resolved ``[harness_refine]`` settings, fail-closed on any trouble.

    ``config.get_harness_refine`` already fails a malformed ``require_approval``
    to True (a typo must never disarm the gate); this adds the outer belt — if
    the section cannot be read at all we report "off, gated", so a broken
    config file refuses the capability instead of running it ungoverned."""
    try:
        from .config import get_harness_refine
        cfg = get_harness_refine()
        if not isinstance(cfg, dict):
            raise ValueError("harness_refine settings are not a mapping")
        return {
            "enable": bool(cfg.get("enable", False)),
            "require_approval": bool(cfg.get("require_approval", True)),
            "max_pending": max(1, int(cfg.get("max_pending", 20) or 20)),
        }
    except Exception:
        log.warning("harness_refine: settings unreadable; refusing "
                    "(fail closed)", exc_info=True)
        return {"enable": False, "require_approval": True, "max_pending": 20}


def enabled() -> bool:
    """Whether governed self-refinement is admitted. OFF by default — an
    agent editing its own operating instructions is an explicit operator
    decision. Also honored via ``MAVERICK_HARNESS_REFINE=1``."""
    return bool(_settings()["enable"])


# ---- stores ---------------------------------------------------------------

def store_path() -> Path:
    """The proposal ledger (queue + spent-approval record), tenant-scoped."""
    from .paths import data_dir
    return data_dir("harness-refine") / "proposals.json"


def refinements_path() -> Path:
    """The applied overlay — the harness artifact a refinement actually
    changes, and the only thing a snapshot/revert covers."""
    from .paths import data_dir
    return data_dir("harness-refine") / "refinements.json"


def snapshots_dir() -> Path:
    """Snapshot base for the overlay.

    Deliberately NOT the shared ``dreams/snapshots`` base: a snapshot there
    carrying only our store would become ``latest`` for the dream loop, and a
    dream rollback would then delete every learned store absent from it."""
    from .paths import data_dir
    return data_dir("harness-refine") / "snapshots"


def _empty_store() -> dict:
    return {"version": _STORE_VERSION, "proposals": {},
            "consumed_approvals": [], "approval_floor": 0}


def _load_store() -> dict:
    """Read the proposal ledger, REFUSING on corruption.

    Unlike ordinary metadata sidecars this one may not degrade to "empty":
    the spent-approval record lives here, so treating a damaged file as a
    blank queue would let an already-consumed approval authorize a second
    refinement. Fail closed instead."""
    path = store_path()
    try:
        if not path.exists():
            return _empty_store()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("store root must be an object")
    except (OSError, UnicodeError, ValueError) as e:
        raise RefineError(
            f"the refinement ledger is unreadable ({e}); refusing rather "
            "than starting from a blank queue") from e
    store = _empty_store()
    proposals = data.get("proposals")
    if isinstance(proposals, dict):
        store["proposals"] = {str(k): dict(v) for k, v in proposals.items()
                              if isinstance(v, dict)}
    consumed = data.get("consumed_approvals")
    if isinstance(consumed, list):
        store["consumed_approvals"] = list(consumed)
    try:
        store["approval_floor"] = int(data.get("approval_floor") or 0)
    except (TypeError, ValueError):
        store["approval_floor"] = 0
    return store


def _save_store(store: dict) -> None:
    from .file_lock import atomic_write_text, ensure_private_directory
    path = store_path()
    ensure_private_directory(path.parent)
    atomic_write_text(path, json.dumps(store, indent=2, sort_keys=True),
                      mode=0o600)


def _locked():
    from .file_lock import cross_process_lock
    return cross_process_lock(store_path())


def _load_refinements() -> dict[str, dict]:
    path = refinements_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("overlay root must be an object")
        return {str(k): dict(v) for k, v in data.items()
                if isinstance(v, dict)}
    except (OSError, UnicodeError, ValueError) as e:
        raise RefineError(
            f"the refinement overlay is unreadable ({e}); refusing") from e


def _write_refinements(entries: dict[str, dict]) -> None:
    from .file_lock import atomic_write_text, ensure_private_directory
    path = refinements_path()
    ensure_private_directory(path.parent)
    atomic_write_text(path, json.dumps(entries, indent=2, sort_keys=True),
                      mode=0o600)


def refinements(*, target: str | None = None) -> list[dict]:
    """The refinements currently in force, newest first.

    This is the read seam the harness builder consumes: an applied refinement
    is only worth governing if something actually reads it."""
    want = str(target or "").strip().lower()
    rows = [dict(v) for v in _load_refinements().values()
            if not want or str(v.get("target") or "") == want]
    return sorted(rows, key=lambda r: float(r.get("applied_at") or 0.0),
                  reverse=True)


# ---- screening + identity -------------------------------------------------

def _screen(field: str, value: object) -> str:
    """Bound, tripwire-screen and secret-redact one free-text field.

    An injection hit REFUSES the whole proposal. Quarantining it instead would
    keep attacker-authored text in a queue whose only exit is "write this into
    the agent's own instructions" — the exact laundering path the screen
    exists to close."""
    text = str(value or "").strip()
    if not text:
        raise RefineError(f"observation field {field!r} is required")
    if len(text) > _MAX_TEXT:
        raise RefineError(
            f"observation field {field!r} is {len(text)} characters; the "
            f"ceiling is {_MAX_TEXT}")
    from .memory_guard import injection_markers
    hits = injection_markers(text)
    if hits:
        raise RefineError(
            f"observation field {field!r} tripped the injection screen "
            f"({', '.join(sorted(hits))}); a refinement mined from an "
            "untrusted run is refused, not stored")
    from .safety.secret_detector import redact
    redacted, matches = redact(text)
    if matches:
        log.warning("harness_refine: redacted %d secret(s) from %s",
                    len(matches), field)
    return redacted


def _digest(target: str, name: str, change: str) -> str:
    """Content commitment over the normalized change.

    Whitespace-insensitive on purpose (a reflow is not an edit), but bound to
    the target and name too, so an approval for one artifact can never cover
    the same words aimed at another."""
    canonical = json.dumps(
        {"target": target, "name": name, "change": " ".join(change.split())},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _action(target: str, name: str) -> str:
    """The approval action string a refinement approval is bound to — the
    exact-match check is what stops an approval applying somewhere else."""
    return f"harness-refine:{target}:{name}"


def _key(target: str, name: str) -> str:
    return f"{target}:{name}"


# ---- propose --------------------------------------------------------------

def _park_approval(target: str, name: str, digest: str, summary: str,
                   proposed_by: str) -> int:
    """Park the human decision this refinement cannot make for itself."""
    from .safety.dual_control import required_approvals
    from .world_model import open_world
    return open_world().create_approval(
        _action(target, name), risk="high",
        detail=f"Apply a self-proposed change to the agent's own {target} "
               f"{name!r}. Digest {digest[:16]}. Rationale: {summary}",
        provenance=PROVENANCE,
        approvals_required=required_approvals("high"),
        requested_by=proposed_by or "")


def _discard(proposal_id: str) -> None:
    """Best-effort removal of a proposal whose audit row never landed."""
    try:
        with _locked():
            store = _load_store()
            if store["proposals"].pop(proposal_id, None) is not None:
                _save_store(store)
    except Exception:  # pragma: no cover -- cleanup never masks the refusal
        log.warning("harness_refine: could not discard unaudited proposal %s",
                    proposal_id, exc_info=True)


def propose(observation: dict, *, goal_id: int | None = None,
            proposed_by: str = "") -> dict:
    """Record one proposed refinement and park its approval.

    ``observation`` is ``{failure, target, name, change, rationale}``. Nothing
    is applied here and nothing is stored unless every field survives
    screening; a full pending queue refuses rather than evicting evidence."""
    cfg = _settings()
    if not cfg["enable"]:
        raise RefineError(
            "harness self-refinement is off; set [harness_refine] enable = "
            "true (or MAVERICK_HARNESS_REFINE=1) to admit it")
    if not isinstance(observation, dict):
        raise RefineError("observation must be a mapping")
    target = str(observation.get("target") or "").strip().lower()
    if target not in TARGETS:
        raise RefineError(
            f"unknown refinement target {target!r}; expected one of "
            f"{', '.join(TARGETS)}")
    name = str(observation.get("name") or "").strip()
    if not name or len(name) > _MAX_NAME or not _NAME_RE.fullmatch(name):
        raise RefineError(
            f"refinement name {name!r} must be 1-{_MAX_NAME} characters of "
            "letters, digits, dot, dash or underscore")
    failure = _screen("failure", observation.get("failure"))
    change = _screen("change", observation.get("change"))
    rationale = _screen("rationale", observation.get("rationale"))

    digest = _digest(target, name, change)
    proposal_id = uuid.uuid4().hex[:16]
    record: dict[str, Any] = {
        "id": proposal_id, "status": "pending", "target": target,
        "name": name, "failure": failure, "change": change,
        "rationale": rationale, "digest": digest, "action": _action(target, name),
        "goal_id": int(goal_id) if goal_id is not None else None,
        "proposed_by": str(proposed_by or "")[:200],
        "proposed_at": time.time(), "approval_id": None,
    }
    with _locked():
        store = _load_store()
        pending = [p for p in store["proposals"].values()
                   if p.get("status") == "pending"]
        if len(pending) >= cfg["max_pending"]:
            raise RefineError(
                f"the pending refinement queue is full "
                f"({len(pending)}/{cfg['max_pending']}); decide the parked "
                "proposals first — a full queue refuses rather than dropping "
                "one an approver has not seen yet")
        if cfg["require_approval"]:
            record["approval_id"] = _park_approval(
                target, name, digest, rationale[:300], record["proposed_by"])
        store["proposals"][proposal_id] = record
        _save_store(store)

    from .audit import EventKind, audit_event
    try:
        audit_event(EventKind.HARNESS_REFINEMENT_PROPOSED,
                    agent="harness_refine", goal_id=record["goal_id"],
                    proposal=proposal_id, target=target, name=name,
                    digest=digest, approval_id=record["approval_id"],
                    proposed_by=record["proposed_by"], failure=failure[:200])
    except Exception:
        # An unrecordable proposal must not sit in the queue looking
        # applicable: the Operating Record is the reason this is sellable.
        _discard(proposal_id)
        raise
    return dict(record)


def list_proposals(*, status: str | None = None) -> list[dict]:
    """Proposals newest first, optionally filtered to one status
    (``pending`` / ``applied`` / ``reverted``)."""
    want = str(status or "").strip().lower()
    rows = [dict(p) for p in _load_store()["proposals"].values()
            if not want or str(p.get("status") or "") == want]
    return sorted(rows, key=lambda r: float(r.get("proposed_at") or 0.0),
                  reverse=True)


# ---- apply ----------------------------------------------------------------

def _check_evaluator_anchors() -> None:
    """Refuse while the evaluator ground truth is not what was released.

    A refinement is a learned-state write, and the anchors are what any later
    "it got better" claim is judged against. Applying one while an anchor is
    edited, missing, or unlocked would launder drift, so the runtime apply
    honours the same integrity ratchet the CI gate enforces."""
    try:
        from . import evaluator_evolution
        problems = evaluator_evolution.validate()
    except Exception as e:
        raise RefineError(
            f"the evaluator anchor guard could not run ({e}); refusing "
            "(fail closed)") from e
    if problems:
        raise RefineError(
            "evaluator anchors fail their integrity lock ("
            + "; ".join(problems[:3])
            + "); refusing to apply a refinement against unverified "
              "ground truth")


def _consume_approval(store: dict, record: dict, approval_id: object) -> int:
    """Validate + spend one refinement approval; returns its id or raises.

    The approval must exist under our provenance, match this proposal's action
    string exactly, and be approved. The spend is recorded BEFORE the effect,
    so a failure afterwards burns the approval rather than ever letting it
    cover two refinements."""
    try:
        aid = int(approval_id)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise RefineError(
            "applying a refinement needs the id of an approved approval; "
            f"{approval_id!r} is not one") from e
    from .world_model import open_world
    approval = open_world().get_approval(aid)
    if approval is None or approval.provenance != PROVENANCE:
        raise RefineError(f"unknown refinement approval {aid!r}")
    action = _action(str(record.get("target") or ""), str(record.get("name") or ""))
    if approval.action != action:
        raise RefineError(
            f"approval #{aid} was granted for {approval.action!r}, not "
            f"{action!r} — a refinement approval is bound to exactly one "
            "target and name")
    if approval.status != "approved":
        raise RefineError(
            f"approval #{aid} is {approval.status}; applying a refinement "
            "needs an approved decision")
    spent = (f"approval #{aid} has already applied a refinement; approvals "
             "are one-shot — propose again to park a new one")
    consumed = list(store.get("consumed_approvals") or [])
    # String-compare the ledger so a junk entry can never crash the check.
    if any(str(u) == str(aid) for u in consumed):
        raise RefineError(spent)
    floor = int(store.get("approval_floor") or 0)
    if aid <= floor:
        raise RefineError(spent)
    consumed.append(aid)
    keep = consumed[-_MAX_CONSUMED_APPROVALS:]
    for stale in consumed[:len(consumed) - len(keep)]:
        try:
            floor = max(floor, int(stale))
        except (TypeError, ValueError):  # junk entry; nothing to raise to
            continue
    store["consumed_approvals"] = keep
    store["approval_floor"] = floor
    return aid


def _snapshot_stores() -> dict[str, Path]:
    return {"refinements.json": refinements_path()}


def _take_snapshot() -> str:
    """Snapshot the overlay through the existing learned-state machinery.

    ``publish_empty`` records the "nothing was in force yet" boundary
    explicitly, so the first refinement is as revertible as the hundredth."""
    from . import dreaming
    from .file_lock import ensure_private_directory
    base = snapshots_dir()
    try:
        ensure_private_directory(base)
        snap = dreaming.snapshot_learning_state(
            keep_last=_KEEP_SNAPSHOTS, directory=base,
            stores=_snapshot_stores(), publish_empty=True,
            raise_on_error=True)
    except Exception as e:
        raise RefineError(
            f"could not snapshot the harness ({e}); refusing to apply a "
            "change we could not reverse") from e
    if snap is None:  # pragma: no cover -- publish_empty always publishes
        raise RefineError(
            "the harness snapshot produced no receipt; refusing to apply a "
            "change we could not reverse")
    return snap.name


def _restore_snapshot(name: str) -> None:
    from . import dreaming
    dreaming.rollback_learning_state(
        name, directory=snapshots_dir(), stores=_snapshot_stores())


def _apply_change(entry: dict) -> None:
    """Write one accepted refinement into the live overlay."""
    from .file_lock import cross_process_lock
    with cross_process_lock(refinements_path()):
        overlay = _load_refinements()
        overlay[_key(entry["target"], entry["name"])] = entry
        _write_refinements(overlay)


def apply(proposal_id: str, *, applied_by: str = "",
          approval_id: int | None = None) -> dict:
    """Apply one approved proposal, snapshotted and audited.

    Refuses unless the proposal is pending, its stored change still matches
    its digest, the evaluator anchors verify, and — when ``require_approval``
    is on — an approved, unspent approval bound to this proposal's action
    exists. The snapshot is taken first, so a failure part-way through
    restores the harness rather than leaving it half-written."""
    cfg = _settings()
    if not cfg["enable"]:
        raise RefineError(
            "harness self-refinement is off; nothing can be applied")
    from .learning_guard import check_learning_halt
    check_learning_halt("harness_refine", "apply")
    _check_evaluator_anchors()

    with _locked():
        store = _load_store()
        record = store["proposals"].get(str(proposal_id))
        if record is None:
            raise RefineError(f"unknown refinement proposal {proposal_id!r}")
        if record.get("status") != "pending":
            raise RefineError(
                f"proposal {proposal_id!r} is {record.get('status')}; only a "
                "pending proposal can be applied")
        expected = _digest(str(record.get("target") or ""),
                           str(record.get("name") or ""),
                           str(record.get("change") or ""))
        if expected != record.get("digest"):
            raise RefineError(
                f"proposal {proposal_id!r} no longer matches the digest it "
                "was approved under; the stored change was edited underneath "
                "— refusing")
        if cfg["require_approval"]:
            wanted = (approval_id if approval_id is not None
                      else record.get("approval_id"))
            record["approval_id"] = _consume_approval(store, record, wanted)
            # Burn the approval before the effect, not after.
            _save_store(store)
        snapshot = _take_snapshot()
        entry = {
            "target": record["target"], "name": record["name"],
            "change": record["change"], "rationale": record["rationale"],
            "failure": record["failure"], "digest": record["digest"],
            "proposal_id": record["id"], "applied_at": time.time(),
            "applied_by": str(applied_by or "")[:200],
        }
        try:
            _apply_change(entry)
        except Exception as e:
            try:
                _restore_snapshot(snapshot)
            except Exception as restore_error:
                raise RefineError(
                    f"applying refinement {proposal_id!r} failed ({e}) AND "
                    f"the restore from snapshot {snapshot} failed "
                    f"({restore_error}); the harness may be half-written — "
                    "restore it by hand before running again") from e
            raise RefineError(
                f"applying refinement {proposal_id!r} failed ({e}); the "
                f"harness was restored from snapshot {snapshot}") from e
        record.update(status="applied", applied_at=entry["applied_at"],
                      applied_by=entry["applied_by"], snapshot=snapshot)
        _save_store(store)

    from .audit import EventKind, audit_event
    audit_event(EventKind.HARNESS_REFINEMENT_APPLIED, agent="harness_refine",
                goal_id=record.get("goal_id"), proposal=record["id"],
                target=record["target"], name=record["name"],
                digest=record["digest"], snapshot=snapshot,
                approval_id=record.get("approval_id"),
                applied_by=record["applied_by"])
    # Learned-state verification covers a self-refinement like any other
    # learned write — the same row shape the dream loop emits.
    audit_event(EventKind.LEARNING_UPDATE, agent="harness_refine",
                content="harness_refinement_applied", proposal=record["id"],
                target=record["target"], name=record["name"],
                digest=record["digest"], snapshot=snapshot)
    return dict(record)


# ---- revert ---------------------------------------------------------------

def revert(proposal_id: str, *, reverted_by: str = "") -> bool:
    """Restore the snapshot this proposal was applied over.

    Returns False when there is nothing applied to undo, so reverting twice is
    a no-op. Deliberately NOT gated on ``enable``: turning the capability off
    must never strand an applied refinement (same reasoning as
    ``learning_guard`` leaving recovery paths outside the halt boundary).

    Restoration is wholesale — the overlay returns to exactly its bytes at
    apply time, so reverting an older refinement also drops the ones applied
    after it. That is the point of a snapshot rather than an inverse patch."""
    from . import dreaming
    with _locked():
        store = _load_store()
        record = store["proposals"].get(str(proposal_id))
        if record is None or record.get("status") != "applied":
            return False
        snapshot = str(record.get("snapshot") or "")
        if snapshot not in dreaming.list_snapshots(snapshots_dir()):
            raise RefineError(
                f"snapshot {snapshot!r} for proposal {proposal_id!r} is no "
                "longer retained; the harness cannot be restored "
                "automatically")
        try:
            _restore_snapshot(snapshot)
        except Exception as e:
            raise RefineError(
                f"restoring snapshot {snapshot!r} failed ({e}); the "
                "refinement stays marked applied") from e
        record.update(status="reverted", reverted_at=time.time(),
                      reverted_by=str(reverted_by or "")[:200])
        _save_store(store)

    from .audit import EventKind, audit_event
    audit_event(EventKind.LEARNING_UPDATE, agent="harness_refine",
                content="harness_refinement_reverted", proposal=record["id"],
                target=record["target"], name=record["name"],
                snapshot=snapshot, reverted_by=record["reverted_by"])
    return True


__all__ = [
    "PROVENANCE", "TARGETS", "RefineError", "apply", "enabled",
    "list_proposals", "propose", "refinements", "refinements_path", "revert",
    "snapshots_dir", "store_path",
]
