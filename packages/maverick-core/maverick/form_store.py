"""A tiny append-only store of hosted-form submissions.

A ``form`` event trigger points a public web form (or any POST) at
``/form/<token>``; each submission is appended here under that token, and the
``form`` :class:`~maverick.automation_events.EventSource` polls it so the
submission fires a flow -- the push-to-poll bridge that lets a form be a
first-class trigger without a bespoke inbound queue.

Best-effort NDJSON, 0600, bounded, in the tenant data dir. A submission is one
line ``{"seq", "ts", "fields"}``; ``seq`` is a per-token monotonic counter used
as the poll cursor (deletion-safe, unlike id-walking).
"""
from __future__ import annotations

import json
import os
import re

_MAX_FIELDS = 50          # per submission
_MAX_VALUE = 2000         # per field value
_MAX_LINES = 5000         # keep the tail bounded


def _safe_token(token: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(token or "").strip())
    return s[:120] or "form"


def _path(token: str):
    from .paths import data_dir
    return data_dir("forms") / f"{_safe_token(token)}.ndjson"


def _bound_fields(fields: dict) -> dict:
    out: dict = {}
    for k, v in (fields or {}).items():
        if len(out) >= _MAX_FIELDS:
            break
        out[str(k)[:200]] = ("" if v is None else str(v))[:_MAX_VALUE]
    return out


def append(token: str, fields: dict, *, ts: float = 0.0) -> int:
    """Append one submission for ``token``; return its ``seq`` (1-based). Fields
    are stringified + bounded. Best-effort -- returns 0 on write failure."""
    p = _path(token)
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        seq = _count(p) + 1
        line = json.dumps({"seq": seq, "ts": float(ts), "fields": _bound_fields(fields)},
                          separators=(",", ":"), sort_keys=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return seq
    except OSError:  # pragma: no cover -- best-effort
        return 0


def _count(p) -> int:
    try:
        with open(p, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def since(token: str, cursor: str) -> tuple[list[dict], str]:
    """Submissions with ``seq`` greater than ``cursor`` (empty cursor = baseline:
    fire nothing, just record the high-water mark). Returns ``(events, cursor)``
    where each event is the flat ``fields`` dict plus ``_seq``/``_ts``."""
    p = _path(token)
    rows: list[dict] = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    continue
    except OSError:
        # A non-empty baseline cursor even for a missing store ("0"), so the
        # first submission after the trigger is armed locks in and then fires --
        # returning "" would leave the trigger's cursor unadvanced and swallow it.
        return [], cursor or "0"
    if not rows:
        return [], cursor or "0"
    highest = str(rows[-1].get("seq") or len(rows))
    try:
        after = int(cursor) if cursor else None
    except (TypeError, ValueError):
        after = None
    if after is None:
        return [], highest                       # baseline only
    fresh = [r for r in rows if int(r.get("seq") or 0) > after]
    if not fresh:
        return [], highest
    events = []
    for r in fresh:
        ev = dict(r.get("fields") or {})
        ev["_seq"], ev["_ts"] = r.get("seq"), r.get("ts")
        events.append(ev)
    return events, str(fresh[-1].get("seq") or highest)


__all__ = ["append", "since"]
