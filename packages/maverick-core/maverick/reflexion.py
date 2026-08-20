"""Reflexion library: per-failure self-critique persistence.

When an agent run fails, we want the NEXT similar run to remember
what went wrong and avoid the same mistake. This module is the
storage + retrieval layer for that loop.

Storage: ``~/.maverick/reflexions.ndjson`` (chmod 600), one JSON
object per line. Each entry records:
  - ts            — when the failure happened
  - goal_text     — title + description of the goal
  - failure_class — classified via maverick.retry.classifier
  - failure_msg   — the exception's short message
  - reflection    — the agent's own one-paragraph postmortem
  - tools_used    — list of tools the agent ran before failing

Retrieval: ``recall(goal_text, k=3)`` returns the top-K most similar
prior reflections. Used by the orchestrator's default-on pre-run context layer
(disable via [reflexion] enable = false).

Similarity scoring uses the explicitly configured, digest-pinned on-box
knowledge model when an exact live MatterContext is bound; otherwise it uses
token-jaccard. There is no FastEmbed download or persisted vector cache.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .file_lock import (
    cross_process_lock,
    ensure_private_directory,
    ensure_private_file,
    open_private_append,
)
from .paths import data_dir

log = logging.getLogger(__name__)


DEFAULT_PATH = data_dir("reflexions.ndjson", tenant=None)

def default_path() -> Path:
    """The active scope's reflexion log (tenant-isolated when one is active)."""
    return _tenant_path("reflexions.ndjson", DEFAULT_PATH)


def _tenant_path(name: str, legacy):
    """Item-30 isolation: with an ACTIVE tenant, this store lives under the
    tenant's data dir (one tenant's learned memory can never feed another's
    runs); single-tenant resolution keeps the legacy location unchanged."""
    from .paths import current_tenant_id, data_dir

    tenant = current_tenant_id()
    if tenant:
        return data_dir(*name.split("/"), tenant=tenant)
    return legacy


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_lock = threading.Lock()
_active_matter: ContextVar[int | None] = ContextVar(
    "maverick_reflexion_matter", default=None,
)
_active_owner: ContextVar[str | None] = ContextVar(
    "maverick_reflexion_owner", default=None,
)


def _exact_matter_id(value: Any) -> int | None:
    """Normalize the physical matter key; ambiguous keys fail closed."""
    if value is None or isinstance(value, bool):
        return None
    try:
        matter_id = int(value)
    except (TypeError, ValueError):
        return None
    return matter_id if matter_id > 0 else None


@contextmanager
def matter_scope(matter_id: int | None, owner: str | None = None):
    """Propagate one goal's matter through agent/tool learning callbacks.

    A few low-level callbacks (notably human-override and flaky-tool learning)
    do not receive the ``Goal`` object. Context-local propagation keeps those
    records useful without turning a missing key into a global memory bucket.
    """
    token = _active_matter.set(_exact_matter_id(matter_id))
    owner_token = _active_owner.set(str(owner) if owner is not None else None)
    try:
        yield
    finally:
        _active_owner.reset(owner_token)
        _active_matter.reset(token)


def current_matter_id() -> int | None:
    """The exact matter propagated by the running orchestrator, if any."""
    return _active_matter.get()


def current_owner() -> str | None:
    """The exact owner propagated by the running orchestrator, if any."""
    return _active_owner.get()


@dataclass
class Reflexion:
    ts: float
    goal_text: str
    failure_class: str
    failure_msg: str
    reflection: str
    tools_used: list[str] = field(default_factory=list)
    channel: str | None = None
    user_id: str | None = None
    # Department attribution: the domain pack a run executed as (None for a
    # generic orchestrator run). Lets recall boost same-department lessons and
    # the dreaming loop consolidate per department. Older log lines without
    # the key load as None — fully backward compatible.
    domain: str | None = None
    # The model that produced this failure. The self-harness loop mines
    # weaknesses PER MODEL (a harness edit for one model must not leak into
    # another's), so it needs the model tagged on the trace. Older lines without
    # the key load as None — fully backward compatible.
    model_id: str | None = None
    # The agent ROLE in play when the failure landed (orchestrator, coder,
    # verifier, ...). Lets the self-harness loop mine weaknesses per role and
    # scope the learned guidance so an orchestrator lesson doesn't tax worker
    # prompts of the same model. Older lines load as None — backward compatible.
    role: str | None = None
    # Exact client-matter boundary. ``Goal.project_id`` is the current physical
    # matter key; a missing key is retained for audit/legacy visibility but is
    # never eligible for cross-run recall or offline consolidation.
    matter_id: int | None = None
    # Principal provenance for offline candidate generation. Runtime recall is
    # shared within the matter, but DGM corpus reads additionally require this
    # exact owner so an operator cannot pool two principals accidentally.
    owner: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def record(
    goal_text: str,
    failure_class: str,
    failure_msg: str,
    reflection: str,
    *,
    tools_used: list[str] | None = None,
    channel: str | None = None,
    user_id: str | None = None,
    domain: str | None = None,
    model_id: str | None = None,
    role: str | None = None,
    matter_id: int | None = None,
    owner: str | None = None,
    path: Path | None = None,
) -> bool:
    """Append a Reflexion. Returns True on success.

    Fail-safe: write errors are logged and swallowed — a failed
    reflection write should never block a subsequent agent run.
    """
    from .learning_guard import learning_write_allowed
    if not learning_write_allowed("reflexion"):
        return False
    default_store = path is None
    path = path if path is not None else default_path()
    # This is the durable privacy boundary. Callers are numerous and some pass
    # raw provider/exception text, so every free-text field is independently
    # redacted and bounded here. A redactor failure returns an empty string;
    # persistence never falls back to the original secret-bearing value.
    def safe(value: Any, limit: int) -> str:
        try:
            return _sanitize_text(str(value or ""))[:limit]
        except Exception:
            return ""

    safe_class = re.sub(r"[^A-Za-z0-9_.:-]+", "_", safe(failure_class, 96)).strip("_")
    safe_tools = [safe(tool, 128) for tool in list(tools_used or [])[:32]]
    safe_tools = [tool for tool in safe_tools if tool]

    def optional(value: Any, limit: int) -> str | None:
        if value is None:
            return None
        cleaned = safe(value, limit)
        return cleaned or None

    entry = Reflexion(
        ts=time.time(),
        goal_text=safe(goal_text, 2_000),
        failure_class=safe_class or "unknown",
        failure_msg=safe(failure_msg, 1_000),
        reflection=safe(reflection, 4_000),
        tools_used=safe_tools,
        channel=optional(channel, 128),
        user_id=optional(user_id, 256),
        domain=optional(domain, 128),
        model_id=optional(model_id, 256),
        role=optional(role, 128),
        matter_id=_exact_matter_id(
            matter_id if matter_id is not None else current_matter_id(),
        ),
        # An empty owner is still an exact local principal in the existing
        # WorldModel schema. Preserve it rather than collapsing it into the
        # legacy/missing-owner sentinel used by the DGM read boundary.
        owner=safe(owner if owner is not None else current_owner(), 256)
        if (owner is not None or current_owner() is not None) else None,
    )
    with _lock:
        try:
            if default_store:
                # The tenant/default data directory is platform-owned.  An
                # explicit caller path keeps its existing parent ACL.
                ensure_private_directory(path.parent)
            with cross_process_lock(path, strict=True):
                from .learning_crypto import (
                    decode_json_record,
                    encode_json_record,
                    protected_learning_enabled,
                )

                if path.exists() and protected_learning_enabled():
                    ensure_private_file(path)
                    with open(path, encoding="utf-8") as existing:
                        if any(
                            raw.strip() and decode_json_record(raw) is None
                            for raw in existing
                        ):
                            raise RuntimeError(
                                "reflexion store authentication failed"
                            )
                fd = open_private_append(
                    path, require_private_parent=default_store,
                )
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(encode_json_record(entry.to_dict()) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            return True
        except (OSError, RuntimeError) as e:
            log.warning("reflexion: write failed: %s", e)
            return False


def _tokens(s: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(s or "")}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _embed_sims(query: str, entries: list[Reflexion]) -> list[float] | None:
    """Cosine similarity of ``query`` to each entry's goal_text.

    Returns a list aligned with ``entries`` when the configured pinned local
    knowledge model is available, else ``None`` so recall falls back to
    jaccard. The compatibility bridge keeps no client vectors or text and has
    no remote/download provider.
    """
    try:
        from .skill.embeddings import _cosine, _have_fastembed, embed
        if not _have_fastembed():
            return None
        vectors = embed([query] + [e.goal_text or "" for e in entries])
        if not vectors or len(vectors) != len(entries) + 1:
            return None
        qv = vectors[0]
        return [_cosine(qv, vectors[i + 1]) for i in range(len(entries))]
    except Exception as e:  # pragma: no cover -- lexical recall remains useful
        log.debug("local reflexion embedding unavailable (%s); using jaccard", e)
        return None


def _scope_matches(
    entry: Reflexion, *, channel: str | None, user_id: str | None,
    matter_id: int | None = None,
) -> bool:
    """Return whether a persisted entry belongs to the requested scope.

    Reflexions can contain user-originated goal text. Keep scoped memories
    from crossing channel/user boundaries, and require an exact matter match.
    """
    # ``None`` is not a shared/global matter. It means the caller could not
    # prove a matter boundary, so no client-derived lesson may be recalled.
    return (
        matter_id is not None
        and entry.matter_id == matter_id
        and entry.channel == channel
        and entry.user_id == user_id
    )


def _reflexion_from_record(data: dict[str, Any]) -> Reflexion | None:
    try:
        values = {
            key: data.get(key)
            for key in (
                "ts",
                "goal_text",
                "failure_class",
                "failure_msg",
                "reflection",
                "tools_used",
                "channel",
                "user_id",
                "domain",
                "model_id",
                "role",
                "matter_id",
                "owner",
            )
        }
        values["matter_id"] = _exact_matter_id(values["matter_id"])
        return Reflexion(**values)
    except TypeError:
        return None


def _sanitize_text(text: str, *, shield: Any | None = None) -> str:
    """Redact secrets and drop unsafe persisted prompt snippets fail-closed."""
    try:
        safe = str(text or "")
    except Exception:
        return ""
    try:
        from .safety.secret_detector import redact as _redact
        safe, _ = _redact(safe)
    except Exception:  # pragma: no cover
        return ""
    if shield is not None:
        try:
            verdict = shield.scan_input(safe)
            if not getattr(verdict, "allowed", True):
                return "[redacted by Shield]"
        except Exception:  # pragma: no cover
            return ""
    return safe


# Only score the most recent N lines so recall cost stays bounded as the
# NDJSON log grows unbounded over a machine's lifetime.
_SCAN_CAP = 500
# How much recency tilts the blended score vs. raw similarity. Similarity
# still dominates (0.7); recency (0.3) breaks ties toward fresher lessons so
# a stale near-match can't outrank an equally-relevant fresh one.
_RECENCY_WEIGHT = 0.3
# Two lessons whose goal-text token sets overlap at/above this are treated as
# near-duplicates; only the higher-scored one survives in the top-k.
_DEDUP_THRESHOLD = 0.9
# Additive boost for a lesson recorded by the SAME department (domain pack)
# as the recalling run: a finance_sox failure is a stronger prior for the
# next finance_sox run than an equally-similar generic one. Cross-department
# lessons are still recallable — boosted, not filtered.
_DOMAIN_BOOST = 0.1


def _live_scope_allows_recall(matter_id: int, domain: str | None) -> bool:
    """Require freshly proven matter authority in the firm posture."""
    try:
        from .security_defaults import secure_by_default

        secure = bool(secure_by_default())
    except Exception:
        secure = True
    if not secure:
        return True
    try:
        from .matter_context import refresh_matter_context

        live_context = refresh_matter_context()
    except Exception:
        return False
    return live_context.matter_id == matter_id and (
        domain is None or live_context.domain == domain
    )


def recall(
    goal_text: str,
    *,
    k: int = 3,
    path: Path | None = None,
    min_score: float = 0.05,
    min_embed_score: float = 0.35,
    channel: str | None = None,
    user_id: str | None = None,
    domain: str | None = None,
    matter_id: int | None = None,
    scan_cap: int = _SCAN_CAP,
) -> list[tuple[float, Reflexion]]:
    """Return the top-k most similar prior reflections.

    Tuples are (score, Reflexion), sorted by score descending. Empty
    list if no file exists or nothing clears the similarity floor.

    Similarity is pinned-local embedding cosine when configured, else
    token-jaccard; the floor is ``min_embed_score`` or ``min_score``
    respectively (the two metrics aren't on the same scale). The returned
    score blends similarity with a recency factor so a fresher lesson
    outranks an equally-relevant stale one; only the most recent
    ``scan_cap`` lines are considered, and near-identical lessons are
    de-duplicated within the top-k.
    """
    path = path if path is not None else default_path()
    matter_id = _exact_matter_id(
        matter_id if matter_id is not None else current_matter_id(),
    )
    if matter_id is None or not goal_text or not path.exists():
        return []
    if not _live_scope_allows_recall(matter_id, domain):
        return []
    qt = _tokens(goal_text)
    entries: list[Reflexion] = []
    scan_limit = max(1, scan_cap)
    try:
        ensure_private_file(path)
        with open(path, encoding="utf-8") as f:
            lines = deque(f, maxlen=scan_limit)
    except OSError:
        return []
    from .learning_crypto import decode_json_record

    for raw in lines:
        data = decode_json_record(raw)
        if data is None:
            continue
        entry = _reflexion_from_record(data)
        if entry is None:
            continue
        if not _scope_matches(
            entry, channel=channel, user_id=user_id, matter_id=matter_id,
        ):
            continue
        entries.append(entry)

    if not entries:
        return []
    # Recency is measured against the freshest entry seen so the blend is
    # deterministic (no wall-clock dependency) and bounded to [0, 1].
    newest = max(e.ts for e in entries)
    oldest = min(e.ts for e in entries)
    span = newest - oldest

    # Prefer pinned-local embedding cosine (catches differently-worded lessons
    # jaccard misses); fall back to per-entry jaccard when it is unconfigured.
    embed_sims = _embed_sims(goal_text, entries)
    if embed_sims is not None:
        sims, floor = embed_sims, min_embed_score
    else:
        sims = [_jaccard(qt, _tokens(e.goal_text)) for e in entries]
        floor = min_score

    scored: list[tuple[float, Reflexion]] = []
    for entry, sim in zip(entries, sims, strict=False):
        if sim < floor:
            continue
        recency = 1.0 if span <= 0 else (entry.ts - oldest) / span
        blended = (1.0 - _RECENCY_WEIGHT) * sim + _RECENCY_WEIGHT * recency
        if domain and entry.domain == domain:
            blended += _DOMAIN_BOOST
        scored.append((blended, entry))

    scored.sort(key=lambda p: (p[0], p[1].ts), reverse=True)

    top: list[tuple[float, Reflexion]] = []
    for score, entry in scored:
        et = _tokens(entry.goal_text)
        if any(_jaccard(et, _tokens(k_entry.goal_text)) >= _DEDUP_THRESHOLD
               for _, k_entry in top):
            continue
        top.append((score, entry))
        if len(top) >= max(1, k):
            break
    return top


def list_recent(
    *,
    limit: int = 50,
    path: Path | None = None,
) -> list[Reflexion]:
    """Return the N most recent reflexions, ordered newest-first."""
    path = path if path is not None else default_path()
    if not path.exists():
        return []
    entries: list[Reflexion] = []
    try:
        from collections import deque
        ensure_private_file(path)
        with open(path, encoding="utf-8") as f:
            # The log is append-ordered and unbounded; only the tail can hold
            # the N most recent entries. 4x headroom absorbs malformed lines
            # and any local ts jitter without paying an O(whole-file) parse.
            tail = deque(f, maxlen=max(1, limit) * 4)
        from .learning_crypto import decode_json_record

        for raw in tail:
            data = decode_json_record(raw)
            if data is None:
                continue
            entry = _reflexion_from_record(data)
            if entry is not None:
                entries.append(entry)
    except OSError:
        return []
    # A legacy/hand-written line can lack ``ts`` (loaded as None); a None key
    # would TypeError the whole sort and silently blank every caller.
    entries.sort(key=lambda r: r.ts or 0, reverse=True)
    return entries[:max(1, limit)]


def clear(path: Path | None = None) -> bool:
    """Delete the reflexion log."""
    path = path if path is not None else default_path()
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def format_context(
    reflexions: list[tuple[float, Reflexion]], *, shield: Any | None = None
) -> str:
    """Render redacted reflexions as an orchestrator prompt addendum."""
    if not reflexions:
        return ""
    lines = [
        "",
        "## Prior failures on similar goals",
        "",
        "You've encountered these failures before. Use them to avoid "
        "repeating the same mistake:",
        "",
    ]
    for score, r in reflexions:
        goal_text = _sanitize_text(r.goal_text, shield=shield)[:120]
        failure_class = _sanitize_text(r.failure_class, shield=shield)[:80]
        lines.append(f"- ({failure_class}, score {score:.2f}) {goal_text}")
        if r.reflection:
            reflection = _sanitize_text(r.reflection, shield=shield)[:300]
            lines.append(f"  └─ lesson: {reflection}")
    lines.append("")
    return "\n".join(lines)


def enabled() -> bool:
    """Whether the cross-run reflexion learning loop is active.

    On by default. ``MAVERICK_REFLEXION=0`` or
    ``[reflexion] enable = false`` provides an explicit opt-out.
    """
    try:
        from .config import (
            config_source_errors,
            governed_learning_default,
            governed_learning_env_flag,
            load_config,
        )
        override = governed_learning_env_flag("MAVERICK_REFLEXION")
        if override is not None:
            return override
        cfg = load_config()
        if config_source_errors():
            return False
        return bool(
            cfg.get("reflexion", {}).get("enable", governed_learning_default())
        )
    except Exception:  # pragma: no cover -- config never blocks a run
        return False


def recall_enabled() -> bool:
    """Whether recorded lessons may be RE-INJECTED into new runs' context.

    Recall is on with reflexion learning by default now that :func:`recall`
    requires an exact, non-null matter key. The independent knob remains so an
    operator can stop prompt re-injection while continuing to record local
    evidence for offline review.
    """
    try:
        from .config import governed_learning_env_flag, load_config
        override = governed_learning_env_flag("MAVERICK_REFLEXION_RECALL")
        if override is not None:
            return override and enabled()
        cfg = load_config()
        return bool(
            cfg.get("reflexion", {}).get("recall", enabled())
        ) and enabled()
    except Exception:  # pragma: no cover -- config never blocks a run
        return False


def tools_from_blackboard(blackboard) -> list[str]:
    """Tool names a run invoked, parsed from the blackboard's observation
    posts (``tool=<name> -> ...``). Order-preserving + de-duplicated.
    Best-effort: any error yields an empty list.
    """
    seen: list[str] = []
    try:
        for e in getattr(blackboard, "entries", []) or []:
            if getattr(e, "kind", None) != "observation":
                continue
            m = re.match(r"tool=(\S+)", getattr(e, "content", "") or "")
            if m and m.group(1) not in seen:
                seen.append(m.group(1))
    except Exception:  # pragma: no cover
        pass
    return seen


def flaky_tools(
    *, min_count: int = 2, path: Path | None = None, scan: int = 300,
    matter_id: int | None = None,
) -> set[str]:
    """Tool names with >= ``min_count`` persisted ``tool_flaky`` lessons.

    Consumed by find_tools to demote tools the loop guard has repeatedly
    caught failing the same way — the recall side of the tool-failure
    taxonomy. Empty set on any error (fail-open)."""
    counts: dict[str, int] = {}
    matter_id = _exact_matter_id(
        matter_id if matter_id is not None else current_matter_id(),
    )
    if matter_id is None:
        return set()
    try:
        for r in list_recent(limit=scan, path=path):
            if r.matter_id != matter_id:
                continue
            if r.failure_class != "tool_flaky":
                continue
            for t in r.tools_used or []:
                counts[t] = counts.get(t, 0) + 1
    except Exception:  # pragma: no cover -- never blocks tool discovery
        return set()
    return {t for t, c in counts.items() if c >= max(1, min_count)}


def record_human_override(
    brief: str, tool_name: str, reason: str, *,
    domain: str | None = None, channel: str | None = None,
    user_id: str | None = None, matter_id: int | None = None,
    owner: str | None = None,
    path: Path | None = None,
) -> bool:
    """Persist a human's refusal of a gated action as a learning signal.

    Governance already audits the denial; this additionally turns the
    operator's "no" into a recallable lesson (failure_class
    ``human_override``) so the next similar goal proposes an alternative or
    seeks approval earlier — and the dreaming loop can consolidate repeated
    refusals into a department insight. No-op unless reflexion is enabled;
    never raises into the denial path.
    """
    try:
        if not enabled():
            return False
        goal_text = _sanitize_text(brief)[:500]
        return record(
            goal_text=goal_text,
            failure_class="human_override",
            failure_msg=f"tool {tool_name} not approved: {reason}"[:300],
            reflection=(
                f"A human declined to approve {tool_name} on a similar goal. "
                "Propose a less-privileged alternative, or surface the "
                "justification and ask for approval earlier in the run."
            ),
            tools_used=[tool_name],
            channel=channel,
            user_id=user_id,
            domain=domain,
            matter_id=matter_id,
            owner=owner,
            path=path,
        )
    except Exception as e:  # pragma: no cover -- learning never blocks a denial
        log.debug("human-override reflexion skipped: %s", e)
        return False


def synthesize_reflection(
    failure_class: str, failure_msg: str, tools_used: list[str]
) -> str:
    """Build a one-paragraph postmortem WITHOUT an extra LLM call.

    The failure path may itself be budget-exhausted, so we synthesize a
    deterministic lesson from the classified failure + the tools the run
    actually reached for. Cheap, never raises, and good enough to steer
    the next similar run away from the same dead end.
    """
    tools = ", ".join(tools_used[:8]) if tools_used else "no tools"
    msg = (failure_msg or "").strip().splitlines()
    head = msg[0][:200] if msg else "(no message)"
    return (
        f"Previous attempt failed ({failure_class}): {head}. "
        f"Tools reached for: {tools}. "
        "Next time, plan the approach before spending budget, and verify "
        "the failing step in isolation before scaling it up."
    )


__all__ = [
    "Reflexion",
    "DEFAULT_PATH",
    "default_path",
    "record",
    "matter_scope",
    "current_matter_id",
    "current_owner",
    "record_human_override",
    "flaky_tools",
    "recall",
    "list_recent",
    "clear",
    "format_context",
    "enabled",
    "tools_from_blackboard",
    "synthesize_reflection",
]
