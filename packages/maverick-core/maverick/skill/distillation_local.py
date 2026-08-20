"""Local continuous-learning skill loop: distill successful runs into a skill.

After a run succeeds, its trajectory (goal + the tools it used) is evidence of a
repeatable approach. This distills the top-k most recent successful trajectories
into a single reusable micro-skill — a ``SKILL.md`` with frontmatter (name,
triggers, tools_needed) and numbered steps — written to
an exact matter/owner namespace under ``~/.maverick/learned-skills/``. The
orchestrator recalls only from the current goal's namespace on later runs.

Enabled by default (``MAVERICK_DISTILL_LOCAL=0`` opts out). Dependency-free:
ranking is by success + recency and the synthesis is
lexical (no embedding model required), so the loop runs anywhere. ``distill`` and
``to_skill_markdown`` are pure and unit-tested; the generated skill is valid for
``maverick.skills.validate_skill_file``.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import data_dir

_STORE = data_dir("learned-skills", tenant=None)
_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "from",
    "by", "at", "as", "is", "are", "be", "this", "that", "it", "your", "my",
    "please", "then", "into", "write", "run", "do", "make", "get", "use",
}


def enabled() -> bool:
    try:
        from ..config import get_self_learning, governed_learning_env_flag
        override = governed_learning_env_flag("MAVERICK_DISTILL_LOCAL")
        if override is not None:
            return override
        return bool(get_self_learning().get("distill_local", True))
    except Exception:  # pragma: no cover
        return False


def _keywords(texts: list[str], k: int = 3) -> list[str]:
    counts: dict[str, int] = {}
    for t in texts:
        for w in re.findall(r"[a-z0-9]+", (t or "").lower()):
            if len(w) < 3 or w in _STOPWORDS:
                continue
            counts[w] = counts.get(w, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]


def _exact_scope(
    project_id: int | None, owner: str | None,
) -> tuple[int, str] | None:
    """Normalize the physical matter key + exact principal, fail closed."""
    if project_id is None or isinstance(project_id, bool) or owner is None:
        return None
    try:
        matter_id = int(project_id)
    except (TypeError, ValueError):
        return None
    if matter_id <= 0:
        return None
    return matter_id, str(owner)


def filter_trajectories(
    trajectories: list[dict], *, project_id: int | None, owner: str | None,
) -> list[dict]:
    """Only evidence from one exact matter/principal cohort may be pooled."""
    scope = _exact_scope(project_id, owner)
    if scope is None:
        return []
    matter_id, exact_owner = scope
    selected: list[dict] = []
    for trajectory in trajectories or []:
        raw_project = trajectory.get("project_id")
        if isinstance(raw_project, bool):
            continue
        try:
            trajectory_project = int(raw_project)
        except (TypeError, ValueError):
            continue
        if trajectory_project != matter_id:
            continue
        if "owner" not in trajectory or trajectory.get("owner") is None:
            continue
        if str(trajectory["owner"]) != exact_owner:
            continue
        selected.append(trajectory)
    return selected


def _owner_scope(owner: str) -> str:
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]


def _safe_goal(value: Any) -> str:
    """Bound and redact persisted goal text; detector failure drops it."""
    text = " ".join(str(value or "").split())[:2_000]
    if not text:
        return ""
    try:
        from ..safety.secret_detector import redact
        safe, _matches = redact(text)
        return " ".join(str(safe or "").split())[:2_000]
    except Exception:
        return ""


def distill(
    trajectories: list[dict], *, top_k: int = 3,
    project_id: int | None = None, owner: str | None = None,
) -> dict | None:
    """Distill successful trajectories into a skill dict, or ``None`` if none.

    Each trajectory: ``{goal, success, tools: [str], t: float}``. Picks the
    most-recent ``top_k`` successful runs, derives a kebab name + triggers from
    their goals, and unions their tools.
    """
    scoped = project_id is not None or owner is not None
    candidates = (
        filter_trajectories(
            trajectories, project_id=project_id, owner=owner,
        )
        if scoped else list(trajectories or [])
    )
    good = [t for t in candidates if t.get("success")]
    if not good:
        return None
    good.sort(key=lambda t: t.get("t", 0), reverse=True)
    chosen = good[:top_k]
    goals = [_safe_goal(t.get("goal")) for t in chosen if t.get("goal")]
    goals = [goal for goal in goals if goal]
    if not goals:
        return None

    kws = _keywords(goals)
    name = "-".join(kws) if kws else "learned-skill"
    name = re.sub(r"[^a-z0-9-]", "", name).strip("-") or "learned-skill"

    tools: list[str] = []
    for t in chosen:
        for tool in t.get("tools", []) or []:
            if tool not in tools:
                tools.append(tool)

    # Collapse ALL whitespace (incl. embedded newlines) before slicing: goal
    # text is written into the learned SKILL.md's YAML frontmatter, so a goal
    # containing a newline could otherwise inject frontmatter keys / break the
    # document structure (str.strip only trims the edges).
    triggers = []
    for g in goals:
        trg = " ".join(g.split()).lower()[:80]
        if trg and trg not in triggers:
            triggers.append(trg)

    # Goal IDS, not just titles: the entity graph derives its
    # skill -> goal lineage from these, and lineage must come from exact
    # keys -- a title is prose, an id is provenance. Absent ids (older
    # callers) simply yield a skill with no source edges, never a guess.
    source_goal_ids = []
    for t in chosen:
        gid = t.get("goal_id")
        if isinstance(gid, int) and gid > 0 and gid not in source_goal_ids:
            source_goal_ids.append(gid)

    skill = {
        "name": name,
        "triggers": triggers,
        "tools_needed": tools,
        # The body is injected into a future prompt. Keep raw historical goal
        # prose out of it: bounded lexical topics retain utility without
        # turning an old user instruction into a standing instruction.
        "summary": "repeatable workflow for " + (", ".join(kws) or name),
        "n_examples": len(chosen),
        "source_goal_ids": source_goal_ids,
    }
    scope = _exact_scope(project_id, owner)
    if scope is not None:
        matter_id, exact_owner = scope
        skill["project_id"] = matter_id
        skill["owner_scope"] = _owner_scope(exact_owner)
    return skill


def _utc_now_iso() -> str:
    """UTC timestamp for skill provenance (the colons in the time round-trip
    through the line-based frontmatter parser; no other punctuation)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_skill_markdown(skill: dict) -> str:
    """Render a distilled skill as a validator-compliant ``SKILL.md`` string.

    Carries machine-readable PROVENANCE in the frontmatter -- when it was
    learned, from how many examples, and by which path -- so a learned skill is
    inspectable and auditable (governed learning), not an anonymous blob. The
    validator accepts these keys and the in-memory Skill ignores them, but they
    are preserved on disk; values are kept special-char-free so the line-based
    frontmatter parser round-trips them."""
    lines = ["---", f"name: {skill['name']}", "triggers:"]
    lines += [f"  - {t}" for t in skill["triggers"]]
    if skill.get("tools_needed"):
        lines.append("tools_needed:")
        lines += [f"  - {t}" for t in skill["tools_needed"]]
    # Provenance (governed learning): structured, special-char-free values.
    lines.append(f"distilled_at: {skill.get('distilled_at') or _utc_now_iso()}")
    lines.append(f"n_examples: {int(skill.get('n_examples', 1))}")
    lines.append(f"source: {skill.get('source') or 'auto-distilled-local-v2'}")
    # The exact goals this skill was distilled from (space-separated ids,
    # special-char-free). This is what lets the entity graph answer "this run
    # turned out to be tainted -- which learned skills depend on it?" with
    # lineage instead of guesswork; a skill written before this key simply
    # reads as provenance-unknown.
    gids = [str(int(g)) for g in (skill.get("source_goal_ids") or [])
            if isinstance(g, int) and g > 0]
    if gids:
        lines.append(f"source_goal_ids: {' '.join(gids[:16])}")
    matter_id = skill.get("project_id")
    owner_scope = str(skill.get("owner_scope") or "")
    if isinstance(matter_id, int) and matter_id > 0 and re.fullmatch(
        r"[0-9a-f]{16}", owner_scope,
    ):
        lines.append(f"matter_id: {matter_id}")
        lines.append(f"owner_scope: {owner_scope}")
    lines.append("---")
    lines.append("")
    lines.append("# What this does")
    lines.append("")
    lines.append(
        f"Distilled from {skill.get('n_examples', 1)} successful run(s). "
        f"Approach for: {skill.get('summary', skill['name'])}.")
    lines.append("")
    lines.append("# Steps")
    lines.append("")
    if skill.get("tools_needed"):
        for i, tool in enumerate(skill["tools_needed"], 1):
            lines.append(f"{i}. Use the `{tool}` tool as the run did before.")
    else:
        lines.append("1. Follow the approach that succeeded on similar past goals.")
    return "\n".join(lines) + "\n"


def _default_store() -> Path:
    """Resolve the learned-skills store for the currently active tenant."""
    return data_dir("learned-skills")


def scoped_store(
    store: Path | str | None, *, project_id: int | None, owner: str | None,
) -> Path | None:
    """Private learned-skill namespace for one exact matter/principal."""
    scope = _exact_scope(project_id, owner)
    if scope is None:
        return None
    matter_id, exact_owner = scope
    root = Path(store) if store is not None else _default_store()
    return root / "matters" / f"matter-{matter_id}" / f"owner-{_owner_scope(exact_owner)}"


def read_sealed_skill(path: Path, root: Path) -> str | None:
    """Read one authenticated learned skill without writing plaintext to disk."""
    try:
        from ..learning_crypto import decode_text
        from ..skills import _read_regular_file

        return decode_text(_read_regular_file(path, root))
    except (OSError, RuntimeError, ValueError):
        return None


def _live_scope_allows_recall(project_id: int, owner: str) -> bool:
    try:
        from ..security_defaults import secure_by_default

        secure = bool(secure_by_default())
    except Exception:
        secure = True
    if not secure:
        return True
    try:
        from ..matter_context import refresh_matter_context

        context = refresh_matter_context()
    except Exception:
        return False
    return context.matter_id == project_id and context.principal == owner


def recall_context(
    goal_text: str, *, project_id: int | None, owner: str | None,
    shield: Any | None = None, max_n: int = 3,
) -> str:
    """Render relevant learned skills from one exact matter/owner namespace.

    Missing scope returns no context. The shared installed-skill loader is not
    used for this store because it would make learned client material visible
    to every matter in the tenant.
    """
    scope = _exact_scope(project_id, owner)
    if scope is None or not _live_scope_allows_recall(*scope):
        return ""
    store = scoped_store(None, project_id=project_id, owner=owner)
    if store is None or not store.is_dir() or not goal_text:
        return ""
    try:
        from ..skills import Skill, relevant_skills, render_for_prompt

        loaded = []
        for path in sorted(store.glob("*.md")):
            text = read_sealed_skill(path, store)
            if text is None:
                continue
            try:
                loaded.append(Skill.parse(text, path))
            except ValueError:
                continue
        selected = relevant_skills(
            goal_text, loaded, max_n=max(1, int(max_n)),
        )
        rendered = render_for_prompt(selected)
        if not rendered:
            return ""
        try:
            from ..security_defaults import secure_by_default

            secure = bool(secure_by_default())
        except Exception:
            secure = True
        if shield is None and secure:
            return ""
        if shield is not None:
            verdict = shield.scan_input(rendered)
            if not getattr(verdict, "allowed", False):
                return ""
        return rendered
    except Exception:
        # Learned memory is an enrichment, never a reason to break a run or to
        # fall back to a broader store.
        return ""


def save_skill(
    skill: dict, store: Path | str | None = None, *,
    project_id: int | None = None, owner: str | None = None,
) -> Path:
    """Write the distilled skill to ``<store>/<name>.md`` and return the path."""
    if not enabled():
        raise RuntimeError("local skill distillation is disabled")
    from ..learning_guard import learning_write_allowed
    if not learning_write_allowed("distillation_local", "save_skill"):
        raise RuntimeError("local skill distillation is halted")
    name = str(skill.get("name") or "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 96:
        raise ValueError("distilled skill name must be bounded kebab-case")
    from ..file_lock import atomic_write_text, ensure_private_directory
    from ..learning_crypto import encode_text, protected_learning_enabled

    if protected_learning_enabled() and _exact_scope(project_id, owner) is None:
        raise ValueError("firm distilled skill requires an exact matter and owner")
    if project_id is not None or owner is not None:
        scope = _exact_scope(project_id, owner)
        scoped = scoped_store(store, project_id=project_id, owner=owner)
        if scope is None or scoped is None:
            raise ValueError("distilled skill requires an exact matter and owner")
        matter_id, exact_owner = scope
        skill = {
            **skill,
            "project_id": matter_id,
            "owner_scope": _owner_scope(exact_owner),
        }
        store = scoped.resolve()
    else:
        store = (Path(store) if store is not None else _default_store()).resolve()
    ensure_private_directory(store)
    path = store / f"{name}.md"
    if path.exists() and read_sealed_skill(path, store) is None:
        raise RuntimeError("learned skill authentication failed")
    atomic_write_text(path, encode_text(to_skill_markdown(skill)), mode=0o600)
    return path


def distill_and_save(
    trajectories: list[dict], *, top_k: int = 3,
    store: Path | str | None = None, project_id: int | None = None,
    owner: str | None = None,
) -> Path | None:
    """Distill + persist in one exact matter/owner namespace."""
    if _exact_scope(project_id, owner) is None:
        return None
    skill = distill(
        trajectories, top_k=top_k, project_id=project_id, owner=owner,
    )
    return save_skill(
        skill, store, project_id=project_id, owner=owner,
    ) if skill else None


__all__ = [
    "enabled", "filter_trajectories", "distill", "to_skill_markdown",
    "scoped_store", "read_sealed_skill", "recall_context", "save_skill",
    "distill_and_save",
]
