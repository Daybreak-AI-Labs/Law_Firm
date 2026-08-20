"""Self-Harness: learn a MODEL-SPECIFIC harness addendum from failure traces.

Implements the loop from *"Self-Harness: Harnesses That Improve Themselves"*
(arXiv 2606.09498) on top of Maverick's existing governance spine, rather than
as a new ungoverned optimizer:

  MINE     recurring failure *signatures* from one model's reflexion traces
  PROPOSE  a minimal operating-guidance line targeting each signature
  VALIDATE the proposal on held-in AND held-out cases -- accept only when it
           does not regress either split and helps at least one (the paper's
           rule: reject a pure trade of one split for another)
  GATE     feed the validated change through ``self_improvement.consider()`` on
           the ``prompt`` rung -- so it inherits evidence/causal gating, the
           calibration-freeze interlock, capability non-escalation, reversibility,
           and the signed learning audit, exactly like every other learned rung.

Why this shape. The paper credits its gains to treating the harness as a
*model-specific, learnable* artifact. Maverick already learns *behaviors*
(skills, insights) that are recalled as context; this adds a learned, per-model
**operating-guidance addendum** that is recalled into the system prompt at build
time (:func:`recall_addendum`) -- never a mutation of the kernel templates. So
it sits inside the same "behavior recalled as context, snapshot + rollback"
safety model: an addendum is a file entry, removing it is the rollback handle.

The LLM proposer is an injected seam (:data:`ProposeFn`); a deterministic
fallback composes a guidance line from the signature so the loop runs and is
unit-testable without a provider. Validation likewise takes injected scorers --
a live A/B needs a real model, exactly as ``learning_rollout`` takes injected
constraints. A clean deployment enables the conservative ``risk_limited``
profile; ``MAVERICK_SELF_HARNESS=0`` or config can opt out.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .audit.errors import AuditRefused
from .learning_guard import Halted, check_learning_halt
from .paths import data_dir

log = logging.getLogger(__name__)

# Keep an addendum short: it rides in EVERY system prompt for that model, so a
# runaway block would tax cache + context on every turn. A handful of crisp
# lines is the whole point ("minimal" in the paper).
_MAX_ADDENDUM_CHARS = 1500
_MAX_LINES_PER_MODEL = 8
# DoS backstop for mining: greedy clustering is worst-case O(n^2), so an
# unbounded trace list could hang a pass. Generous -- the runner feeds at most
# ``limit`` (500) recent traces, so this never trips in normal use; it only
# bounds a pathological direct caller. See mine_failures.
_MAX_MINE_TRACES = 4000
# Bound per-text semantic feature extraction. Goal descriptions can be long and
# semantic mining compares each record against cluster heads; capping the
# offline trigram input and caching extracted features below prevents long
# attacker-controlled failure texts from turning maintenance mining into a CPU
# and memory churn amplifier while preserving short/normal goal behavior.
_MAX_SEMANTIC_TEXT_CHARS = 4096
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def enabled() -> bool:
    """Whether the risk-limited self-harness loop is active. ON by default.

    Opt out with ``MAVERICK_SELF_HARNESS=0`` or ``[self_harness] enable = false``.
    When off, :func:`recall_addendum` returns ``""`` (the prompt is unchanged)
    and :func:`run_self_harness` is a no-op."""
    try:
        from .config import get_self_harness, governed_learning_env_flag
        override = governed_learning_env_flag("MAVERICK_SELF_HARNESS")
        if override is not None:
            return override
        return bool(get_self_harness().get("enable", True))
    except Exception:  # pragma: no cover -- config never blocks a run
        return False


def settings() -> dict:
    """Resolved ``[self_harness]`` settings (tuning knobs) with defaults filled
    in. Defaults preserve the loop's historical behavior; an operator opts into
    the stricter floors / optional paths via config or the wizard."""
    try:
        from .config import get_self_harness
        resolved = get_self_harness()
        if not isinstance(resolved, dict):
            raise ValueError("self-harness settings are not a mapping")
        return {**resolved, "_config_valid": True}
    except Exception:  # pragma: no cover -- config never blocks a run
        return {"enable": False, "risk_limited": True, "auto_run": False,
                "_config_valid": False,
                "min_support": 3, "min_support_by_class": {},
                "require_held_out": True, "min_held_out": 8, "min_delta": 0.02,
                "confidence_z": 1.96, "max_cost_factor": 1.25,
                "max_latency_factor": 1.25, "max_tool_calls_factor": 1.10,
                "semantic_mining": False, "retire_after_days": 0.0,
                "candidates_per_signature": 3, "mine_bucket_by": (),
                "max_promotions_per_cycle": 1,
                "holdout_rotations": 1, "judge_samples": 3,
                "holdout_ledger": None, "holdout_family_alpha": 0.05,
                "holdout_query_alpha": 0.05, "holdout_max_queries": 1,
                "metamorphic": True, "metamorphic_tolerance": 0.0,
                "calibrate_judge": True, "calibration_max_age_hours": 24.0,
                "corpus_harvest": "off", "store": "files",
                "relapse_failure_share": 0.0, "relapse_min_outcomes": 5,
                "promote_as_canary": True,
                "efficacy_review": False, "eval_corpus": None,
                "eval_budget_dollars": None}


def _store_path() -> Path:
    return data_dir("harness") / "addenda.json"


def _world_routed(p: Path) -> bool:
    """True when ``p`` is the DEFAULT store location and the operator selected
    the world learning store. An EXPLICIT non-default path always means the
    file store at that path, so tests and tenant redirection are byte-for-byte
    unchanged."""
    try:
        if str(settings().get("store") or "files").strip().lower() != "world":
            return False
        return Path(p) == _store_path()
    except Exception:  # pragma: no cover -- config trouble means file store
        return False


def _store_rmw_lock(p: Path):
    """DB-side critical-section lock for the world store: ``flock`` only
    serializes ONE host, so a multi-host Postgres deployment also takes a session
    advisory lock around the whole load-modify-save. A no-op for the file
    store and the (single-host) SQLite world store."""
    if _world_routed(p):
        from . import learning_store
        return learning_store.rmw_lock()
    import contextlib
    return contextlib.nullcontext()


# ---- store (the learned, per-model addenda) -------------------------------

def load_addenda(path: Path | None = None) -> dict[str, str]:
    """The accepted ``{model_id: addendum_text}`` map (empty on any error)."""
    p = path if path is not None else _store_path()
    if _world_routed(p):
        from . import learning_store
        raw = learning_store.load_addenda_db()
        return {str(k): v for k, v in raw.items()
                if isinstance(v, str) and v.strip()}
    try:
        from .learning_crypto import decode_text

        decoded = decode_text(p.read_text(encoding="utf-8"))
        if decoded is None:
            return {}
        data = json.loads(decoded)
        if isinstance(data, dict):
            # Accept ONLY string values: a tampered/corrupt store with a numeric
            # or null value must not coerce to "123"/"None" and get recalled into
            # a prompt as literal garbage.
            return {str(k): v for k, v in data.items()
                    if isinstance(v, str) and v.strip()}
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {}


def _write_addenda(addenda: dict[str, str], path: Path | None = None) -> None:
    p0 = path if path is not None else _store_path()
    if _world_routed(p0):
        from . import learning_store
        learning_store.write_addenda_db(addenda)
        return
    # Route through atomic_write_text: a UNIQUE temp + os.replace + chmod 0600
    # that CLEANS THE TEMP UP on any failure. A hand-rolled fixed "<name>.tmp"
    # instead would (a) leave a stale .tmp behind whenever os.replace fails
    # (disk full / read-only FS / target is a dir) and (b) let an UNLOCKED
    # rollback racing a locked apply collide on the shared temp -- one
    # os.replace moving the temp out from under the other. The store itself
    # stays atomic either way, but the stray temp is real. (Found by the
    # fault-injection battery.)
    from .file_lock import atomic_write_text
    p = path if path is not None else _store_path()
    from .learning_crypto import encode_text

    payload = encode_text(json.dumps(addenda, indent=2, sort_keys=True))
    atomic_write_text(p, payload, mode=0o600)


def _load_addenda_strict(path: Path) -> dict[str, str]:
    """Load the prompt-bound store for a privileged CAS, failing on corruption.

    The public recall path remains tolerant so a damaged optional addendum never
    breaks an agent run. Promotion is different: treating malformed state as an
    empty mapping would overwrite evidence and make an ungoverned mutation look
    like a clean baseline.
    """
    if _world_routed(path):
        from . import learning_store
        raw = learning_store.load_addenda_db(strict=True)
    else:
        if path.is_symlink():
            raise ValueError("addenda store must not be a symbolic link")
        try:
            from .learning_crypto import decode_text

            decoded = decode_text(path.read_text(encoding="utf-8"))
            if decoded is None:
                raise ValueError("addenda store is not authenticated ciphertext")
            raw = json.loads(decoded)
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("addenda store is unreadable or malformed") from exc
    if not isinstance(raw, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw.items()):
        raise ValueError("addenda store must map strings to strings")
    return dict(raw)


def _addenda_artifact_revision(path: Path, store: Mapping[str, str]):
    """Return the content-addressed full-store revision used by promotion CAS."""
    from .self_improvement import ArtifactRevision

    canonical = json.dumps(
        dict(store), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    version = "sha256:" + hashlib.sha256(b"self-harness-version\0" + canonical).hexdigest()
    location = "world" if _world_routed(path) else os.path.normcase(
        os.path.abspath(os.fspath(path)))
    return ArtifactRevision(
        identity=f"self_harness:addenda:{location}",
        sha256=digest,
        version=version,
    )


def _scoped_key(model_id: str, context: str) -> str:
    """The store key for a (model, mining-scope). Model-wide guidance (no context)
    lives under the bare ``model_id`` -- so the default store is byte-identical to
    before scoping existed. A domain-scoped line lives under a COMPOSITE key
    ``"<model_id>\\x00<context>"`` (e.g. ``"<model>\\x00domain=finance"``); the NUL
    can't appear in a model id, so the two namespaces never collide."""
    return f"{model_id}\x00{context}" if context else str(model_id)


def _owner_scope_digest(owner: str) -> str:
    """Return the opaque, stable owner namespace used by learned artifacts."""
    return hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:16]


def _matter_scoped_key(
    model_id: str, *, matter_id: int, owner_scope: str, context: str = "",
) -> str:
    """Key one addendum block to an exact matter and opaque owner.

    The principal itself never enters the prompt store.  The optional existing
    domain/role/tool context is nested *inside* the matter/owner namespace, so
    no contextual variant can escape to a model-global key.
    """
    if (
        isinstance(matter_id, bool)
        or not isinstance(matter_id, int)
        or matter_id <= 0
        or not re.fullmatch(r"[0-9a-f]{16}", owner_scope or "")
    ):
        raise ValueError("exact matter and owner scope required")
    matter_context = f"matter={matter_id};owner={owner_scope}"
    if context:
        matter_context += f";{context}"
    return _scoped_key(str(model_id), matter_context)


def _secure_defaults_enabled() -> bool:
    """Resolve the production posture; uncertainty remains secure."""
    try:
        from .security_defaults import secure_by_default

        return secure_by_default()
    except Exception:  # pragma: no cover - policy failure must not enable global recall
        return True


def _bound_matter_owner_scope() -> tuple[int, str] | None:
    """Resolve the active execution namespace from its validated context."""
    try:
        from .matter_context import (
            GOAL_EXECUTION_PURPOSE,
            MatterContext,
            current_matter_context,
        )

        context = current_matter_context()
        if (
            not isinstance(context, MatterContext)
            or context.purpose != GOAL_EXECUTION_PURPOSE
        ):
            return None
        matter_id = context.matter_id
        principal = context.principal
        if (
            isinstance(matter_id, bool)
            or not isinstance(matter_id, int)
            or matter_id <= 0
            or not isinstance(principal, str)
            or not principal
        ):
            return None
        return matter_id, _owner_scope_digest(principal)
    except Exception:  # pragma: no cover - malformed/missing context fails closed
        return None


def _runtime_store_keys(
    model_id: str, contexts: list[str], *, secure: bool,
) -> list[str]:
    """Return only the prompt keys authorized for this runtime invocation."""
    mid = str(model_id)
    if not secure:
        return [mid] + [_scoped_key(mid, context) for context in contexts]
    scope = _bound_matter_owner_scope()
    if scope is None:
        return []
    matter_id, owner_scope = scope
    return [
        _matter_scoped_key(
            mid, matter_id=matter_id, owner_scope=owner_scope, context=context,
        )
        for context in ["", *contexts]
    ]


def _promotion_store_key(
    model_id: str, context: str, *, matter_id: int, owner_scope: str,
) -> str:
    """Select a governed promotion key for the active security posture."""
    if not _secure_defaults_enabled():
        return _scoped_key(str(model_id), context)
    return _matter_scoped_key(
        str(model_id), matter_id=matter_id,
        owner_scope=owner_scope, context=context,
    )


def _scope_contexts(domain: str | None = None,
                    tools: list[str] | tuple[str, ...] | None = None,
                    role: str | None = None) -> list[str]:
    """The ordered, deduped list of mining-scope context strings for a run:
    ``domain=<d>`` first (one department per run), then ``role=<r>`` (one role
    per agent), then ``tool=<t>`` for each tool the run had on hand, SORTED so
    the recalled block is deterministic regardless of the agent's tool-registry
    iteration order. Empty entries are dropped. The shared basis for recall /
    usage / outcome scoping so all three stay in step."""
    out: list[str] = []
    if domain:
        out.append(f"domain={domain}")
    if role:
        out.append(f"role={role}")
    for t in sorted({str(t) for t in (tools or []) if t}):
        out.append(f"tool={t}")
    return out


def recall_addendum(model_id: str | None, path: Path | None = None, *,
                    domain: str | None = None,
                    tools: list[str] | tuple[str, ...] | None = None,
                    role: str | None = None) -> str:
    """The learned operating-guidance block for ``model_id`` (``""`` if none /
    disabled). Recalled into the system prompt by the agent at build time.

    Under secure defaults every block is nested under the currently bound exact
    matter and hashed principal. Missing context yields no guidance, and legacy
    global keys are never a fallback. Explicitly disabling secure defaults keeps
    the historical model/domain/role/tool key layout for local compatibility.
    """
    if not model_id or not enabled():
        return ""
    contexts = _scope_contexts(domain, tools, role)
    keys = _runtime_store_keys(
        str(model_id), contexts, secure=_secure_defaults_enabled(),
    )
    if not keys:
        return ""
    store = load_addenda(path)
    block = ""
    for key in keys:
        scoped = store.get(key, "")
        if scoped:
            block = (block + "\n" + scoped) if block else scoped
    return block


# ---- sidecar: structured per-line provenance ------------------------------
# The addenda store is the prompt-bound source of truth and is kept byte-stable
# (its determinism is a proven property). Per-line PROVENANCE (why a line was
# learned, when, on what evidence) lives in a SIBLING ``*.meta.json`` keyed by a
# content-addressed line id, reconciled to the block under the same lock. It is
# strictly auxiliary: a missing/corrupt sidecar never affects recall, and a
# legacy line with no record simply has no provenance (and is never auto-retired,
# since its age is unknown). This is the structured-record foundation that
# unblocks retirement, conflict detection, and post-promotion efficacy.

def _meta_path(addenda_path: Path | None = None) -> Path:
    p = addenda_path if addenda_path is not None else _store_path()
    return p.with_suffix(".meta.json")


def _line_id(model_id: str, line: str) -> str:
    """Stable content-addressed id for a (model, line). Normalized so a trivial
    reword (the delta-merge's notion of "the same line") maps to the same id."""
    h = hashlib.sha256(f"{model_id}\x00{_norm_line(line)}".encode())
    return h.hexdigest()[:16]


def load_line_meta(path: Path | None = None) -> dict[str, dict]:
    """The ``{line_id: record}`` provenance sidecar (empty on any error)."""
    if _world_routed(path if path is not None else _store_path()):
        from . import learning_store
        return learning_store.load_line_meta_db()
    mp = _meta_path(path)
    try:
        from .learning_crypto import decode_text

        decoded = decode_text(mp.read_text(encoding="utf-8"))
        if decoded is None:
            return {}
        data = json.loads(decoded)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except (FileNotFoundError, ValueError, OSError):
        pass
    return {}


def _write_line_meta(meta: dict[str, dict], path: Path | None = None) -> None:
    if _world_routed(path if path is not None else _store_path()):
        from . import learning_store
        learning_store.write_line_meta_db(meta)
        return
    from .file_lock import atomic_write_text
    from .learning_crypto import encode_text

    payload = encode_text(json.dumps(meta, indent=2, sort_keys=True))
    atomic_write_text(_meta_path(path), payload, mode=0o600)


def _reconcile_meta(meta: dict[str, dict], model_id: str,
                    current_lines: list[str]) -> dict[str, dict]:
    """Drop sidecar records for ``model_id`` whose line is no longer in the block
    (evicted under the cap, merged, or forgotten). Records for OTHER models are
    untouched. Returns the same dict (mutated) for convenience."""
    valid = {_line_id(model_id, ln) for ln in current_lines}
    for lid in [k for k, r in meta.items()
                if r.get("model_id") == model_id and k not in valid]:
        del meta[lid]
    return meta


def _upsert_line_meta(meta: dict[str, dict], model_id: str, line: str,
                      provenance: dict, *, now: float) -> None:
    """Record/refresh a line's provenance. First-seen ``learned_at`` is kept;
    ``updated_at`` and the evidence refresh on re-promotion."""
    lid = _line_id(model_id, line)
    rec = meta.get(lid) or {}
    rec.update({
        "model_id": model_id, "text": line,
        "signature": provenance.get("signature"),
        "rationale": provenance.get("rationale"),
        "hypothesis": provenance.get("hypothesis"),
        "held_out_delta": provenance.get("held_out_delta"),
        "samples": provenance.get("samples"),
        "learned_at": rec.get("learned_at", now),
        "updated_at": now,
    })
    # Canary is only touched when the caller passes it (a non-canary re-promote
    # must not clear a probation flag, nor stamp canary=False on every record).
    if "canary" in provenance:
        rec["canary"] = bool(provenance.get("canary"))
    meta[lid] = rec


def line_provenance(model_id: str, path: Path | None = None) -> list[dict]:
    """Per-line provenance for ``model_id`` across ALL its scopes, ordered to match
    the recalled blocks (newest last). Each entry is ``{text, domain, signature,
    rationale, held_out_delta, samples, learned_at, updated_at}``; fields are
    ``None`` for a legacy line with no record, and ``domain`` is ``None`` for a
    model-wide line. Includes domain-scoped lines (which the pre-fix version
    missed). Read-only; powers ``self-harness show --verbose``."""
    store = load_addenda(path)
    meta = load_line_meta(path)
    fields = ("signature", "rationale", "hypothesis", "held_out_delta", "samples",
              "learned_at", "updated_at", "last_recalled_at", "recall_notes")
    out = []
    for k, ln in _iter_model_lines(store, str(model_id)):
        rec = meta.get(_line_id(k, ln)) or {}
        _, ctx = _split_key(k)
        domain = ctx[len("domain="):] if ctx.startswith("domain=") else None
        out.append({"text": ln, "domain": domain, **{f: rec.get(f) for f in fields}})
    return out


def _bullets(block: str) -> list[str]:
    return [ln[2:].strip() for ln in (block or "").splitlines()
            if ln.startswith("- ")]


def _split_key(store_key: str) -> tuple[str, str]:
    """Decompose a store key into ``(model_id, context)`` -- the inverse of
    :func:`_scoped_key`. A bare model key yields ``(model_id, "")``; a composite
    ``"<model>\\x00domain=finance"`` yields ``("<model>", "domain=finance")``. Used
    wherever a store key is SURFACED to an operator/audit/dashboard so the raw NUL
    never leaks."""
    mid, _, ctx = str(store_key).partition("\x00")
    return mid, ctx


def _model_store_keys(store: dict, model_id: str) -> list[str]:
    """Every store key belonging to ``model_id``: the bare key (if present) plus
    every composite ``"<model_id>\\x00..."`` scope key, in deterministic order. The
    management surface (forget/retire/efficacy/canary) walks this so a model's
    DOMAIN-scoped lines are never invisible to it -- with no composite keys the
    list is just ``[model_id]``, so the default store behaves exactly as before."""
    mid = str(model_id)
    keys = [mid] if mid in store else []
    keys += sorted(k for k in store if isinstance(k, str) and k.startswith(mid + "\x00"))
    return keys


def _iter_model_lines(store: dict, model_id: str):
    """Yield ``(store_key, line)`` over every line in every scope of ``model_id``,
    so a reader can compute the right ``_line_id(store_key, line)`` for the sidecar
    record actually written by :func:`_apply_addendum`."""
    for k in _model_store_keys(store, model_id):
        for ln in _bullets(store.get(k, "")):
            yield k, ln


def _norm_line(s: str) -> str:
    """Canonical form for delta-merge equality: case-folded, whitespace-collapsed,
    trailing punctuation stripped. Two lines with the same normal form are the
    SAME guidance reworded only trivially (e.g. an LLM proposer returning
    "Verify the token." then later "verify the token") -- they should refresh,
    not occupy two of the bounded slots. Deliberately EXACT-after-normalization,
    not fuzzy: templated per-class lines differ by a single token, and a fuzzy
    threshold would wrongly merge distinct failure classes."""
    return " ".join(str(s or "").split()).casefold().rstrip(".,;:!? ")


# ---- CONFLICT DETECTION (advisory) ----------------------------------------
# Addenda are CUMULATIVE: a new line can quietly contradict an existing one
# ("prefer streaming large exports" vs "avoid streaming exports; batch first"),
# silently degrading the prompt. This is a cheap DETERMINISTIC heuristic --
# shared topic + OPPOSITE polarity -- that FLAGS suspected conflicts for an
# operator; it never auto-blocks (a false positive must not drop real guidance).
# Stopwords + the loop's own template scaffolding are removed so "topic overlap"
# reflects the guidance content, not the boilerplate every line shares.
_STOPWORDS = frozenset(
    "a an the to of for and or is are be it this that with on in at as your you "
    "when whenever goal resembles past failures kind has have had before after "
    "its their them they not no".split())
_NEG_CUES = ("avoid", "never", "don't", "dont", "do not", "without", "no longer",
             "stop ", "skip ", "n't", "cannot", "can't", " not ", "refrain")


def _content_tokens(line: str) -> set[str]:
    return _tokens(line) - _STOPWORDS


def _is_negative(line: str) -> bool:
    """Whether a guidance line is phrased as a prohibition (avoid/never/don't...)."""
    low = " " + (line or "").lower() + " "
    return any(cue in low for cue in _NEG_CUES)


def find_conflicts(new_line: str, existing_lines: list[str], *,
                   min_overlap: float = 0.5,
                   classifier_fn: Callable[[str, str], bool] | None = None) -> list[str]:
    """Existing lines that appear to CONTRADICT ``new_line``: high content-token
    overlap (same topic) but OPPOSITE polarity (one prohibits what the other
    prefers). Advisory and deliberately conservative (high overlap threshold) --
    the caller surfaces these for review, never auto-drops on them.

    ``classifier_fn`` is the SEMANTIC seam: an injected ``(a, b) -> bool`` judge
    (e.g. an LLM) that decides contradiction by MEANING -- it catches conflicts
    the lexical heuristic misses (reworded, no token overlap) and suppresses its
    false positives. When present it is authoritative per pair; a classifier that
    RAISES falls back to the heuristic for that pair, so a flaky judge never drops
    a real flag. Addenda are bounded (<= _MAX_LINES_PER_MODEL), so the O(n^2)
    pairwise calls stay tiny. Still advisory -- the gate never blocks on it."""
    nt, neg = _content_tokens(new_line), _is_negative(new_line)
    out = []
    for ex in existing_lines or []:
        if _norm_line(ex) == _norm_line(new_line):
            continue
        if classifier_fn is not None:
            try:
                if bool(classifier_fn(new_line, ex)):
                    out.append(ex)
                continue  # classifier is authoritative for this pair
            except Exception:  # a bad judge can't drop a real conflict -> heuristic
                pass
        if _jaccard(nt, _content_tokens(ex)) >= min_overlap and _is_negative(ex) != neg:
            out.append(ex)
    return out


def detect_store_conflicts(model_id: str | None = None,
                           path: Path | None = None, *,
                           classifier_fn: Callable[[str, str], bool] | None = None,
                           ) -> list[tuple[str, str, str]]:
    """Suspected contradictory pairs in the CURRENT store as ``(model, a, b)``,
    de-duplicated (each unordered pair once). Powers ``self-harness conflicts``.
    An optional ``classifier_fn`` (see :func:`find_conflicts`) refines the lexical
    heuristic with a semantic judge."""
    out: list[tuple[str, str, str]] = []
    for store_key, block in load_addenda(path).items():
        mid, ctx = _split_key(store_key)
        if model_id is not None and mid != model_id:
            continue
        # Surface a READABLE label, never the raw NUL composite key: "M" for
        # model-wide, "M [domain=finance]" for a scoped block.
        label = mid if not ctx else f"{mid} [{ctx}]"
        lines = _bullets(block)
        seen: set[frozenset] = set()
        for i, ln in enumerate(lines):
            for other in find_conflicts(ln, lines[i + 1:], classifier_fn=classifier_fn):
                key = frozenset((_norm_line(ln), _norm_line(other)))
                if key not in seen:
                    seen.add(key)
                    out.append((label, ln, other))
    return out


def list_learned(path: Path | None = None) -> dict[str, list[str]]:
    """The learned guidance lines per model, for operator inspection.

    Returns ``{model_id: [line, ...]}`` parsed from the stored addenda -- the
    same lines :func:`recall_addendum` injects, minus the framing header. Keys are
    the BARE model id (never the raw NUL composite key); a domain-scoped line is
    listed under its model with a ``"  [domain=…]"`` tag so an operator/dashboard
    sees the scope without the store's internal key layout. Note this reads the
    STORE regardless of :func:`enabled`; the recall path is what gates on the
    toggle, so an operator can still inspect/roll back what was learned while the
    feature is paused."""
    out: dict[str, list[str]] = {}
    for store_key, block in load_addenda(path).items():
        lines = _bullets(block)
        if not lines:
            continue
        mid, ctx = _split_key(store_key)
        tagged = lines if not ctx else [f"{ln}  [{ctx}]" for ln in lines]
        out.setdefault(mid, []).extend(tagged)
    return out


def forget_addendum(model_id: str, *, line: str | None = None,
                    domain: str | None = None, path: Path | None = None) -> bool:
    """Operator rollback: remove a model's learned addendum, or a single line.

    The user-facing undo handle for self-harness learning. By default it spans ALL
    the model's scopes (so a full rollback can't orphan a domain block); pass
    ``domain`` to scope the removal to just that department's block. The removal is
    serialized + atomic like every other store write, and is itself audited so the
    rollback leaves a trail. Returns ``True`` if something was removed, ``False``
    if there was nothing matching to remove."""
    p = path if path is not None else _store_path()
    from .file_lock import cross_process_lock
    removed = False
    with _lock, cross_process_lock(p), _store_rmw_lock(p):
        before = load_addenda(p)
        # Span ALL of the model's scopes (bare + composite "<model>\x00domain=...")
        # so a full rollback can't ORPHAN a domain-scoped block that recall still
        # injects, and a single-line forget can target a line in any scope. With an
        # explicit domain, operate ONLY on that scope's composite key.
        if domain:
            sk = _scoped_key(str(model_id), f"domain={domain}")
            keys = [sk] if sk in before else []
        else:
            keys = _model_store_keys(before, model_id)
        if not keys:
            return False
        after = dict(before)
        touched: list[str] = []
        for k in keys:
            if line is None:
                del after[k]
                touched.append(k)
                removed = True
            else:
                existing = _bullets(before[k])
                kept = [ln for ln in existing if ln != line]
                if len(kept) == len(existing):
                    continue  # the line isn't in this scope's block
                removed = True
                touched.append(k)
                if kept:
                    header = "Operating guidance learned for this model:"
                    after[k] = header + "\n" + "\n".join(f"- {ln}" for ln in kept)
                else:
                    del after[k]
        if not removed:
            return False
        _write_addenda(after, p)
        try:  # keep the provenance sidecar in step (best-effort)
            meta = load_line_meta(p)
            for k in touched:
                _reconcile_meta(meta, k, _bullets(after.get(k, "")))
            _write_line_meta(meta, p)
        except Exception:  # pragma: no cover -- sidecar is best-effort
            log.debug("self_harness: line-meta prune failed", exc_info=True)
    from .audit import EventKind, audit_event

    audit_event(
        EventKind.LEARNING_UPDATE,
        agent="self_harness",
        model_id=model_id,
        rung="prompt",
        line_sha256=(
            hashlib.sha256(line.encode("utf-8")).hexdigest() if line else "*"
        ),
        phase="forget",
    )
    return removed


def retire_stale(*, older_than_days: float, model_id: str | None = None,
                 now: float | None = None, path: Path | None = None) -> int:
    """Retire learned lines not refreshed within ``older_than_days``.

    Prompt guidance goes stale as models, tools, and APIs change; a loop that
    only ever accumulates eventually carries obsolete instructions. Age is a
    line's ``updated_at`` in the provenance sidecar (re-promotion refreshes it),
    so a line that keeps proving useful stays. A line with NO record (legacy /
    pre-sidecar) is NEVER retired -- its age is unknown. Removes from both the
    addenda block and the sidecar, audits each removal with phase ``retire``, and
    returns the count. ``now`` is injectable for testing. Never raises."""
    p = path if path is not None else _store_path()
    cutoff = (now if now is not None else time.time()) - older_than_days * 86400.0
    removed: list[tuple[str, str]] = []
    from .file_lock import cross_process_lock
    try:
        with _lock, cross_process_lock(p), _store_rmw_lock(p):
            add = load_addenda(p)
            meta = load_line_meta(p)
            after = dict(add)
            for m, block in list(add.items()):
                # Match the BARE model so a per-model retire also covers that
                # model's domain-scoped blocks (composite keys); ``m`` stays the
                # store key for meta/line-id ops (which are keyed by it).
                if model_id is not None and _split_key(m)[0] != model_id:
                    continue
                bullets = _bullets(block)
                kept = []
                for ln in bullets:
                    rec = meta.get(_line_id(m, ln)) or {}
                    # Staleness is by last ACTIVITY: a line refreshed (promoted)
                    # OR used (recalled) recently is not stale.
                    active = [t for t in (rec.get("updated_at"), rec.get("last_recalled_at"))
                              if isinstance(t, (int, float))]
                    last_active = max(active) if active else None
                    (removed.append((m, ln)) if last_active is not None and last_active < cutoff
                     else kept.append(ln))
                if kept == bullets:
                    continue
                if kept:
                    header = "Operating guidance learned for this model:"
                    after[m] = header + "\n" + "\n".join(f"- {x}" for x in kept)
                else:
                    after.pop(m, None)
                _reconcile_meta(meta, m, kept)
            if removed:
                _write_addenda(after, p)
                try:
                    _write_line_meta(meta, p)
                except Exception:  # pragma: no cover -- sidecar best-effort
                    pass
    except Exception:  # pragma: no cover -- retirement never perturbs a run
        log.warning("self_harness: retire_stale failed", exc_info=True)
        return 0
    for m, ln in removed:
        from .audit import EventKind, audit_event

        # Audit with the BARE model id (+ scope), never the raw NUL key or
        # learned instruction text.
        mid, ctx = _split_key(m)
        audit_event(
            EventKind.LEARNING_UPDATE,
            agent="self_harness",
            model_id=mid,
            scope=(ctx or None),
            rung="prompt",
            line_sha256=hashlib.sha256(ln.encode("utf-8")).hexdigest(),
            phase="retire",
        )
    return len(removed)


# ---- MINE -----------------------------------------------------------------

@dataclass(frozen=True)
class FailureSignature:
    """A recurring failure pattern for one model -- the weakness to target."""
    model_id: str
    failure_class: str
    signature: str              # short human description of the recurring failure
    support: int                # how many traces back it
    examples: tuple[str, ...]   # representative (sanitized) goal texts
    context: str = ""           # optional mining scope (e.g. "domain=finance")
                                # when failures were bucketed by a finer dimension;
                                # "" = model-wide (the default, backward compatible)


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    """Character n-grams of the whitespace-normalized lowercase text. Catches
    morphological overlap that whole-token Jaccard misses (authenticate vs
    authentication share trigrams; share no whole token). Long texts are clipped
    to keep semantic mining's pairwise comparisons bounded."""
    s = " ".join((text or "").lower().split())[:_MAX_SEMANTIC_TEXT_CHARS]
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _semantic_features(text: str) -> tuple[set[str], set[str]]:
    """Precomputed features for the built-in semantic similarity."""
    return _tokens((text or "")[:_MAX_SEMANTIC_TEXT_CHARS]), _char_ngrams(text)


def _semantic_similarity_from_features(
    a: tuple[set[str], set[str]], b: tuple[set[str], set[str]],
) -> float:
    """Built-in semantic similarity over precomputed token/trigram sets."""
    tok = _jaccard(a[0], b[0])
    ng = _jaccard(a[1], b[1])
    return max(tok, ng)


def semantic_similarity(a: str, b: str) -> float:
    """Deterministic, offline, dictionary-free similarity for *semantic-ish*
    mining: the max of token-Jaccard and character-trigram-Jaccard, so
    morphological / reworded variants (authenticate vs authentication, "timeout"
    vs "timed out") cluster even when whole-token overlap is low.

    This is NOT true embedding semantics -- inject a ``similarity_fn`` into
    :func:`mine_failures` for that (the live, model-backed path). It is the
    deterministic default the ``semantic_mining`` flag turns on so enabling the
    feature is a real, reproducible improvement over strict token overlap without
    requiring a provider, and it stays inside the determinism guarantee."""
    return _semantic_similarity_from_features(_semantic_features(a), _semantic_features(b))


def _is_minable(r: object, model_id: str) -> bool:
    """Whether a single reflexion is eligible to be mined for ``model_id``:
    a dict, tagged with this exact model, and UNSCOPED (no channel/user_id).
    The single source of truth for the mining-eligibility guard so the CLI's
    eligible count can never drift from what :func:`mine_failures` actually
    considers."""
    return (isinstance(r, dict)
            and str(r.get("model_id") or "") == str(model_id)
            and r.get("channel") is None and r.get("user_id") is None)


def count_eligible(reflexions: list[dict], *, model_id: str) -> int:
    """How many of ``reflexions`` are eligible to be mined for ``model_id``.

    A reflexion is eligible only if it is tagged with this model AND unscoped
    (the trace-poisoning guard drops scoped/remote-user failures). The operator
    CLI uses this to explain why scanning N reflexions mined nothing: "scanned
    N" can far exceed the eligible count when failures are scoped or belong to a
    different model, and a bare "no weaknesses" otherwise reads as "this model
    never fails" rather than "those failures were excluded by design"."""
    return sum(1 for r in (reflexions or []) if _is_minable(r, model_id))


# Dimensions a caller may bucket mining by (per-domain etc.), beyond the always-on
# (model, failure_class). Allowlisted so a caller can't bucket by a high-cardinality
# free-text field (e.g. goal_text) and shatter every trace into its own group.
# ``domain`` is the grounded one -- the department pack a run executed as, already
# tracked on the reflexion and used by dreaming for per-department consolidation.
# ``tool`` is the component the failure occurred in -- the tool in play when the
# run failed (the last of ``tools_used``) -- so a weakness can be mined PER TOOL
# and its guidance recalled only when that tool is on hand (#7 component profiles).
# ``role`` is the agent role in play at failure (stamped on the reflexion), so a
# weakness can be mined PER ROLE and its guidance recalled only into that role's
# prompts -- an orchestrator lesson stops taxing worker prompts of the same model.
_BUCKET_DIMS = ("domain", "tool", "role")


def _bucket_value(r: dict, dim: str) -> str:
    """The scalar bucket value for one reflexion on one allowlisted ``dim``.

    Most dims are plain scalar fields (``domain``). ``tool`` is DERIVED: the trace
    carries a ``tools_used`` LIST, and the component a failure belongs to is the
    tool in play when it failed -- the LAST entry. Missing/empty -> "" so untagged
    traces cluster together rather than each forming a singleton bucket."""
    if dim == "tool":
        used = r.get("tools_used") or []
        return str(used[-1]) if isinstance(used, (list, tuple)) and used else ""
    v = r.get(dim)
    return "" if v is None else str(v)


def _bucket_key(r: dict, bucket_by: tuple[str, ...]) -> tuple[str, ...]:
    """The extra group key for one reflexion under ``bucket_by`` (a tuple of
    allowlisted reflexion dims). Missing/None -> "" so untagged traces still
    cluster together rather than each forming a singleton bucket."""
    return tuple(_bucket_value(r, k) for k in bucket_by)


def _context_str(bucket_by: tuple[str, ...], values: tuple[str, ...]) -> str:
    """Human "domain=finance" scope string from a bucket key (empty parts dropped)."""
    return ", ".join(f"{k}={v}" for k, v in zip(bucket_by, values, strict=False) if v)


def mine_failures(
    reflexions: list[dict], *, model_id: str, min_support: int = 3,
    similarity: float = 0.3, min_support_by_class: dict[str, int] | None = None,
    similarity_fn: Callable[[str, str], float] | None = None,
    bucket_by: tuple[str, ...] = (),
) -> list[FailureSignature]:
    """Cluster ONE model's failure traces into recurring signatures.

    Model-specific by design (the paper's key lever): only traces whose
    ``model_id`` matches are considered, so a weakness mined for one model never
    leaks into another's harness. Within a model, traces are grouped by
    ``failure_class`` then greedily clustered by goal-text overlap; only clusters
    with ``>= min_support`` members survive (a one-off is noise, not a pattern).
    ``min_support < 1`` disables mining (returns nothing).

    ``similarity_fn`` is the SEMANTIC-MINING seam: an injected
    ``(text_a, text_b) -> [0,1]`` similarity (e.g. embedding cosine) used in place
    of the default token-Jaccard, so failures that mean the same thing in
    different words cluster together. It must be DETERMINISTIC for the loop's
    determinism guarantee to hold; :func:`semantic_similarity` is the built-in
    deterministic default the ``semantic_mining`` flag selects. A raising/garbage
    fn degrades to "no match" (a fresh cluster), never crashing the pass.

    ``bucket_by`` mines at a FINER granularity than model-wide: an allowlisted
    tuple of scalar reflexion fields (``("domain",)``) sub-groups failures so a
    department-specific weakness yields a *department-scoped* signature (its
    ``context`` records the bucket and the proposed guidance is scoped to it).
    Unknown/high-cardinality dims are rejected by the caller (see ``_BUCKET_DIMS``);
    the always-on (model, failure_class) grouping is unchanged when empty.

    SCOPE GUARD (trace-poisoning defense): only UNSCOPED failures -- ones with
    no ``channel`` and no ``user_id`` -- are mined. A scoped reflexion came from
    a remote user on some channel and may carry attacker-influenced goal/failure
    text; the addendum is recalled into EVERY future run of this model (across
    all channels/tenants), so admitting scoped text would let a hostile caller
    poison the harness cross-channel. This mirrors dreaming's unscoped-only
    promotion guard. A purely-local operator's runs are unscoped, so this costs
    nothing in the intended single-operator case."""
    if min_support < 1:
        return []
    mine = [r for r in (reflexions or []) if _is_minable(r, model_id)]
    # DoS backstop: greedy clustering is O(n*clusters) -- worst case O(n^2) when
    # every trace is a distinct goal. The runner already feeds only the most
    # recent ``limit`` (500) traces, but mine_failures is public; a direct caller
    # passing a huge list would otherwise hang the pass. Cap at a generous bound
    # (well above the runner's 500) and keep the most RECENT slice, so a runaway
    # input is bounded without affecting any realistic call. (Found by the
    # algorithmic-complexity battery.)
    if len(mine) > _MAX_MINE_TRACES:
        log.warning("self_harness: mining %d traces capped to %d (most recent)",
                    len(mine), _MAX_MINE_TRACES)
        mine = mine[-_MAX_MINE_TRACES:]
    # Canonicalize order so mining is DETERMINISTIC and permutation-invariant:
    # the greedy clustering and the by-class grouping both depend on iteration
    # order, so the same failures in a different log order would otherwise mine
    # different weaknesses -- non-reproducible learning from identical evidence.
    mine.sort(key=lambda r: (str(r.get("failure_class") or "error"),
                             str(r.get("goal_text") or ""),
                             str(r.get("failure_msg") or "")))
    # Group by (failure_class, bucket-key). With the default empty ``bucket_by``
    # the bucket-key is ``()`` for every record -> one group per class, identical
    # to the historical by-class grouping (the determinism proof relies on this).
    bucket_by = tuple(bucket_by or ())
    by_group: dict[tuple[str, tuple[str, ...]], list[dict]] = {}
    for r in mine:
        key = (str(r.get("failure_class") or "error"), _bucket_key(r, bucket_by))
        by_group.setdefault(key, []).append(r)

    out: list[FailureSignature] = []
    # sorted() so group iteration order is deterministic regardless of dict
    # insertion order (matters once a class splits into several buckets).
    for (fclass, bucket_vals), recs in sorted(by_group.items()):
        context = _context_str(bucket_by, bucket_vals)
        # Adaptive support: some failure classes warrant a lower (or higher)
        # evidence bar -- two identical auth failures against the same connector
        # are plenty; two unrelated timeouts may not be. Per-class override falls
        # back to the global floor; clamped to >=1 so it can never admit a one-off.
        eff_support = min_support
        if min_support_by_class and fclass in min_support_by_class:
            try:
                eff_support = max(1, int(min_support_by_class[fclass]))
            except (TypeError, ValueError):
                eff_support = min_support
        clusters: list[list[dict]] = []
        # Cache each cluster HEAD's token set (parallel to ``clusters``). The
        # head's goal text never changes, so re-tokenizing it on every
        # comparison -- as a naive ``_tokens(cluster[0][...])`` in the inner loop
        # does -- is pure waste: it turned the greedy pass into a tokenize-bound
        # O(n^2) that took ~69s on 8k traces. Tokenize each head once instead.
        # Behaviour is identical: ``heads[i]`` is the token set of ``clusters[i]``'s
        # first member, exactly what the old code recomputed. (Found by the
        # algorithmic-complexity battery.)
        heads: list[set[str]] = []
        head_texts: list[str] = []      # parallel head goal-texts for similarity_fn
        builtin_semantic = similarity_fn is semantic_similarity
        head_semantic: list[tuple[set[str], set[str]]] = []
        for r in recs:
            rtext = str(r.get("goal_text", ""))
            rsem = _semantic_features(rtext) if builtin_semantic else None
            rt = rsem[0] if rsem is not None else _tokens(rtext)
            for i, htok in enumerate(heads):
                if builtin_semantic and rsem is not None:
                    sim = _semantic_similarity_from_features(head_semantic[i], rsem)
                elif similarity_fn is not None:
                    try:
                        sim = float(similarity_fn(head_texts[i], rtext))
                    except Exception:  # a bad seam can't crash mining -> no match
                        sim = 0.0
                else:
                    sim = _jaccard(rt, htok)
                if sim >= similarity:
                    clusters[i].append(r)
                    break
            else:
                clusters.append([r])
                heads.append(rt)
                head_texts.append(rtext)
                if builtin_semantic and rsem is not None:
                    head_semantic.append(rsem)
        for cluster in clusters:
            if len(cluster) < eff_support:
                continue
            examples = tuple(
                dict.fromkeys(  # de-dupe, preserve order
                    str(c.get("goal_text", "")).strip().splitlines()[0][:160]
                    for c in cluster if str(c.get("goal_text", "")).strip()
                )
            )[:3]
            sig_text = _summarize_signature(cluster)
            # Fold the bucket scope into the signature TEXT too (when present) so
            # it shows up in `show`, the audit, and conflict detection for free,
            # and so two domains' same-class clusters are distinct signatures.
            if context:
                sig_text = f"{sig_text} [{context}]"
            out.append(FailureSignature(
                model_id=str(model_id), failure_class=fclass,
                signature=sig_text, support=len(cluster), examples=examples,
                context=context,
            ))
    # Strongest weaknesses first.
    out.sort(key=lambda s: s.support, reverse=True)
    return out


def _summarize_signature(cluster: list[dict]) -> str:
    """A short, deterministic description of what keeps going wrong."""
    fclass = str(cluster[0].get("failure_class") or "error")
    # Most common short failure message in the cluster, if any.
    msgs = [str(c.get("failure_msg") or "").strip() for c in cluster]
    msgs = [m for m in msgs if m]
    # sorted() before max() so a tie in frequency breaks deterministically
    # (alphabetically) instead of by set iteration order.
    common = max(sorted(set(msgs)), key=msgs.count) if msgs else ""
    common = common.splitlines()[0][:120] if common else ""
    return f"{fclass}: {common}" if common else fclass


# ---- PROPOSE --------------------------------------------------------------

@dataclass(frozen=True)
class HarnessProposal:
    """A candidate operating-guidance line targeting one failure signature."""
    model_id: str
    signature: str
    addendum_line: str          # the minimal guidance to add
    rationale: str
    hypothesis: str = ""         # optional richer "why this line should help",
                                 # supplied by a structured proposer. METADATA
                                 # ONLY -- it rides into the audit + provenance
                                 # sidecar, never into the recalled prompt.
    context: str = ""            # mining scope carried from the signature (e.g.
                                 # "domain=finance"); routes the line to a
                                 # scoped store key so recall can target it.


# An injected proposer: given a signature, return EITHER a single short guidance
# line (str) OR a structured mapping ``{"line": str, "hypothesis"?: str}`` -- a
# richer proposer (e.g. an LLM in the GEPA/RPT shape) can return its reasoning
# alongside the line. The model proposes how to avoid its OWN recurring failure.
# Pure/seam.
ProposeFn = Callable[[FailureSignature], "str | Mapping[str, str]"]


def _proposal_parts(raw: object) -> tuple[str, str]:
    """Split a proposer's return into ``(line, hypothesis)``. A bare string is
    the line with no hypothesis; a mapping may carry ``line`` (or the legacy
    ``addendum_line``) plus an optional ``hypothesis``. Anything else is empty --
    a malformed return degrades to "no proposal", it never crashes the loop."""
    if isinstance(raw, Mapping):
        line = raw.get("line") or raw.get("addendum_line") or ""
        hypothesis = raw.get("hypothesis") or ""
        return (str(line), str(hypothesis))
    return (str(raw or ""), "")

def _sanitize_line(text: str) -> str:
    """Neutralize a proposed addendum line before it can enter a prompt.

    Defense-in-depth on top of the unscoped-only mining guard: the line still
    derives from trace text and (with an LLM proposer) from model output, so
    strip control chars, collapse all whitespace to single spaces (no multi-line
    break-out), and scrub secrets. The result is one bounded plain-prose line.

    Stripping is by UNICODE CATEGORY, not an ASCII regex: every control char
    (category ``Cc`` -- C0, the C1 0x80-0x9f block, and DEL) and every format
    char (``Cf`` -- zero-width ZWSP/ZWNJ/ZWJ, the BOM, and bidi overrides like
    RLO/LRO/isolates) is replaced with a space. An ASCII-only ``[\\x00-\\x1f\\x7f]``
    pass let the whole non-ASCII slice through, and this line lands in EVERY
    system prompt for the model AND in the signed audit + addenda.json a human
    reviews: a C1 byte injects a terminal escape when that log is cat'd, a
    zero-width char splits a trigger word past a downstream filter, and an RLO
    visually reverses the guidance an auditor reads. Zl/Zp/Zs separators are
    Unicode whitespace, so ``split()`` below already collapses them. (Found by
    the adversarial input-fuzzing battery.)"""
    raw = str(text or "")
    cleaned = "".join(
        " " if unicodedata.category(c) in {"Cc", "Cf"} else c for c in raw)
    line = " ".join(cleaned.split())  # collapse all whitespace incl. newlines
    try:
        from .secrets import scrub
        line = scrub(line)
    except Exception:  # pragma: no cover -- scrubbing must never break the loop
        pass
    return line.strip()


# Semantic policy-erosion screen for the proposed LINE itself. `_sanitize_line`
# defends SYNTAX (control chars, secrets, multi-line) and the gate enforces
# capability non-escalation on the CANDIDATE; neither reads the MEANING of the
# prose that will ride in every future prompt for this model. A line that tells
# the model to disable/bypass/ignore its own safety machinery is exactly the
# poisoning a trace-influenced (or buggy) proposer could smuggle as plain prose.
# Matches an erosion VERB near a safety-control NOUN within a clause. Deliberately
# conservative: it refuses even negated mentions ("don't skip validation") because
# the lesson should be stated POSITIVELY ("always run validation") -- a line that
# talks about ignoring safety at all is a smell in always-on guidance. The
# class-grounded fallback lines use only positive verbs (verify/check/validate/
# avoid/refresh), so they never trip it. (Threat raised by an external review.)
_POLICY_EROSION_RE = re.compile(
    r"\b(?:ignore|bypass|disable|circumvent|evade|override|overrule|suppress|"
    r"skip|conceal|hide|turn\s+off|opt\s+out\s+of)\b"
    r"[\w\s,'\"()-]{0,40}?\b(?:safety|shield|guard\s?rails?|guardrails?|"
    r"validations?|validator|verifier|verification|polic(?:y|ies)|approvals?|"
    r"consent|authentication|authorization|auth|credentials?|permissions?|"
    r"sandbox|audit|budget|spending|uncertainty|warnings?)\b",
    re.IGNORECASE)


def _erodes_policy(line: str) -> bool:
    """Whether a proposed guidance line tells the model to weaken its own safety
    machinery (disable/bypass/ignore validation, auth, budget, sandbox, audit,
    ...). Refused in :func:`propose_addendum` as defense-in-depth."""
    return bool(_POLICY_EROSION_RE.search(line or ""))


def _screen_addendum_line(line: str) -> str | None:
    """Apply the prompt-addendum safety screen to an already-proposed line.

    Transfer can source legacy/tampered addenda that predate the current
    proposal guard, so every write path must re-run the same syntax and semantic
    checks before a line can land in another model's recalled prompt.
    """
    line = _sanitize_line(line)
    if not line or len(line) > 280:
        return None
    if _erodes_policy(line):
        log.warning("self_harness: refused policy-eroding addendum line: %r", line)
        return None
    return line


# Failure-class-grounded guidance for the deterministic proposer. arXiv
# 2603.23994 warns the STARTING ARTIFACT bounds what the loop can ever learn, so
# a generic "slow down and verify" line is a weak seed. These per-class lines
# are specific AND -- unlike the generic fallback -- do NOT embed the
# trace-derived signature text into the prompt, so they need no sanitization.
_CLASS_GUIDANCE: dict[str, str] = {
    "timeout": "this kind of goal has timed out before; budget the work, prefer "
               "incremental/streaming steps, and check for long-running "
               "operations before you start.",
    "auth": "this kind of goal has failed on authentication before; verify "
            "credentials and token freshness (refresh if near expiry) before the call.",
    "parse": "this kind of goal has failed on parsing before; validate the "
             "response shape before parsing and handle malformed or partial data.",
    "tool_error": "this kind of goal has hit a tool error before; check the "
                  "tool's preconditions and arguments and read its error output "
                  "before retrying.",
    "shield": "this kind of goal has been blocked by the safety shield before; "
              "stay within policy and avoid the action that tripped it.",
    "max_steps": "this kind of goal has run out of steps before; plan the fewest "
                 "steps to the result and avoid exploratory detours.",
    "budget": "this kind of goal has exhausted its budget before; do the cheapest "
              "sufficient work first and avoid redundant tool calls.",
    "agent_error": "this kind of goal has failed mid-run before; re-read the "
                   "error, confirm preconditions, and take the smallest safe "
                   "next step rather than retrying blindly.",
}


def _default_propose(sig: FailureSignature) -> str:
    """Deterministic fallback proposer (no LLM): a failure-class-grounded
    guidance line, templated so the loop runs and is testable without a
    provider. Specific by class; falls back to a generic (sanitized) line for an
    unknown class. A bucket ``context`` (e.g. domain-scoped mining) is folded in
    so the guidance names the scope it was learned in."""
    # ``sig.context`` is "domain=finance"-style (derived from allowlisted scalar
    # fields), so it carries no trace-derived free text into the prompt.
    scope = f" (seen in {sig.context})" if sig.context else ""
    specific = _CLASS_GUIDANCE.get(sig.failure_class)
    if specific:
        return f"When a goal resembles your past {sig.failure_class} failures{scope}, {specific}"
    return (f"When a goal resembles your past {sig.failure_class} failures{scope} "
            f"({sig.signature}), slow down and verify the precondition that "
            f"tripped you before acting.")


def _parse_structured_proposal(text: str) -> dict | None:
    """Best-effort parse of a structured proposer's JSON ``{"line","hypothesis"}``.
    Tolerates a fenced ```json block or surrounding prose by extracting the first
    balanced ``{...}`` slice. Returns ``None`` if nothing parseable with a string
    ``line`` is found -- the caller then falls back to plain-line extraction, so a
    model that ignores the format still works."""
    s = str(text or "")
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j <= i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    line = obj.get("line") or obj.get("addendum_line") or ""
    hyp = obj.get("hypothesis") or ""
    if not isinstance(line, str) or not line.strip():
        return None
    return {"line": line, "hypothesis": hyp if isinstance(hyp, str) else ""}


def llm_proposer(llm, *, budget=None, model: str | None = None,
                 max_tokens: int = 240, structured: bool = True) -> ProposeFn:
    """Build a REFLECTIVE proposer backed by an LLM -- the GEPA/RPT shape
    (arXiv 2507.19457, 2605.21781): read the mined failure signature + example
    goals and write ONE minimal operating-guidance line. Injected exactly like
    any other ``propose_fn``; the model is NOT hard-coded (kernel rule 2) --
    pass ``model`` or let the ``llm``'s own resolved model stand.

    With ``structured`` (default) the proposer asks for a small JSON object
    ``{"line", "hypothesis"}`` so the model's REASONING is captured alongside the
    line (it rides into the audit + provenance sidecar, never the prompt). The
    parse is best-effort: a model that returns a bare line instead still works
    (the text becomes the line), and any provider/parse error falls OPEN to
    :func:`_default_propose`, so a flaky or off-format model never blocks a pass.
    Whichever shape comes back, the line still flows through ``propose_addendum``'s
    ``_sanitize_line`` + length + policy gates, so an attacker-influenced model
    cannot smuggle control chars, secrets, or multi-line break-outs."""
    def _propose(sig: FailureSignature):
        try:
            if structured:
                system = (
                    "You tune a coding agent's operating guidance. Given a "
                    "recurring failure pattern for ONE model, return a JSON object "
                    'with two string fields: "line" -- a single short imperative '
                    "guidance line (<=200 characters, no markdown, no newlines) "
                    "that, added to that model's system prompt, would help it "
                    'avoid this class of failure; and "hypothesis" -- one sentence '
                    "on why that line should help. Be specific and minimal. Return "
                    "only the JSON object.")
            else:
                system = (
                    "You tune a coding agent's operating guidance. Given a recurring "
                    "failure pattern for ONE model, write a single short imperative "
                    "guidance line (<=200 characters, no preamble, no markdown, no "
                    "newlines) that, added to that model's system prompt, would help "
                    "it avoid this class of failure. Be specific and minimal.")
            examples = "\n".join(f"- {e}" for e in sig.examples[:3]) or "(none)"
            user = (f"Model: {sig.model_id}\nFailure class: {sig.failure_class}\n"
                    f"Signature: {sig.signature}\nExample goals that failed:\n"
                    f"{examples}\n\n" + ("JSON:" if structured else "Guidance line:"))
            resp = llm.complete(system, [{"role": "user", "content": user}],
                                budget=budget, max_tokens=max_tokens, model=model)
            text = (getattr(resp, "text", "") or "")
            if structured:
                parsed = _parse_structured_proposal(text)
                if parsed:
                    return parsed
            # Plain-line extraction -- also the fallback when JSON parsing failed.
            line = next((s.strip() for s in text.splitlines() if s.strip()), "")
            line = line.lstrip("-*•> ").strip().strip('"').strip()
            if line:
                return line
        except Exception as e:  # pragma: no cover -- fail open to deterministic
            log.warning("self_harness: llm proposer failed (%s); using fallback", e)
        return _default_propose(sig)
    return _propose


def llm_conflict_classifier(llm, *, budget=None, model: str | None = None,
                            max_tokens: int = 8) -> Callable[[str, str], bool]:
    """An LLM-backed ``classifier_fn`` for :func:`find_conflicts` /
    :func:`detect_store_conflicts`: judge by MEANING whether two guidance lines
    contradict each other -- catching reworded conflicts the lexical heuristic
    misses and suppressing its false positives. The model is NOT hard-coded
    (kernel rule 2); the CLI wires the verifier role.

    Deliberately RAISES on a provider error or an unparseable verdict:
    ``find_conflicts`` treats a raising classifier as "fall back to the lexical
    heuristic for this pair", so a flaky judge never drops a real flag."""
    def _classify(a: str, b: str) -> bool:
        resp = llm.complete(
            "Two lines of operating guidance for the same agent follow. Answer "
            "ONLY 'yes' or 'no': do they CONTRADICT each other (following both "
            "at once would be impossible or incoherent)?",
            [{"role": "user", "content": f"A: {a}\nB: {b}\n\nyes/no:"}],
            budget=budget, max_tokens=max_tokens, model=model)
        text = (getattr(resp, "text", "") or "").strip().lower()
        if text.startswith("yes"):
            return True
        if text.startswith("no"):
            return False
        raise ValueError(f"unparseable conflict verdict: {text[:40]!r}")
    return _classify


def propose_addendum(sig: FailureSignature, *, propose_fn: ProposeFn | None = None,
                     ) -> HarnessProposal | None:
    """Produce a MINIMAL guidance line for ``sig`` via the injected proposer
    (or the deterministic fallback). The proposer may return a bare line or a
    structured ``{"line", "hypothesis"?}`` mapping -- the hypothesis (a richer
    'why this should help') is carried as METADATA into the audit + provenance,
    never into the prompt. Returns ``None`` if the proposal is empty or too long
    to be 'minimal'."""
    check_learning_halt("self_harness", "proposal")
    fn = propose_fn or _default_propose
    try:
        line, hypothesis = _proposal_parts(fn(sig))
    except Halted:
        raise
    except Exception as e:  # pragma: no cover -- a bad proposer can't crash the loop
        log.warning("self_harness: proposer failed for %s (%s)", sig.signature, e)
        return None
    line = _screen_addendum_line(line)
    if line is None:
        return None
    # The hypothesis rides into the signed audit + the provenance sidecar (a human
    # reads both), so it gets the SAME syntactic scrub as the line -- control
    # chars / secrets / multi-line stripped -- plus a length bound. It is NOT
    # policy-screened: it never enters a prompt, it only annotates WHY a line was
    # learned, so the positive-phrasing rule the prompt needs doesn't apply.
    hypothesis = _sanitize_line(hypothesis)[:280]
    return HarnessProposal(
        model_id=sig.model_id, signature=sig.signature,
        addendum_line=line, hypothesis=hypothesis, context=sig.context,
        rationale=f"targets {sig.support} '{sig.failure_class}' failures",
    )


# ---- VALIDATE -------------------------------------------------------------

@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    held_in_delta: float
    held_out_delta: float
    reason: str
    # Raw held-out A/B (the unseen-split generalization signal) + case count, so
    # the gate's evidence check judges the honest baseline-vs-candidate numbers.
    baseline_score: float = 0.0
    candidate_score: float = 0.0
    samples: int = 0
    # Honest split denominators after excluding nothing: production structured
    # scorers fail closed unless every requested arm/case completed. ``samples``
    # is the promotion-evidence denominator (held-out when present), never the
    # held-in + held-out total.
    held_in_samples: int = 0
    held_out_samples: int = 0
    # Conservative lower confidence bound on candidate - baseline.  Passing this
    # into the shared promotion gate prevents a point-estimate win from being
    # reinterpreted there as causal evidence.
    effect_ci_low: float | None = None


# A scorer returns either a plain success rate in [0,1], OR a mapping
# ``{"success": rate, "cost"?: float, "latency"?: float, "tool_calls"?: float}``
# so validation can also gate on OPERATIONAL regressions (a line can raise the
# pass rate while making the model slower / pricier / chattier). Injected: a
# live A/B needs a real model, exactly like learning_rollout's constraints.
ScoreFn = Callable[[str, list[str]], object]
HoldoutAuthorize = Callable[[str, str, list[str]], float]


def _wilson_lower_bound(successes: float, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for a binomial proportion --
    the statistically-honest "how good is this, conservatively?" given ``n``
    observations. A 1-of-1 win has a low bound; a 50-of-50 win a high one."""
    if n <= 0:
        return 0.0
    phat = max(0.0, min(1.0, successes / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _wilson_upper_bound(successes: float, n: int, z: float = 1.96) -> float:
    """Upper endpoint paired with :func:`_wilson_lower_bound`."""
    if n <= 0:
        return 1.0
    phat = max(0.0, min(1.0, successes / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return min(1.0, (centre + margin) / denom)


def _difference_ci_lower(candidate_rate: float, baseline_rate: float,
                         candidate_n: int, baseline_n: int,
                         z: float = 1.96) -> float:
    """Newcombe score lower bound for a difference of two proportions.

    The former gate compared the candidate's Wilson lower bound with the
    baseline *point estimate*.  That ignores baseline uncertainty and can promote
    noise.  Newcombe's score construction combines the candidate lower and
    baseline upper Wilson distances into a bound on the actual uplift.
    """
    if candidate_n <= 0 or baseline_n <= 0:
        return float("-inf")
    c = max(0.0, min(1.0, float(candidate_rate)))
    b = max(0.0, min(1.0, float(baseline_rate)))
    c_lb = _wilson_lower_bound(round(c * candidate_n), candidate_n, z)
    b_ub = _wilson_upper_bound(round(b * baseline_n), baseline_n, z)
    return (c - b) - math.sqrt((c - c_lb) ** 2 + (b_ub - b) ** 2)


def _binary_leaves(value: object) -> tuple[bool, ...] | None:
    """Flatten one case's replicated binary draws, or reject malformed data."""
    if value in (False, True, 0, 1):
        return (bool(value),)
    if not isinstance(value, (list, tuple)) or not value:
        return None
    leaves: list[bool] = []
    for item in value:
        nested = _binary_leaves(item)
        if nested is None:
            return None
        leaves.extend(nested)
    return tuple(leaves)


def _paired_difference_ci_lower(candidate_outcomes: tuple, baseline_outcomes: tuple,
                                z: float = 1.96) -> float:
    """Conservative lower bound for uplift on matched binary cases.

    The auto evaluator runs both arms on the same tasks.  Treating those arms as
    independent throws away that pairing and reconstructing integer counts from
    rounded aggregate rates can misstate the evidence.  For matched outcomes,
    uplift is ``P(candidate wins, baseline loses) - P(candidate loses,
    baseline wins)``.  Wilson-bound both discordant proportions in the adverse
    directions; using two 95% endpoints gives a conservative simultaneous lower
    bound without pretending repeated draws are independent tasks.
    """
    if not candidate_outcomes or len(candidate_outcomes) != len(baseline_outcomes):
        return float("-inf")
    pairs: list[tuple[bool, bool]] = []
    for candidate, baseline in zip(candidate_outcomes, baseline_outcomes, strict=True):
        candidate_draws = _binary_leaves(candidate)
        baseline_draws = _binary_leaves(baseline)
        if candidate_draws is None or baseline_draws is None:
            return float("-inf")
        # Replicate calls reduce measurement noise but do not mint independent
        # tasks. Collapse each task adversarially for a lower bound: the
        # candidate succeeds only if every draw succeeds, while the baseline
        # succeeds if any draw succeeds.
        pairs.append((all(candidate_draws), any(baseline_draws)))
    improvements = sum(candidate and not baseline for candidate, baseline in pairs)
    regressions = sum(baseline and not candidate for candidate, baseline in pairs)
    n = len(pairs)
    return (_wilson_lower_bound(improvements, n, z)
            - _wilson_upper_bound(regressions, n, z))


def _outcomes_match_rate(outcomes: tuple | None, rate: object) -> bool:
    """Reject structured evidence whose aggregate contradicts its case rows."""
    if outcomes is None:
        return True
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return False
    leaves: list[bool] = []
    for value in outcomes:
        case = _binary_leaves(value)
        if case is None:
            return False
        leaves.extend(case)
    observed = (sum(leaves) / len(leaves)) if leaves else 0.0
    return math.isclose(float(rate), observed, rel_tol=0.0, abs_tol=1e-12)


def _score_parts(v: object) -> tuple:
    """(success, cost, latency, tool_calls) from a scorer result -- a bare rate
    or a mapping. Non-numeric/missing fields are ``None`` and simply not gated."""
    if isinstance(v, Mapping):
        return (v.get("success"), v.get("cost"), v.get("latency"), v.get("tool_calls"))
    return (v, None, None, None)


def _structured_evidence(v: object, fallback: int) -> tuple[int, int, tuple | None, bool]:
    """Return ``(samples, attempted, outcomes, complete)`` for one arm.

    Aggregate third-party scorers retain the historical full-coverage assumption.
    Production corpus scorers return explicit evidence, allowing the validator to
    reject dropped/asymmetric cases instead of fabricating a denominator.
    """
    if not isinstance(v, Mapping) or not any(
            k in v for k in ("samples", "attempted", "outcomes", "complete", "clean")):
        return fallback, fallback, None, True
    samples = v.get("samples", fallback)
    attempted = v.get("attempted", samples)
    outcomes = v.get("outcomes")
    valid_counts = (isinstance(samples, int) and not isinstance(samples, bool)
                    and isinstance(attempted, int) and not isinstance(attempted, bool)
                    and 0 <= samples <= attempted
                    # The validator supplied exactly ``fallback`` cases.  A
                    # structured provider cannot claim completeness over a
                    # smaller private denominator and thereby borrow the
                    # caller's held-out count for the evidence floor.
                    and attempted == fallback)
    if not valid_counts:
        return 0, max(0, fallback), None, False
    if outcomes is not None:
        if not isinstance(outcomes, (list, tuple)) or len(outcomes) != attempted:
            return samples, attempted, None, False
        outcomes = tuple(outcomes)
    complete = bool(v.get("complete", samples == attempted))
    clean = bool(v.get("clean", True))
    return samples, attempted, outcomes, complete and clean and samples == attempted


def _call_score(fn: ScoreFn, add: str, cases: list[str]) -> tuple[object, bool]:
    """Call one arm and snapshot its cleanliness before the next arm overwrites it."""
    check_learning_halt("self_harness", "evaluation")
    value = fn(add, cases)
    clean = bool(getattr(fn, "last_clean", True))
    if isinstance(value, Mapping) and "clean" in value:
        clean = clean and bool(value.get("clean"))
    return value, clean


def _call_ab(
    *, add: str, cases: list[str], score_with: ScoreFn,
    score_without: ScoreFn, candidate_first: bool,
) -> tuple[object, bool, object, bool]:
    """Call both aggregate arms and preserve each call-local clean flag."""
    if candidate_first:
        with_raw, with_clean = _call_score(score_with, add, cases)
        without_raw, without_clean = _call_score(score_without, add, cases)
    else:
        without_raw, without_clean = _call_score(score_without, add, cases)
        with_raw, with_clean = _call_score(score_with, add, cases)
    return with_raw, with_clean, without_raw, without_clean


def _metamorphic_failure(
    *, add: str, held_out: list[str], score_with: ScoreFn,
    score_without: ScoreFn, metamorphic_fn: Callable[[list[str]], list[str]],
    metamorphic_tolerance: float, in_delta: float, out_delta: float,
    result_base: dict, signature: str,
    holdout_authorize: HoldoutAuthorize | None,
) -> ValidationResult | None:
    """Return a rejection when required paraphrase evidence is not robust.

    The transform and both A/B arms are load-bearing once configured: missing,
    partial, asymmetric, dirty, or non-finite evidence is indeterminate.  A
    candidate must retain its original held-out lift within the configured
    absolute tolerance; merely becoming non-harmful on paraphrases is not proof
    that the learned guidance generalized.
    """
    metamorphic_z = 0.0
    if holdout_authorize is not None:
        try:
            # Spend the transformed-view query before the paraphraser sees the
            # sealed source cases. The purpose plus source-view digest and the
            # evaluator epoch bind what is authorized; a failed transform still
            # burns the query, so crash/error paths cannot reveal cases for free.
            metamorphic_z = float(
                holdout_authorize("metamorphic", signature, list(held_out)))
            if not math.isfinite(metamorphic_z) or metamorphic_z <= 0:
                raise ValueError("invalid holdout critical value")
        except Halted:
            raise
        except Exception:
            return ValidationResult(
                False, in_delta, out_delta,
                "sealed holdout authorization failed", **result_base)
    try:
        mcases = [str(c) for c in (metamorphic_fn(list(held_out)) or [])]
    except Halted:
        raise
    except Exception:
        return ValidationResult(False, in_delta, out_delta,
                                _INDETERMINATE_REASON, **result_base)
    source_identities = {_case_identity(source) for source in held_out}
    if (len(mcases) != len(held_out)
            or _case_list_failure("metamorphic", mcases) is not None
            # A permutation of the unchanged holdout is not a metamorphic
            # transformation. Reject collisions against the complete source
            # set, rather than only comparing equal list positions.
            or any(_case_identity(transformed) in source_identities
                   for transformed in mcases)):
        return ValidationResult(False, in_delta, out_delta,
                                _INDETERMINATE_REASON, **result_base)
    try:
        # A sealed evaluation must not always grant the candidate the same
        # provider/cache/warmup position. The main splits use opposite order;
        # choose the transformed view's order independently and reproducibly.
        digest = hashlib.sha256(
            f"{signature}\0metamorphic".encode()).digest()
        m_with_raw, mw_clean, m_without_raw, mo_clean = _call_ab(
            add=add, cases=mcases, score_with=score_with,
            score_without=score_without,
            candidate_first=(holdout_authorize is None or digest[0] % 2 == 0),
        )
    except Halted:
        raise
    except Exception:
        return ValidationResult(False, in_delta, out_delta,
                                _INDETERMINATE_REASON, **result_base)
    m_with, *_ = _score_parts(m_with_raw)
    m_without, *_ = _score_parts(m_without_raw)
    mw_n, mw_attempted, mw_outcomes, mw_complete = _structured_evidence(
        m_with_raw, len(mcases))
    mo_n, mo_attempted, mo_outcomes, mo_complete = _structured_evidence(
        m_without_raw, len(mcases))
    m_structured = [isinstance(v, Mapping) and any(
        k in v for k in ("samples", "attempted", "outcomes", "complete", "clean"))
        for v in (m_with_raw, m_without_raw)]
    m_paired = mw_attempted == mo_attempted and mw_n == mo_n
    if mw_outcomes is not None or mo_outcomes is not None:
        m_paired = m_paired and mw_outcomes is not None and mo_outcomes is not None
    m_valid = (
        mw_clean and mo_clean and mw_complete and mo_complete and m_paired
        and (not any(m_structured) or all(m_structured))
        and _outcomes_match_rate(mw_outcomes, m_with)
        and _outcomes_match_rate(mo_outcomes, m_without)
        and all(isinstance(s, (int, float)) and not isinstance(s, bool)
                and math.isfinite(s) and 0.0 <= s <= 1.0
                for s in (m_with, m_without))
    )
    if not m_valid:
        return ValidationResult(False, in_delta, out_delta,
                                _INDETERMINATE_REASON, **result_base)
    m_delta = m_with - m_without
    if metamorphic_z > 0:
        if mw_outcomes is not None and mo_outcomes is not None:
            m_ci_low = _paired_difference_ci_lower(
                mw_outcomes, mo_outcomes, metamorphic_z)
        else:
            m_ci_low = _difference_ci_lower(
                m_with, m_without, mw_n, mo_n, metamorphic_z)
        if m_ci_low <= 0:
            return ValidationResult(
                False, in_delta, out_delta,
                ("metamorphic improvement not confident "
                 f"(effect CI lower {m_ci_low:.3g} <= 0)"),
                **result_base)
    required = out_delta - metamorphic_tolerance
    if m_delta < required:
        return ValidationResult(
            False, in_delta, out_delta,
            f"failed metamorphic check (overfit to wording; paraphrase delta "
            f"{m_delta:.3g} < required {required:.3g})", **result_base)
    return None


# The rejection reason for an evaluation that never actually judged the line
# (budget-dead arm, broken scorer). A sentinel constant so callers that must
# distinguish "judged on the merits" from "indeterminate" do not string-match
# prose.
_INDETERMINATE_REASON = "scorer returned a non-finite or out-of-range value"


def _valid_validation_policy(
    *, min_delta: object, min_held_out: object, confidence_z: object,
    max_cost_factor: object, max_latency_factor: object,
    max_tool_calls_factor: object, metamorphic_tolerance: object,
) -> bool:
    """Reject non-finite policy values that would defeat numeric comparisons."""
    if (not isinstance(min_held_out, int) or isinstance(min_held_out, bool)
            or min_held_out < 0):
        return False
    required = (min_delta, confidence_z, metamorphic_tolerance)
    optional = (max_cost_factor, max_latency_factor, max_tool_calls_factor)

    def _nonnegative_finite(value: object) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and float(value) >= 0.0)

    return (all(_nonnegative_finite(value) for value in required)
            and all(value is None or _nonnegative_finite(value)
                    for value in optional))


def _case_identity(case: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", case).split()).casefold()


def _case_list_failure(label: str, cases: object) -> str | None:
    if not isinstance(cases, list) or any(
            not isinstance(case, str) or not case.strip()
            or len(case) > 1_048_576 or "\x00" in case
            for case in cases):
        return f"{label} cases are malformed or duplicated"
    identities = [_case_identity(case) for case in cases]
    if len(set(identities)) != len(identities):
        return f"{label} cases are malformed or duplicated"
    return None


def _operational_regression_reason(
    factors_and_pairs: tuple[tuple[str, float | None, tuple[object, object]], ...],
) -> str | None:
    """Validate configured resource evidence and return the first regression."""
    for label, factor, (candidate, baseline) in factors_and_pairs:
        if factor is None:
            continue
        valid = (
            isinstance(candidate, (int, float)) and not isinstance(candidate, bool)
            and math.isfinite(candidate) and candidate >= 0
            and isinstance(baseline, (int, float)) and not isinstance(baseline, bool)
            and math.isfinite(baseline) and baseline >= 0
        )
        if not valid:
            return f"{label} evidence missing or invalid on held-out"
        if candidate > baseline * factor:
            return (f"{label} regressed on held-out "
                    f"({candidate:.3g} > {baseline:.3g} x {factor})")
    return None


def validate_proposal(  # noqa: C901 - gate remains one fail-closed evidence boundary
    proposal: HarnessProposal, *, held_in: list[str], held_out: list[str],
    score_with: ScoreFn, score_without: ScoreFn,
    min_delta: float = 0.0, min_held_out: int = 0, confidence_z: float = 0.0,
    max_cost_factor: float | None = None, max_latency_factor: float | None = None,
    max_tool_calls_factor: float | None = None,
    metamorphic_fn: Callable[[list[str]], list[str]] | None = None,
    metamorphic_tolerance: float = 0.0,
    holdout_authorize: HoldoutAuthorize | None = None,
) -> ValidationResult:
    """The Self-Harness acceptance test: a harness edit must not regress EITHER
    split and must help at least one (reject pure trades).

    ``held_in`` are the mined cases the edit was written against; ``held_out``
    are unseen cases that guard against overfitting the edit to its own examples
    (the paper's central failure mode). ``score_with``/``score_without`` return
    a success rate (or a mapping with ``success`` + optional ``cost``/
    ``latency``/``tool_calls``) for the prompt WITH vs WITHOUT the candidate line.

    The low-level API keeps neutral defaults for compatibility; the production
    risk-limited profile supplies non-weakenable floors. ``min_delta``
    (effect-size floor) and ``min_held_out`` (unseen-sample
    floor) -- a sub-threshold lift or a 1-of-1 held-out win is not durable
    evidence. ``confidence_z`` (>0) requires the candidate's held-out Wilson lower
    bound to clear the baseline rate -- statistically-confident generalization,
    not luck. ``max_*_factor`` reject a candidate that regresses cost/latency/
    tool-calls on the held-out split beyond the factor (a line in every prompt
    must not quietly make the model slower or pricier). ``metamorphic_fn`` (a
    meaning-preserving paraphraser; injected because a real one needs a model)
    rejects a line whose held-out improvement does NOT survive paraphrasing the
    unseen cases -- catching a line overfit to surface wording rather than the
    underlying weakness (``metamorphic_tolerance`` allows a small slip)."""
    if not _valid_validation_policy(
            min_delta=min_delta, min_held_out=min_held_out,
            confidence_z=confidence_z, max_cost_factor=max_cost_factor,
            max_latency_factor=max_latency_factor,
            max_tool_calls_factor=max_tool_calls_factor,
            metamorphic_tolerance=metamorphic_tolerance):
        return ValidationResult(
            False, 0.0, 0.0, "invalid validation policy",
            baseline_score=0.0, candidate_score=0.0, samples=0,
            held_in_samples=0, held_out_samples=0)
    case_failure = (
        _case_list_failure("held-in", held_in)
        or _case_list_failure("held-out", held_out)
    )
    if (case_failure is None
            and {_case_identity(case) for case in held_in}
            & {_case_identity(case) for case in held_out}):
        case_failure = "held-in and held-out cases overlap"
    if case_failure is not None:
        return ValidationResult(
            False, 0.0, 0.0, case_failure,
            baseline_score=0.0, candidate_score=0.0, samples=0,
            held_in_samples=0, held_out_samples=0)
    add = proposal.addendum_line
    # Spend/record authorization before any scorer sees a sealed confirmation
    # case. A missing/corrupt/exhausted ledger therefore cannot leak results and
    # then retroactively decide whether the access counted.
    if held_out and holdout_authorize is not None:
        try:
            permit_z = float(holdout_authorize(
                "confirmation", proposal.signature, list(held_out)))
            if not math.isfinite(permit_z) or permit_z <= 0:
                raise ValueError("invalid holdout critical value")
            confidence_z = max(confidence_z, permit_z)
        except Halted:
            raise
        except Exception:
            return ValidationResult(
                False, 0.0, 0.0, "sealed holdout authorization failed",
                baseline_score=0.0, candidate_score=0.0, samples=0,
                held_in_samples=0, held_out_samples=0)
    # Snapshot ``last_clean`` immediately after each call; the next call on the
    # same scorer overwrites it. Sealed paths deterministically counterbalance
    # the aggregate arm order across development and confirmation so the
    # candidate is not systematically favored by warmup/cache/provider drift.
    # Legacy injected scorers retain their historical call order.
    sealed_ab = holdout_authorize is not None
    order_digest = hashlib.sha256(
        f"{proposal.signature}\0ab-order".encode()).digest()
    in_candidate_first = not sealed_ab or order_digest[0] % 2 == 0
    try:
        in_with_raw, iw_clean, in_without_raw, io_clean = _call_ab(
            add=add, cases=held_in, score_with=score_with,
            score_without=score_without, candidate_first=in_candidate_first)
        out_with_raw, ow_clean, out_without_raw, oo_clean = _call_ab(
            add=add, cases=held_out, score_with=score_with,
            score_without=score_without,
            candidate_first=(not in_candidate_first if sealed_ab else True))
    except Halted:
        raise
    except Exception:
        return ValidationResult(
            False, 0.0, 0.0, _INDETERMINATE_REASON,
            baseline_score=0.0, candidate_score=0.0, samples=0,
            held_in_samples=0, held_out_samples=0)
    in_with, in_ci, in_li, in_ti = _score_parts(in_with_raw)
    in_without, iwo_cost, iwo_lat, iwo_tc = _score_parts(in_without_raw)
    out_with, out_cost, out_lat, out_tc = _score_parts(out_with_raw)
    out_without, owo_cost, owo_lat, owo_tc = _score_parts(out_without_raw)

    iw_n, iw_attempted, iw_outcomes, iw_complete = _structured_evidence(
        in_with_raw, len(held_in))
    io_n, io_attempted, io_outcomes, io_complete = _structured_evidence(
        in_without_raw, len(held_in))
    ow_n, ow_attempted, ow_outcomes, ow_complete = _structured_evidence(
        out_with_raw, len(held_out))
    oo_n, oo_attempted, oo_outcomes, oo_complete = _structured_evidence(
        out_without_raw, len(held_out))
    structured_flags = [isinstance(v, Mapping) and any(
        k in v for k in ("samples", "attempted", "outcomes", "complete", "clean"))
        for v in (in_with_raw, in_without_raw, out_with_raw, out_without_raw)]
    evidence_clean = all((iw_clean, io_clean, ow_clean, oo_clean,
                          iw_complete, io_complete, ow_complete, oo_complete))
    evidence_paired = (iw_attempted == io_attempted and ow_attempted == oo_attempted)
    # One structured arm beside one opaque arm cannot prove matched coverage.
    evidence_symmetric = not any(structured_flags) or all(structured_flags)
    if iw_outcomes is not None or io_outcomes is not None:
        evidence_paired = evidence_paired and iw_outcomes is not None and io_outcomes is not None
    if ow_outcomes is not None or oo_outcomes is not None:
        evidence_paired = evidence_paired and ow_outcomes is not None and oo_outcomes is not None
    outcome_rates_match = all((
        _outcomes_match_rate(iw_outcomes, in_with),
        _outcomes_match_rate(io_outcomes, in_without),
        _outcomes_match_rate(ow_outcomes, out_with),
        _outcomes_match_rate(oo_outcomes, out_without),
    ))
    held_in_n = min(iw_n, io_n)
    held_out_n = min(ow_n, oo_n)
    # A score is a success RATE in [0,1]. A buggy/hostile scorer returning a
    # non-finite (NaN/inf) or out-of-range value must NOT drive a promotion: an
    # inf candidate would otherwise sail past the gate's "candidate >= baseline"
    # evidence check. Reject up front. (Found by the 1000-round fuzz campaign.)
    scores = (in_with, in_without, out_with, out_without)
    if (not evidence_clean or not evidence_paired or not evidence_symmetric
            or not outcome_rates_match
            or not all(isinstance(s, (int, float)) and not isinstance(s, bool)
                       and math.isfinite(s)
                       and 0.0 <= s <= 1.0 for s in scores)):
        return ValidationResult(False, 0.0, 0.0, _INDETERMINATE_REASON,
                                baseline_score=0.0, candidate_score=0.0, samples=0,
                                held_in_samples=held_in_n,
                                held_out_samples=held_out_n)
    in_delta, out_delta = in_with - in_without, out_with - out_without
    # The gate sees ONLY unseen confirmation evidence when it exists.  Held-in
    # examples guide proposal search but can never inflate the promotion floor.
    if held_out:
        ev_base, ev_cand, ev_n = out_without, out_with, held_out_n
        ev_cand_outcomes, ev_base_outcomes = ow_outcomes, oo_outcomes
        ev_costs = (out_cost, owo_cost)
        ev_lats = (out_lat, owo_lat)
        ev_tools = (out_tc, owo_tc)
    else:
        ev_base, ev_cand, ev_n = in_without, in_with, held_in_n
        ev_cand_outcomes, ev_base_outcomes = iw_outcomes, io_outcomes
        ev_costs = (in_ci, iwo_cost)
        ev_lats = (in_li, iwo_lat)
        ev_tools = (in_ti, iwo_tc)
    base = dict(baseline_score=ev_base, candidate_score=ev_cand, samples=ev_n,
                held_in_samples=held_in_n, held_out_samples=held_out_n,
                effect_ci_low=None)
    # Unseen-sample floor: too few held-out cases is not trustworthy generalization.
    if min_held_out and held_out_n < min_held_out:
        return ValidationResult(False, in_delta, out_delta,
                                f"too few held-out cases ({held_out_n} < {min_held_out})",
                                **base)
    # Non-negative on both, strictly positive on at least one.
    if in_delta < 0 or out_delta < 0:
        return ValidationResult(False, in_delta, out_delta,
                                "regressed a split (held-in or held-out)", **base)
    if in_delta <= 0 and out_delta <= 0:
        return ValidationResult(False, in_delta, out_delta,
                                "no improvement on either split", **base)
    # Effect-size floor: when a held-out split exists, ONLY its lift can satisfy
    # the promotion floor.  Letting a large development-set lift compensate for
    # weak confirmation evidence launders overfit search progress into a sealed
    # promotion decision.
    evidence_delta = out_delta if held_out else in_delta
    if evidence_delta < min_delta:
        return ValidationResult(
            False, in_delta, out_delta,
            f"confirmation improvement below threshold "
            f"({evidence_delta:.4g} < {min_delta})",
            **base)
    # Statistical confidence: bound the UPLIFT itself, including uncertainty in
    # both arms.  The previous candidate-LB > baseline-point rule understated
    # uncertainty in the baseline and was anti-conservative under noisy search.
    if confidence_z > 0 and ev_n > 0:
        if ev_cand_outcomes is not None and ev_base_outcomes is not None:
            ci_low = _paired_difference_ci_lower(
                ev_cand_outcomes, ev_base_outcomes, confidence_z)
        else:
            ci_low = _difference_ci_lower(ev_cand, ev_base, ev_n, ev_n, confidence_z)
        base["effect_ci_low"] = ci_low
        if ci_low <= min_delta:
            return ValidationResult(
                False, in_delta, out_delta,
                f"held-out improvement not confident "
                f"(effect CI lower {ci_low:.3g} <= {min_delta:.3g})",
                **base)
    # Operational regression: a line that lifts pass rate but blows up cost,
    # latency, or tool-call churn on the unseen split is not a net win.
    operational_failure = _operational_regression_reason((
        ("cost", max_cost_factor, ev_costs),
        ("latency", max_latency_factor, ev_lats),
        ("tool_calls", max_tool_calls_factor, ev_tools),
    ))
    if operational_failure is not None:
        return ValidationResult(
            False, in_delta, out_delta, operational_failure, **base)
    # Metamorphic robustness: a line that helps only the EXACT held-out wording is
    # overfit to surface form, not the underlying weakness. If an injected
    # ``metamorphic_fn`` (a meaning-preserving paraphraser -- a real one needs a
    # model, so it is injected) can transform the unseen cases, the improvement
    # must SURVIVE the transform (held-out delta stays >= -tolerance on the
    # paraphrases). A line that flips to hurting paraphrased cases is rejected.
    if metamorphic_fn is not None and held_out:
        failure = _metamorphic_failure(
            add=add, held_out=held_out, score_with=score_with,
            score_without=score_without, metamorphic_fn=metamorphic_fn,
            metamorphic_tolerance=metamorphic_tolerance,
            in_delta=in_delta, out_delta=out_delta, result_base=base,
            signature=proposal.signature,
            holdout_authorize=holdout_authorize)
        if failure is not None:
            return failure
    return ValidationResult(True, in_delta, out_delta, "validated", **base)


def _validate_rotated(
    proposal: HarnessProposal, *, pool: list[str], rotations: int,
    score_with: ScoreFn, score_without: ScoreFn, validate_kwargs: dict,
) -> ValidationResult:
    """Cross-validate a candidate across ``rotations`` holdout folds and accept
    only if it GENERALIZES, not just if one lucky 30%% slice liked it.

    The combined ``pool`` of held-in + held-out goals is partitioned into K
    deterministic folds (:func:`corpus_kfold_splits`); the candidate is validated
    once per fold and the verdicts are aggregated with an ALL-folds rule: every
    fold must accept (so a line that regresses or fails the floors in ANY fold is
    rejected -- the strict, low-variance generalization test). The reported deltas
    are the per-fold means; ``samples`` is the distinct pool size, not the folds'
    samples summed -- every goal in ``pool`` appears in every fold (k-fold, not a
    disjoint split), so summing would inflate the evidence count by ~k and let
    rotation under-clear the downstream gate's ``min_samples`` floor. Falls back
    to a single
    :func:`validate_proposal` when the pool is too small to fold (< 2 goals) or
    ``rotations <= 1``."""
    from .self_harness_eval import corpus_kfold_splits
    splits = corpus_kfold_splits([{"goal": g} for g in pool], k=rotations)
    # Too small to fold (one rotation with empty held-out): defer to the single
    # split the caller would otherwise have used (held_in=pool, held_out=[]).
    if len(splits) < 2:
        hi, ho = (splits[0] if splits else (list(pool), []))
        return validate_proposal(proposal, held_in=hi, held_out=ho,
                                 score_with=score_with, score_without=score_without,
                                 **validate_kwargs)
    results = [validate_proposal(proposal, held_in=hi, held_out=ho,
                                 score_with=score_with, score_without=score_without,
                                 **validate_kwargs)
               for hi, ho in splits]
    k = len(results)
    n_pass = sum(1 for r in results if r.accepted)
    in_delta = sum(r.held_in_delta for r in results) / k
    out_delta = sum(r.held_out_delta for r in results) / k
    base = dict(
        baseline_score=sum(r.baseline_score for r in results) / k,
        candidate_score=sum(r.candidate_score for r in results) / k,
        samples=len(pool),
        held_in_samples=len(pool),
        held_out_samples=len(pool),
        effect_ci_low=(min(r.effect_ci_low for r in results
                           if r.effect_ci_low is not None)
                       if all(r.effect_ci_low is not None for r in results)
                       else None))
    if n_pass == k:
        return ValidationResult(True, in_delta, out_delta,
                                f"validated across {k} rotations", **base)
    # Surface the first failing fold's reason so an operator sees WHY it didn't
    # generalize, not just that it didn't.
    first_fail = next((r for r in results if not r.accepted), None)
    why = first_fail.reason if first_fail is not None else "rotation"
    return ValidationResult(False, in_delta, out_delta,
                            f"failed rotation ({n_pass}/{k} folds passed: {why})", **base)


# ---- APPLY / ROLLBACK (the reversible handle the gate requires) -----------

def _compose_addendum(model_id: str, existing: str, line: str) -> str:
    """Append ``line`` to a model's addendum block, bounded + deduped."""
    header = "Operating guidance learned for this model:"
    lines = []
    for raw in (existing or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("Operating guidance"):
            continue
        if ln.startswith("- "):
            ln = ln[2:].strip()
        lines.append(ln)
    # Delta-merge (ACE anti-collapse, arXiv 2510.04618): drop any existing line
    # that normalizes to the SAME guidance as the new one, then append the new
    # one last (newest). This refreshes an exact OR trivially-reworded re-promote
    # to newest (renewed relevance) rather than leaving it where this pass's
    # other new lines would evict it under the newest-wins cap -- which left a
    # promoted line absent from the store (found by the 100k soak) -- and stops a
    # case/whitespace variant from consuming a second of the bounded slots and
    # evicting DISTINCT guidance (the "context collapse / brevity bias" mode).
    # Normalized-EXACT, not fuzzy: templated per-class lines differ by one token,
    # so a similarity threshold would wrongly merge distinct failure classes.
    nl = _norm_line(line)
    lines = [ln for ln in lines if _norm_line(ln) != nl]
    lines.append(line)
    lines = lines[-_MAX_LINES_PER_MODEL:]

    # Enforce the char budget by dropping WHOLE OLDEST bullets, not by slicing
    # the rendered block. A trailing ``block[:_MAX_ADDENDUM_CHARS]`` cut the
    # NEWEST bullets off the end (inverting the newest-wins cap) and could sever
    # a bullet mid-line -- corrupting the last stored line so a later re-promote
    # of it no longer dedups. Lines are <=280 chars, so ~6 max-length lines
    # already overflow 1500: this is reachable, not theoretical. Drop from the
    # front (oldest) until it fits; keep at least the single newest line.
    # (Found by the stateful sequence battery.)
    def _render(ls: list[str]) -> str:
        return header + "\n" + "\n".join(f"- {x}" for x in ls) if ls else header

    while len(lines) > 1 and len(_render(lines)) > _MAX_ADDENDUM_CHARS:
        lines.pop(0)
    # Last-resort guard for a single pathological line wider than the buffer
    # (can't happen under the 280-char propose cap, but stays defensive).
    return _render(lines)[:_MAX_ADDENDUM_CHARS]


def _rollback_handle(path: Path | None = None) -> Callable[[], None]:
    """A thunk restoring the addendum store to its CURRENT state -- the
    reversible handle the gate requires. Captured before any write, so it is a
    valid undo whether or not the change is ultimately applied. Restores the
    provenance sidecar too, so a revert leaves no orphaned metadata."""
    p = path if path is not None else _store_path()
    before = load_addenda(p)
    before_meta = load_line_meta(p)

    def _rollback() -> None:
        _write_addenda(before, p)
        try:
            _write_line_meta(before_meta, p)
        except Exception:  # pragma: no cover -- sidecar is best-effort
            pass

    return _rollback


def _cas_rollback_handle(
    path: Path, *, before_store: dict[str, str], after_store: dict[str, str],
    store_key: str, before_meta: dict[str, dict],
) -> Callable[[], None]:
    """Restore only an exact promoted generation, never a newer store.

    The old snapshot rollback could erase unrelated promotions that landed
    after this candidate. This handle first proves the complete addenda store is
    still the exact ``after`` revision. It then restores the prior source of
    truth and reconciles only the affected block's metadata.
    """
    before_revision = _addenda_artifact_revision(path, before_store)
    after_revision = _addenda_artifact_revision(path, after_store)
    prior_records = {
        key: dict(value) for key, value in before_meta.items()
        if value.get("model_id") == store_key
    }

    def _rollback() -> None:
        from .file_lock import cross_process_lock

        with _lock, cross_process_lock(path), _store_rmw_lock(path):
            current = _load_addenda_strict(path)
            if _addenda_artifact_revision(path, current) != after_revision:
                raise RuntimeError(
                    "refusing rollback because the addenda store has a newer revision")
            _write_addenda(before_store, path)
            restored = _load_addenda_strict(path)
            if _addenda_artifact_revision(path, restored) != before_revision:
                raise RuntimeError("addenda rollback did not restore the declared revision")
            try:
                meta = load_line_meta(path)
                # Remove records introduced/retained only by the reverted block,
                # restore any evicted prior records, and preserve unrelated
                # models/scopes plus their newer recall counters.
                _reconcile_meta(meta, store_key, _bullets(before_store.get(store_key, "")))
                for key, value in prior_records.items():
                    meta.setdefault(key, value)
                _write_line_meta(meta, path)
            except Exception:  # pragma: no cover -- source of truth is restored
                log.debug("self_harness: rollback metadata reconciliation failed",
                          exc_info=True)

    return _rollback


# Serializes the store's load-modify-save so concurrent passes (the runner, a
# manual CLI, parallel per-model passes) can't clobber each other. The whole
# read-compose-write is one critical section -- an in-process lock plus a
# cross-process flock -- because without it two writers that both read the old
# store both write back, losing one's addendum (8 concurrent promotions
# collapsed to 1 in a forced-interleaving probe).
_lock = threading.Lock()


def _apply_addendum_locked(
    proposal: HarnessProposal, path: Path, before: dict[str, str], *,
    provenance: dict | None = None, store_key: str | None = None,
) -> dict[str, str]:
    """Apply one proposal while the caller holds every store/CAS lock."""
    store_key = store_key or _scoped_key(proposal.model_id, proposal.context)
    after = dict(before)
    new_block = _compose_addendum(
        store_key, before.get(store_key, ""), proposal.addendum_line)
    after[store_key] = new_block
    _write_addenda(after, path)
    try:
        meta = load_line_meta(path)
        _upsert_line_meta(meta, store_key, proposal.addendum_line,
                          provenance or {}, now=time.time())
        _reconcile_meta(meta, store_key, _bullets(new_block))
        _write_line_meta(meta, path)
    except Exception:  # pragma: no cover -- sidecar is best-effort
        log.debug("self_harness: line-meta update failed", exc_info=True)
    return after


def _apply_addendum(proposal: HarnessProposal, path: Path | None = None, *,
                    provenance: dict | None = None) -> None:
    """Write the accepted line into the model's addendum block (atomically), and
    record its provenance in the sidecar reconciled to the block actually
    written. The sidecar update is best-effort: a failure there never perturbs
    the addenda store (the source of truth)."""
    p = path if path is not None else _store_path()
    from .file_lock import cross_process_lock
    with _lock, cross_process_lock(p), _store_rmw_lock(p):
        before = load_addenda(p)
        _apply_addendum_locked(
            proposal, p, before, provenance=provenance)


# ---- RECALL-USAGE TRACKING ------------------------------------------------
# recall_addendum() rides the agent's per-prompt hot path and stays a PURE READ.
# Usage ("when was this guidance last actually used?") is tracked separately by
# the consumer (Agent._with_harness_addendum) via note_recall, which is
# IN-PROCESS THROTTLED: the common case is a dict lookup with NO I/O, and disk is
# touched at most once per model per interval. This lets retire_stale keep a line
# that's still being USED even if it hasn't been re-PROMOTED in a while.
_recall_note_lock = threading.Lock()
_recall_noted_monotonic: dict[str, float] = {}


def note_recall(model_id: str | None, *, now: float | None = None,
                path: Path | None = None, min_interval_s: float = 3600.0,
                domain: str | None = None,
                tools: list[str] | tuple[str, ...] | None = None,
                role: str | None = None) -> None:
    """Best-effort: record that ``model_id``'s guidance was just recalled, so
    staleness can be judged by USE, not only by promotion. In-process throttled
    (``min_interval_s``; 0 disables the throttle, for tests) so the hot recall
    path does at most one cheap write per model per interval. Tracks the
    same exact keys ``recall_addendum`` injected for this run. Under secure
    defaults those keys include the bound matter and hashed principal; without a
    MatterContext nothing is credited. Never raises."""
    if not model_id:
        return
    mid = str(model_id)
    contexts = _scope_contexts(domain, tools, role)
    keys = _runtime_store_keys(
        mid, contexts, secure=_secure_defaults_enabled(),
    )
    if not keys:
        return
    throttle_key = "\x00".join(keys)
    if min_interval_s:
        mono = time.monotonic()
        with _recall_note_lock:
            last = _recall_noted_monotonic.get(throttle_key)
            if last is not None and (mono - last) < min_interval_s:
                return  # throttled -- pure in-memory, no I/O
            _recall_noted_monotonic[throttle_key] = mono
    try:
        p = path if path is not None else _store_path()
        ts = now if now is not None else time.time()
        from .file_lock import cross_process_lock
        with _lock, cross_process_lock(p), _store_rmw_lock(p):
            meta = load_line_meta(p)
            store = load_addenda(p)
            changed = False
            for key in keys:
                for ln in _bullets(store.get(key, "")):
                    rec = meta.get(_line_id(key, ln))
                    if rec is not None:
                        rec["last_recalled_at"] = ts
                        rec["recall_notes"] = (rec.get("recall_notes") or 0) + 1
                        changed = True
            if changed:
                _write_line_meta(meta, p)
    except Exception:  # pragma: no cover -- usage tracking never perturbs a run
        log.debug("self_harness: note_recall failed", exc_info=True)


# ---- OUTCOME-CORRELATED EFFICACY ------------------------------------------
# Recall tracks *that* guidance was used; efficacy tracks whether using it
# correlated with SUCCESS. Two complementary signals: cheap recall->outcome
# counters in the sidecar (a coarse aggregate -- all of a model's lines co-occur
# in its prompt, so an outcome is attributed to all current lines), and a
# causal A/B re-review (`review_efficacy`) that re-measures a line's lift with a
# live scorer and demotes dead weight. Demotion is the REVERSIBLE direction
# (removing a line is always safe -- it's the rollback handle), so it goes through
# the audited `forget` path, not the promotion gate.

# Bounded per-line window of the most RECENT outcomes (1 = success, 0 =
# failure), newest last. Lifetime counters only ever grow, so a line with a few
# ancient successes would be shielded forever; the window is the recency signal
# the counter-driven reviews judge instead. Small on purpose: it rides the
# sidecar record and 20 outcomes is plenty to see a relapse.
_OUTCOME_WINDOW = 20


def _recent_counts(rec: Mapping) -> tuple[int, int]:
    """(successes, failures) from a record's recent-outcomes window; a legacy
    record with no window falls back to the lifetime counters, so pre-window
    sidecars keep their historical behavior."""
    window = rec.get("recent_outcomes")
    if isinstance(window, list) and window:
        s = sum(1 for o in window if o)
        return s, len(window) - s
    return int(rec.get("recall_success") or 0), int(rec.get("recall_failure") or 0)


def _is_relapsing(rec: Mapping, *, failure_share: float, min_outcomes: int) -> bool:
    """The recent-window predicate used by :func:`review_relapses`.

    ``failure_share <= 0`` disables it; a record whose window holds fewer than
    ``min_outcomes``
    outcomes -- including legacy records with no window at all -- is never
    relapsing (lifetime counters are not a recency signal)."""
    if not failure_share or failure_share <= 0:
        return False
    window = rec.get("recent_outcomes")
    if not isinstance(window, list) or len(window) < max(1, int(min_outcomes)):
        return False
    return sum(1 for o in window if not o) / len(window) >= failure_share


def note_outcome(model_id: str | None, success: bool, *, line: str | None = None,
                 domain: str | None = None,
                 tools: list[str] | tuple[str, ...] | None = None,
                 role: str | None = None,
                 path: Path | None = None) -> None:
    """Record that a run using ``model_id``'s guidance succeeded or failed,
    incrementing per-line ``recall_success``/``recall_failure`` counters AND
    appending to the bounded ``recent_outcomes`` window in the sidecar -- the
    signals :func:`review_efficacy`/:func:`review_canaries`/
    :func:`review_relapses` act on.

    Under secure defaults a specific line and the normal aggregate path are both
    constrained to the exact bound matter/owner keys that runtime recall could
    have injected. Missing MatterContext records nothing. Explicitly insecure
    legacy mode retains the historical line-across-model-scopes behavior.
    Best-effort; never raises."""
    if not model_id:
        return
    mid = str(model_id)
    secure = _secure_defaults_enabled()
    keys = _runtime_store_keys(
        mid, _scope_contexts(domain, tools, role), secure=secure,
    )
    if not keys:
        return
    field_name = "recall_success" if success else "recall_failure"
    try:
        p = path if path is not None else _store_path()
        from .file_lock import cross_process_lock
        with _lock, cross_process_lock(p), _store_rmw_lock(p):
            store = load_addenda(p)
            meta = load_line_meta(p)
            changed = False
            if line is not None:
                if secure:
                    pairs = [
                        (key, stored_line)
                        for key in keys
                        for stored_line in _bullets(store.get(key, ""))
                        if stored_line == line
                    ]
                else:
                    pairs = [
                        (key, stored_line)
                        for key, stored_line in _iter_model_lines(store, mid)
                        if stored_line == line
                    ]
            else:
                pairs = [(k, ln) for k in keys for ln in _bullets(store.get(k, ""))]
            for k, ln in pairs:
                rec = meta.get(_line_id(k, ln))
                if rec is not None:
                    rec[field_name] = int(rec.get(field_name) or 0) + 1
                    window = rec.get("recent_outcomes")
                    window = list(window) if isinstance(window, list) else []
                    window.append(1 if success else 0)
                    rec["recent_outcomes"] = window[-_OUTCOME_WINDOW:]
                    changed = True
            if changed:
                _write_line_meta(meta, p)
    except Exception:  # pragma: no cover -- efficacy tracking never perturbs a run
        log.debug("self_harness: note_outcome failed", exc_info=True)


def line_efficacy(model_id: str, path: Path | None = None) -> list[dict]:
    """Per-line outcome counters for ``model_id`` across ALL its scopes: each
    ``{text, domain, success, failure, total, rate}`` where ``rate`` is
    success/total (``None`` until an outcome lands) and ``domain`` is the scope
    (``None`` for model-wide). Read-only. Includes domain-scoped lines, which the
    pre-fix version was blind to."""
    store = load_addenda(path)
    meta = load_line_meta(path)
    out = []
    for k, ln in _iter_model_lines(store, str(model_id)):
        rec = meta.get(_line_id(k, ln)) or {}
        s = int(rec.get("recall_success") or 0)
        f = int(rec.get("recall_failure") or 0)
        total = s + f
        rs, rf = _recent_counts(rec)
        recent = rs + rf
        _, ctx = _split_key(k)
        domain = ctx[len("domain="):] if ctx.startswith("domain=") else None
        out.append({"text": ln, "domain": domain, "success": s, "failure": f,
                    "total": total, "rate": (s / total) if total else None,
                    "recent_success": rs, "recent_failure": rf,
                    "recent_rate": (rs / recent) if recent else None})
    return out


def review_efficacy(model_id: str, cases: list[str], *,
                    score_with: ScoreFn, score_without: ScoreFn,
                    min_lift: float = 0.0, min_samples: int = 1,
                    path: Path | None = None) -> list[str]:
    """Re-measure each current line's CAUSAL lift with a live A/B and DEMOTE the
    dead weight -- a line whose lift has fallen to ``<= min_lift`` over at least
    ``min_samples`` cases no longer earns its place in every prompt (the model,
    tools, or APIs moved on). Returns the demoted lines.

    Demotion uses the audited `forget` path (reversible), NOT the promotion gate.
    Scorers are injected (a real eval needs a real model); without ``cases`` it is
    a no-op. Never raises -- a maintenance review must not perturb anything."""
    demoted: list[str] = []
    if not cases:
        return demoted
    try:
        # Snapshot the (key, line) pairs first: forget_addendum mutates the store
        # mid-loop, so iterating a live view would skip lines. Spans every scope.
        for _k, ln in list(_iter_model_lines(load_addenda(path), str(model_id))):
            try:
                w, *_ = _score_parts(score_with(ln, cases))
                wo, *_ = _score_parts(score_without(ln, cases))
            except Exception:  # a bad scorer can't drop a line
                continue
            if not (isinstance(w, (int, float)) and isinstance(wo, (int, float))
                    and math.isfinite(w) and math.isfinite(wo)):
                continue
            if len(cases) >= min_samples and (w - wo) <= min_lift:
                if forget_addendum(model_id, line=ln, path=path):
                    demoted.append(ln)
    except AuditRefused:
        raise
    except Exception:  # pragma: no cover -- review never perturbs a run
        log.debug("self_harness: review_efficacy failed", exc_info=True)
    return demoted


# ---- CANARY / STAGED ROLLOUT ----------------------------------------------
# A freshly-promoted line can ride as a CANARY (on probation): still recalled,
# but watched. `review_canaries` reads the recall->outcome counters and advances
# the lifecycle -- GRADUATE a line that proves out (clear the flag -> permanent),
# DEMOTE one that correlates with failures (audited forget), leave the rest on
# probation. A slow-rollout valve on top of the gate; failures win ties (safe).

def mark_canary(model_id: str, line: str, *, canary: bool = True,
                path: Path | None = None) -> bool:
    """Mark/unmark a learned line as a canary. Returns True if the flag changed
    (False if the line has no sidecar record or already had this state). Atomic;
    never raises."""
    if not model_id or line is None:
        return False
    try:
        p = path if path is not None else _store_path()
        from .file_lock import cross_process_lock
        with _lock, cross_process_lock(p), _store_rmw_lock(p):
            store = load_addenda(p)
            meta = load_line_meta(p)
            changed = False
            for k, ln in _iter_model_lines(store, str(model_id)):
                if ln != line:
                    continue
                rec = meta.get(_line_id(k, ln))
                if rec is not None and bool(rec.get("canary")) != bool(canary):
                    rec["canary"] = bool(canary)
                    changed = True
            if changed:
                _write_line_meta(meta, p)
            return changed
    except Exception:  # pragma: no cover -- canary state never perturbs a run
        log.debug("self_harness: mark_canary failed", exc_info=True)
        return False


def list_canaries(model_id: str, path: Path | None = None) -> list[str]:
    """The model's lines currently on canary probation, across ALL scopes
    (model-wide + domain). Includes domain-scoped canaries, which the pre-fix
    version was blind to."""
    store = load_addenda(path)
    meta = load_line_meta(path)
    return [ln for k, ln in _iter_model_lines(store, str(model_id))
            if (meta.get(_line_id(k, ln)) or {}).get("canary")]


def review_canaries(model_id: str, *, graduate_after: int = 3,
                    demote_after: int = 2, path: Path | None = None) -> dict:
    """Advance the canary lifecycle from the recall->outcome counters:
    DEMOTE (audited forget) a canary with ``>= demote_after`` failures; else
    GRADUATE (clear the flag -> permanent) one with ``>= graduate_after``
    successes; else leave it on probation. Failures take precedence (a flaky line
    is pulled even if it also has wins). Counts come from the RECENT-outcomes
    window when one exists (so a RE-probated veteran is judged on fresh evidence,
    not shielded by its lifetime successes); a legacy record with no window keeps
    the lifetime-counter behavior. Spans every scope, so domain-scoped
    canaries are reviewed too. Returns ``{"graduated": [...], "demoted": [...]}``.
    Never raises."""
    graduated: list[str] = []
    demoted: list[str] = []
    try:
        store = load_addenda(path)
        meta = load_line_meta(path)
        # Snapshot pairs first -- forget/mark mutate the store/meta mid-loop.
        for k, ln in list(_iter_model_lines(store, str(model_id))):
            rec = meta.get(_line_id(k, ln)) or {}
            if not rec.get("canary"):
                continue
            s, f = _recent_counts(rec)
            if f >= demote_after:
                if forget_addendum(model_id, line=ln, path=path):
                    demoted.append(ln)
            elif s >= graduate_after:
                if mark_canary(model_id, ln, canary=False, path=path):
                    graduated.append(ln)
    except AuditRefused:
        raise
    except Exception:  # pragma: no cover -- review never perturbs a run
        log.debug("self_harness: review_canaries failed", exc_info=True)
    return {"graduated": graduated, "demoted": demoted}


def review_relapses(model_id: str, *, failure_share: float,
                    min_outcomes: int = 5, path: Path | None = None) -> list[str]:
    """Put a GRADUATED (non-canary) line whose recent outcomes have gone bad
    BACK on canary probation -- the recency guard the lifetime counters lack: a
    line with a few ancient successes is otherwise immune to the counter-driven
    lifecycle forever, however it performs today.

    A line relapses when its recent-outcomes window holds ``>= min_outcomes``
    outcomes and the failing share is ``>= failure_share``. Re-probation is the
    REVERSIBLE, advisory-first direction: nothing is removed here -- the line is
    just watched again, and :func:`review_canaries` (judging the same recent
    window) graduates it back or pulls it via the audited ``forget`` path as
    fresh evidence arrives. Lines already on probation and legacy records with
    no window are skipped. Each re-probation is audited (phase ``relapse``).
    Returns the re-probated lines. Never raises."""
    relapsed: list[str] = []
    try:
        if not failure_share or failure_share <= 0:
            return relapsed
        store = load_addenda(path)
        meta = load_line_meta(path)
        for k, ln in list(_iter_model_lines(store, str(model_id))):
            rec = meta.get(_line_id(k, ln)) or {}
            if rec.get("canary"):
                continue  # already on probation, already watched
            if not _is_relapsing(rec, failure_share=failure_share,
                                 min_outcomes=min_outcomes):
                continue
            window = rec.get("recent_outcomes") or []
            fails = sum(1 for o in window if not o)
            if mark_canary(model_id, ln, canary=True, path=path):
                relapsed.append(ln)
                from .audit import EventKind, audit_event

                mid, ctx = _split_key(k)
                audit_event(
                    EventKind.LEARNING_UPDATE,
                    agent="self_harness",
                    model_id=mid,
                    scope=(ctx or None),
                    rung="prompt",
                    line_sha256=hashlib.sha256(ln.encode("utf-8")).hexdigest(),
                    phase="relapse",
                    recent_failures=fails,
                    recent_total=len(window),
                )
    except AuditRefused:
        raise
    except Exception:  # pragma: no cover -- review never perturbs a run
        log.debug("self_harness: review_relapses failed", exc_info=True)
    return relapsed


# ---- DRIVE (mine -> propose -> validate -> gate) --------------------------

@dataclass
class SelfHarnessReport:
    model_id: str
    mined: int = 0
    proposed: int = 0
    validated: int = 0
    promoted: int = 0
    skipped: list[str] = field(default_factory=list)
    applied_lines: list[str] = field(default_factory=list)
    # Advisory: (new_line, existing_line) pairs the promoted line may contradict.
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    # Governance readiness at pass time -- so an operator can tell WHY a pass
    # promoted nothing: the verifier-drift freeze is armed (no learning while the
    # judge is unreliable) and/or the promotion gate is disabled. Both are
    # best-effort reads; neither changes the pass's behavior (the gate still
    # enforces them), they only EXPLAIN it.
    frozen: bool = False
    gate_enabled: bool = True
    # Outcome-driven lifecycle results, populated by the driver cycle from the
    # accumulated recall->outcome counters: canaries that graduated to permanent
    # and lines demoted (canary pulled, or efficacy dead-weight).
    graduated: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    # Graduated lines put BACK on canary probation because their recent
    # outcomes turned bad (the relapse recency guard).
    relapsed: list[str] = field(default_factory=list)


def _governance_readiness() -> tuple[bool, bool]:
    """(frozen, gate_enabled) -- whether verifier-drift has frozen learning and
    whether the promotion controller is enabled. Best-effort: any error reads as
    'not frozen, enabled' so readiness reporting never blocks or misleads a pass
    that would otherwise run."""
    frozen = False
    gate_enabled = True
    try:
        from . import calibration
        frozen = bool(calibration.learning_frozen())
    except Exception:  # pragma: no cover -- readiness read never blocks a pass
        pass
    try:
        from . import self_improvement as si
        gate_enabled = bool(si.enabled())
    except Exception:  # pragma: no cover
        pass
    return frozen, gate_enabled


def _best_validated_candidate(
    sig: FailureSignature, *, first: HarnessProposal, propose_fn, k: int,
    held_in: list[str], held_out: list[str], score_with, score_without,
    validate_kwargs: dict, holdout_rotations: int = 1,
) -> tuple[HarnessProposal | None, ValidationResult | None]:
    """Best-of-N with a sealed confirmation boundary.

    ``first`` is the proposal already in hand (counted once by the caller); up to
    ``k-1`` more are drawn from ``propose_fn`` and de-duped by normalized line, so
    a DETERMINISTIC proposer -- which returns the same line every call -- collapses
    to exactly the single-candidate result and ``k`` only ever matters for a
    STOCHASTIC (e.g. LLM-at-temperature) proposer.

    Candidates compete ONLY on ``held_in`` development evidence.  The winner is
    frozen before the held-out scorer is called, then receives exactly one
    confirmation evaluation (or a configured rotation battery).  Selecting the
    maximum on held-out and promoting from that same score adaptively overfits the
    test set; this boundary is what makes the reported held-out evidence honest.
    ``(None, None)`` if development finds no survivor or confirmation fails.
    """
    if not held_in:
        return None, None  # no development set -> best-of-N would search on test
    candidates = [first]
    for _ in range(max(0, k - 1)):
        p = propose_addendum(sig, propose_fn=propose_fn)
        if p is not None:
            candidates.append(p)
    seen: set[str] = set()
    best: tuple[tuple, HarnessProposal, ValidationResult] | None = None
    dev_kwargs = dict(validate_kwargs)
    dev_kwargs.update(min_held_out=0, confidence_z=0.0, metamorphic_fn=None)
    for proposal in candidates:
        norm = _norm_line(proposal.addendum_line)
        if norm in seen:
            continue
        seen.add(norm)
        vr = validate_proposal(
            proposal, held_in=held_in, held_out=[],
            score_with=score_with, score_without=score_without, **dev_kwargs)
        if not vr.accepted:
            continue
        key = (round(vr.held_in_delta, 6), vr.samples)
        if best is None or key > best[0]:
            best = (key, proposal, vr)
    if best is None:
        return None, None
    selected = best[1]
    if holdout_rotations > 1 and held_out:
        min_required = int(validate_kwargs.get("min_held_out", 0) or 0)
        if min_required and len(held_out) < min_required:
            return None, None
        rotation_kwargs = dict(validate_kwargs)
        # The floor applies to the complete sealed pool, not each smaller fold.
        rotation_kwargs["min_held_out"] = 0
        confirmed = _validate_rotated(
            selected, pool=list(held_out), rotations=holdout_rotations,
            score_with=score_with, score_without=score_without,
            validate_kwargs=rotation_kwargs)
    else:
        confirmed = validate_proposal(
            selected, held_in=held_in, held_out=held_out,
            score_with=score_with, score_without=score_without, **validate_kwargs)
    if not confirmed.accepted:
        return None, None
    return selected, confirmed


def run_self_harness(  # noqa: C901 - fail-closed orchestration boundary
    reflexions: list[dict], *, model_id: str,
    project_id: int | None = None, owner: str | None = None,
    held_in: list[str] | None = None, held_out: list[str] | None = None,
    score_with: ScoreFn | None = None, score_without: ScoreFn | None = None,
    propose_fn: ProposeFn | None = None, controller=None,
    min_support: int = 3, path: Path | None = None,
    require_held_out: bool = False, min_delta: float = 0.0, min_held_out: int = 0,
    confidence_z: float = 0.0, max_cost_factor: float | None = None,
    max_latency_factor: float | None = None, max_tool_calls_factor: float | None = None,
    min_support_by_class: dict[str, int] | None = None,
    candidates_per_signature: int = 1,
    max_promotions_per_cycle: int = 0,
    semantic_mining: bool = False,
    similarity_fn: Callable[[str, str], float] | None = None,
    bucket_by: tuple[str, ...] = (),
    metamorphic_fn: Callable[[list[str]], list[str]] | None = None,
    metamorphic_tolerance: float = 0.0,
    holdout_authorize: HoldoutAuthorize | None = None,
    holdout_rotations: int = 1,
    canary: bool = False,
    eval_for_context: Callable[[str], tuple | None] | None = None,
    promotion_authorize: Callable[[], bool] | None = None,
    apply_promotions: bool = False,
) -> SelfHarnessReport:
    """One self-harness pass for ``model_id``: mine weaknesses, propose minimal
    edits, validate on held-in/held-out, and GATE each survivor through the
    self-improvement ladder before applying it.

    ``score_with``/``score_without`` (the live A/B) are injected; without them
    validation is skipped and the pass is a dry inspection (nothing is applied).
    ``semantic_mining`` (or an explicit ``similarity_fn``) clusters failures by
    meaning rather than strict token overlap -- an injected fn wins, else the flag
    selects the deterministic built-in :func:`semantic_similarity`. ``bucket_by``
    mines at a finer granularity (e.g. ``("domain",)`` for department-scoped
    weaknesses + guidance).

    ``eval_for_context`` is the SCOPED-EVALUATION seam: given a signature's
    mining context (e.g. ``"domain=finance"``), return a ``(held_in, held_out,
    score_with, score_without)`` quad to validate THAT signature's candidates
    against, or ``None`` to keep the pass-level defaults. A department-scoped
    weakness is then judged on its own department's cases rather than the
    general pool (which under-credits a narrow line). It may also supply
    scorers a dry pass lacks, so a domain-keyed-only corpus still validates its
    scoped candidates. Default ``None`` = the historical single evaluation.
    ``promotion_authorize`` is a final fail-closed evidence receipt checked
    after validation and immediately before the governed apply transaction. It
    lets risk-limited callers bind a just-completed evaluation to a fresh judge
    calibration window; default ``None`` forbids artifact application.
    Every pass requires one exact ``project_id`` + ``owner`` scope and ignores
    records that are missing or do not exactly match it. This applies even to
    offline candidate generation: raw client traces are never treated as a
    global/default corpus. The default is offline-only: mining/proposal/
    evaluation remain intact, but validated candidates do not mutate the
    runtime addendum store. An explicit operator promotion must additionally
    set ``apply_promotions=True`` and provide a positive
    ``promotion_authorize`` evidence callback. Runtime runners never opt in.
    Returns a :class:`SelfHarnessReport`. Operational failures are captured in
    the report, but :class:`Halted` is deliberately re-raised so an operator
    interlock cannot be mistaken for an ordinary no-change pass."""
    report = SelfHarnessReport(model_id=str(model_id))
    if not enabled():
        report.skipped.append("disabled")
        return report
    # Record governance readiness so a pass that promotes nothing can be EXPLAINED
    # (verifier-drift freeze armed / promotion gate off), not just observed.
    report.frozen, report.gate_enabled = _governance_readiness()
    try:
        check_learning_halt("self_harness", "start")
        promotion_scope = _exact_promotion_scope(project_id, owner)
        if promotion_scope is None:
            report.skipped.append(
                "reflexion processing requires exact matter and owner scope"
            )
            return report
        reflexions = _scope_reflexions_for_harness(
            reflexions, matter_id=promotion_scope[0], owner=str(owner),
        )
        sim_fn = similarity_fn or (semantic_similarity if semantic_mining else None)
        sigs = mine_failures(reflexions, model_id=model_id, min_support=min_support,
                             min_support_by_class=min_support_by_class,
                             similarity_fn=sim_fn, bucket_by=bucket_by)
        report.mined = len(sigs)
        for sig in sigs:
            if (max_promotions_per_cycle
                    and report.promoted >= max_promotions_per_cycle):
                report.skipped.append(
                    "promotion cap reached; refresh deployed prompt before "
                    "evaluating the next candidate")
                break
            # The addendum block holds at most _MAX_LINES_PER_MODEL lines. Stop
            # once this pass has filled them: signatures are sorted STRONGEST
            # (highest support) first, so processing more would only let a weaker
            # line evict a stronger one under the newest-wins cap -- and would
            # gate + audit a promotion we'd immediately discard. Keep the
            # strongest weaknesses; report the rest as deferred.
            if report.promoted >= _MAX_LINES_PER_MODEL:
                report.skipped.append(f"addendum at capacity: {sig.signature}")
                continue
            proposal = propose_addendum(sig, propose_fn=propose_fn)
            if proposal is None:
                report.skipped.append(f"no proposal: {sig.signature}")
                continue
            report.proposed += 1

            # Resolve THIS signature's evaluation: the pass-level splits/scorers,
            # unless the scoped seam supplies a quad for its mining context (a
            # finance-scoped candidate judged on finance cases). Resolution comes
            # FIRST so the seam can also supply scorers a dry pass lacks.
            sw, swo = score_with, score_without
            hi, ho = held_in or list(sig.examples), held_out or []
            if eval_for_context is not None and sig.context:
                try:
                    quad = eval_for_context(sig.context)
                except Halted:
                    raise
                except Exception:  # a bad seam keeps the defaults, never crashes
                    log.debug("self_harness: eval_for_context failed", exc_info=True)
                    quad = None
                if quad is not None:
                    hi, ho, sw, swo = quad
                    hi, ho = list(hi or []), list(ho or [])
            if sw is None or swo is None:
                report.skipped.append(f"no scorer (dry): {proposal.addendum_line}")
                continue
            # Held-out is the overfitting guard the docs call mandatory; with a
            # live scorer, `require_held_out` makes that explicit -- never promote
            # on only the mined examples (default off for back-compat).
            if require_held_out and not ho:
                report.skipped.append(f"no held-out cases: {proposal.addendum_line}")
                continue
            validate_kwargs = dict(
                min_delta=min_delta, min_held_out=min_held_out,
                confidence_z=confidence_z, max_cost_factor=max_cost_factor,
                max_latency_factor=max_latency_factor,
                max_tool_calls_factor=max_tool_calls_factor,
                metamorphic_fn=metamorphic_fn,
                metamorphic_tolerance=metamorphic_tolerance,
                holdout_authorize=holdout_authorize)
            if candidates_per_signature and candidates_per_signature > 1:
                # Best-of-N: keep the strongest of several candidate lines for this
                # signature (only meaningful with a stochastic proposer; a
                # deterministic one collapses to the single candidate).
                proposal, vr = _best_validated_candidate(
                    sig, first=proposal, propose_fn=propose_fn,
                    k=candidates_per_signature, held_in=hi, held_out=ho,
                    score_with=sw, score_without=swo,
                    validate_kwargs=validate_kwargs,
                    holdout_rotations=holdout_rotations)
                if proposal is None or vr is None:
                    report.skipped.append(f"rejected (no candidate passed): {sig.signature}")
                    continue
            elif holdout_rotations and holdout_rotations > 1 and ho:
                # Holdout rotation: cross-validate across K folds of the combined
                # held-in+held-out pool and accept only if the lift generalizes
                # across ALL folds, not just one fixed slice.
                vr = _validate_rotated(
                    proposal, pool=list(hi) + list(ho), rotations=holdout_rotations,
                    score_with=sw, score_without=swo,
                    validate_kwargs=validate_kwargs)
                if not vr.accepted:
                    report.skipped.append(f"rejected ({vr.reason}): {proposal.addendum_line}")
                    continue
            else:
                vr = validate_proposal(
                    proposal, held_in=hi, held_out=ho, score_with=sw,
                    score_without=swo, **validate_kwargs)
                if not vr.accepted:
                    report.skipped.append(f"rejected ({vr.reason}): {proposal.addendum_line}")
                    continue
            report.validated += 1

            if not apply_promotions:
                report.skipped.append(
                    "validated offline; runtime promotion requires a separate "
                    f"operator action: {proposal.addendum_line}"
                )
                continue
            if promotion_scope is None:
                report.skipped.append(
                    "operator promotion requires exact matter and owner "
                    f"provenance: {proposal.addendum_line}"
                )
                continue
            if promotion_authorize is None:
                report.skipped.append(
                    "operator promotion requires explicit approval evidence: "
                    f"{proposal.addendum_line}"
                )
                continue

            ok, why = _gate_and_apply(proposal, vr, controller=controller,
                                      path=path, canary=canary,
                                      promotion_authorize=promotion_authorize,
                                      matter_id=promotion_scope[0],
                                      owner_scope=promotion_scope[1])
            if not ok:
                # Surface WHY the gate refused (e.g. "too few samples (3 < 5)",
                # frozen verifier, disabled controller) so an operator with a
                # small validation set or a drifting judge isn't left guessing.
                report.skipped.append(f"gate refused ({why}): {proposal.addendum_line}")
                continue
            report.promoted += 1
            report.applied_lines.append(proposal.addendum_line)
            # Advisory conflict check within the block the line actually landed in
            # (the scoped key for a domain line); flag any EXISTING line it appears
            # to contradict (operator review, not a block).
            block_key = _scoped_key(str(model_id), proposal.context)
            existing = [ln for ln in _bullets(load_addenda(path).get(block_key, ""))
                        if _norm_line(ln) != _norm_line(proposal.addendum_line)]
            for c in find_conflicts(proposal.addendum_line, existing):
                report.conflicts.append((proposal.addendum_line, c))
                log.warning("self_harness: learned line may conflict with existing "
                            "guidance for %s: %r vs %r", model_id,
                            proposal.addendum_line, c)
    except Halted:
        raise
    except Exception as e:  # pragma: no cover -- learning never perturbs a run
        log.warning("self_harness: pass failed (%s)", e)
        report.skipped.append(f"error: {e}")
    return report


def _exact_promotion_scope(
    project_id: int | None, owner: str | None,
) -> tuple[int, str] | None:
    """Return auditable matter + opaque principal provenance, fail closed."""
    if project_id is None or isinstance(project_id, bool) or owner is None:
        return None
    try:
        matter_id = int(project_id)
    except (TypeError, ValueError):
        return None
    if matter_id <= 0:
        return None
    owner_scope = _owner_scope_digest(str(owner))
    return matter_id, owner_scope


def _scope_reflexions_for_harness(
    reflexions: list[dict], *, matter_id: int, owner: str,
) -> list[dict]:
    """Return only traces bearing the exact operator-declared provenance."""
    selected: list[dict] = []
    for raw in reflexions or []:
        record = raw.to_dict() if hasattr(raw, "to_dict") else raw
        if not isinstance(record, Mapping):
            continue
        raw_matter = record.get("matter_id")
        if isinstance(raw_matter, bool):
            continue
        try:
            record_matter = int(raw_matter)
        except (TypeError, ValueError):
            continue
        if record_matter != matter_id or record.get("owner") != owner:
            continue
        selected.append(dict(record))
    return selected


def _promotion_receipt_evidence(proposal: HarnessProposal) -> dict[str, int | str]:
    """Return content-free bindings for a self-harness promotion receipt.

    The exact proposal remains in the encrypted matter-scoped addenda/metadata
    store.  Promotion records and their audit outbox are longer-lived and may
    be replicated independently, so they carry only UTF-8 byte counts and
    canonical SHA-256 bindings for client-derived proposal text.
    """
    evidence: dict[str, int | str] = {}
    for label, value in (
        ("addendum", proposal.addendum_line),
        ("signature", proposal.signature),
        ("rationale", proposal.rationale),
        ("hypothesis", proposal.hypothesis or ""),
    ):
        raw = value.encode("utf-8")
        evidence[f"{label}_bytes"] = len(raw)
        evidence[f"{label}_sha256"] = hashlib.sha256(raw).hexdigest()
    return evidence


def _gate_and_apply(  # noqa: C901 - durable promotion transaction boundary
    proposal: HarnessProposal, vr: ValidationResult, *,
    controller=None, path: Path | None = None,
    canary: bool = False,
    promotion_authorize: Callable[[], bool] | None = None,
    matter_id: int, owner_scope: str,
) -> tuple[bool, str]:
    """Prepare, CAS-apply, and commit one governed prompt promotion.

    The durable ledger never claims promotion before the exact intended addenda
    generation is live. A crash after PREPARE is recoverable by comparing the
    current full-store revision with the declared before/after revisions; a
    third state remains in-doubt and blocks later writes instead of guessing.
    """
    from . import self_improvement as si
    from .file_lock import cross_process_lock

    check_learning_halt("self_harness", "promotion")
    if (
        matter_id <= 0
        or not re.fullmatch(r"[0-9a-f]{16}", owner_scope or "")
    ):
        return False, "exact matter and owner provenance unavailable"
    if promotion_authorize is None:
        return False, "explicit operator approval evidence unavailable"
    try:
        if promotion_authorize() is not True:
            return False, "explicit operator approval evidence unavailable"
    except Halted:
        raise
    except Exception:
        log.debug(
            "self_harness: promotion evidence authorization failed",
            exc_info=True)
        return False, "explicit operator approval evidence unavailable"
    if not si.enabled():
        return False, "self-improvement disabled"
    active = controller or si.shared()
    p = path if path is not None else _store_path()
    apply_prov = {
        "signature": proposal.signature, "rationale": proposal.rationale,
        "hypothesis": proposal.hypothesis or None,
        "matter_id": matter_id, "owner_scope": owner_scope,
        "held_out_delta": round(vr.held_out_delta, 4), "samples": vr.samples,
        "held_in_samples": vr.held_in_samples,
        "held_out_samples": vr.held_out_samples,
        "effect_ci_low": (round(vr.effect_ci_low, 6)
                          if vr.effect_ci_low is not None else None)}
    if canary:  # only stamp the flag when staging -- non-canary leaves meta clean
        apply_prov["canary"] = True

    store_key = _promotion_store_key(
        proposal.model_id, proposal.context,
        matter_id=matter_id, owner_scope=owner_scope,
    )
    with _lock, cross_process_lock(p), _store_rmw_lock(p):
        try:
            before_store = _load_addenda_strict(p)
            before_revision = _addenda_artifact_revision(p, before_store)

            def _inspect(identity: str):
                if identity != before_revision.identity:
                    raise si.PromotionLedgerError("unexpected artifact identity")
                return before_revision

            # Resolve a crash-left PREPARE before admitting another write to the
            # same full-store artifact. The addenda lock stabilizes inspection.
            active.recover_promotions(
                _inspect, artifact_identity=before_revision.identity)
            after_store = dict(before_store)
            after_store[store_key] = _compose_addendum(
                store_key, before_store.get(store_key, ""), proposal.addendum_line)
            after_revision = _addenda_artifact_revision(p, after_store)
        except Halted:
            raise
        except Exception as exc:
            log.warning(
                "self_harness: cannot establish addenda transaction baseline: %s", exc)
            return False, "addenda state or promotion recovery is unavailable"
        if before_revision == after_revision:
            return False, "candidate does not change the deployed addendum"

        before_meta = load_line_meta(p)
        rollback = _cas_rollback_handle(
            p, before_store=before_store, after_store=after_store,
            store_key=store_key, before_meta=before_meta)
        receipt_evidence = _promotion_receipt_evidence(proposal)
        cand = si.Candidate(
            rung="prompt",
            summary=("self-harness prompt candidate sha256:"
                     f"{receipt_evidence['addendum_sha256']}"),
            baseline_score=vr.baseline_score,
            candidate_score=vr.candidate_score,
            samples=vr.samples,
            effect_ci_low=vr.effect_ci_low,
            payload={
                "model_id": proposal.model_id,
                "matter_id": matter_id,
                "owner_scope": owner_scope,
                "before_sha256": before_revision.sha256,
                "after_sha256": after_revision.sha256,
                **receipt_evidence,
            },
            rollback=rollback,
            provenance={
                "source": "self_harness",
                "matter_id": matter_id, "owner_scope": owner_scope,
                "before_sha256": before_revision.sha256,
                "after_sha256": after_revision.sha256,
                **receipt_evidence,
            },
            audit_payload={
                "_audit_agent": "self_harness",
                "model_id": proposal.model_id,
                "phase": "apply",
                "matter_id": matter_id,
                "owner_scope": owner_scope,
                "before_sha256": before_revision.sha256,
                "after_sha256": after_revision.sha256,
                "held_out_delta": round(vr.held_out_delta, 4),
                "held_in_samples": vr.held_in_samples,
                "held_out_samples": vr.held_out_samples,
                **receipt_evidence,
            },
        )
        # Close the validation-to-apply race: a HALT that lands while the
        # evaluator or authorization receipt is running must refuse the
        # privileged transition before PREPARE or artifact mutation.
        check_learning_halt("self_harness", "promotion")
        preparation = si.prepare_promotion(
            cand, before=before_revision, after=after_revision,
            controller=active)
        if not preparation.ok:
            return False, preparation.blocking_reason or "refused"

        if preparation.committed:
            current = _addenda_artifact_revision(p, _load_addenda_strict(p))
            if current != after_revision:
                return False, "committed promotion artifact is not deployed"
        elif preparation.needs_apply:
            try:
                check_learning_halt("self_harness", "apply")
                authorization = si.authorize_prepared(
                    preparation,
                    artifact=before_revision,
                    controller=active,
                )
                if not authorization.ok:
                    return False, authorization.blocking_reason or "refused"
                written = _apply_addendum_locked(
                    proposal, p, before_store, provenance=apply_prov,
                    store_key=store_key)
                observed = _addenda_artifact_revision(p, _load_addenda_strict(p))
                if written != after_store or observed != after_revision:
                    raise RuntimeError("addenda CAS readback differs from prepared intent")
            except Halted:
                # HALT is not an evaluation rejection.  Close the durable
                # PREPARE when the exact before artifact remains live, retain
                # it for recovery otherwise, and always propagate the distinct
                # interlock signal to the scheduler.
                try:
                    observed = _addenda_artifact_revision(
                        p, _load_addenda_strict(p))
                    aborted = si.abort_prepared(
                        preparation, artifact=observed,
                        reason="HALT before addenda artifact application",
                        controller=active)
                    if "in doubt" in (aborted.blocking_reason or "").lower():
                        log.warning(
                            "self_harness: HALT left addenda transaction in doubt")
                except Exception:
                    log.warning(
                        "self_harness: HALT transaction cleanup failed; recovery required",
                        exc_info=True)
                raise
            except Exception as exc:
                log.warning("self_harness: addenda transaction apply failed: %s", exc)
                try:
                    observed = _addenda_artifact_revision(p, _load_addenda_strict(p))
                    aborted = si.abort_prepared(
                        preparation, artifact=observed,
                        reason="addenda artifact application failed", controller=active)
                    return False, aborted.blocking_reason or "artifact application failed"
                except Exception:
                    return False, "artifact application is in doubt; recovery required"
            verdict = si.commit_prepared(
                preparation, artifact=observed, controller=active)
            if not verdict.ok:
                return False, verdict.blocking_reason or "promotion commit failed"
        else:
            return False, "promotion transaction is not applicable"

    return True, "promoted"


__all__ = [
    "enabled", "recall_addendum", "load_addenda",
    "list_learned", "forget_addendum", "retire_stale",
    "load_line_meta", "line_provenance", "note_recall",
    "note_outcome", "line_efficacy", "review_efficacy",
    "mark_canary", "list_canaries", "review_canaries", "review_relapses",
    "find_conflicts", "detect_store_conflicts", "llm_conflict_classifier",
    "FailureSignature", "mine_failures", "count_eligible", "semantic_similarity",
    "HarnessProposal", "ProposeFn", "propose_addendum", "llm_proposer",
    "ValidationResult", "ScoreFn", "validate_proposal",
    "SelfHarnessReport", "run_self_harness",
]
