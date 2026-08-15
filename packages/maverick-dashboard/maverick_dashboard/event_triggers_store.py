"""Dashboard-owned registry of polled event triggers.

An event trigger binds a saved template to an :class:`~maverick.automation_events.EventSource`
(plus its config and operator-set default params). A scheduled poll asks the
source for items newer than the stored ``cursor``; each fires the template as a
goal. Persisted to ``~/.maverick/dashboard-event-triggers.toml`` -- dashboard-
owned like the webhook triggers, deliberately NOT merged into the kernel config.

Mirrors ``triggers_store``'s atomic, 0600, cross-process-locked write and its
JSON-in-TOML encoding for the nested ``params``/``config`` tables and the
mutable ``cursor``.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

from maverick import config

# Reuse the webhook store's slug (identical charset/length rules) so the two
# can't drift.
from .triggers_store import (
    slugify,
    snapshot_from_template,
    snapshot_revision,
    validated_template_snapshot,
)

_LOCK = threading.Lock()

# URL/TOML-safe slug, same shape as webhook trigger names.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def _locked():
    from contextlib import ExitStack

    from maverick.file_lock import cross_process_lock
    stack = ExitStack()
    stack.enter_context(_LOCK)
    stack.enter_context(cross_process_lock(_path(), strict=True))
    return stack


def _tomllib():
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
        import tomli as tomllib  # type: ignore
    return tomllib


def _path():
    return config.dashboard_overrides_path().parent / "dashboard-event-triggers.toml"


def _decode_obj(raw: object) -> dict:
    try:
        val = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def _record_key(name: str, tenant: str) -> str:
    """Return a stable TOML table key without exposing the tenant identifier."""
    if not tenant:
        return name
    digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:32]
    return f"{name}--{digest}"


def _tenant_floor(tenant: str | None) -> str | None:
    """Resolve an omitted tenant to the active namespace, when one is pinned."""
    if tenant is not None:
        return str(tenant)
    from maverick.paths import current_tenant_id

    current = current_tenant_id()
    return str(current) if current else None


def _matching(triggers: list[dict], name: str, tenant: str | None) -> list[dict]:
    floor = _tenant_floor(tenant)
    matches = [t for t in triggers if t["name"] == name]
    if floor is not None:
        return [t for t in matches if t.get("tenant", "") == floor]
    # Backward-compatible for callers outside a tenant context, but never pick
    # arbitrarily once two tenants own the same public trigger name.
    return matches if len(matches) <= 1 else []


def list_triggers() -> list[dict]:
    """Every registered event trigger as a dict (params/config decoded)."""
    p = _path()
    if not p.exists():
        return []
    try:
        with open(p, "rb") as f:
            raw = _tomllib().load(f)
    except (OSError, ValueError):
        return []
    out: list[dict] = []
    for table_key, t in (raw.get("trigger") or {}).items():
        if not isinstance(t, dict):
            continue
        try:
            template_snapshot = json.loads(t.get("template_snapshot") or "null")
        except (TypeError, ValueError):
            template_snapshot = None
        out.append({
            # Old files used the public name as the table key. New files keep
            # the public name explicitly because the table key also namespaces
            # it by tenant.
            "name": str(t.get("name") or table_key),
            "template": str(t.get("template") or ""),
            "flow": str(t.get("flow") or ""),
            "flow_owner": str(t.get("flow_owner") or ""),
            "flow_revision": str(t.get("flow_revision") or ""),
            "template_snapshot": (
                template_snapshot if isinstance(template_snapshot, dict) else None
            ),
            "template_revision": str(t.get("template_revision") or ""),
            "source": str(t.get("source") or ""),
            "config": _decode_obj(t.get("config")),
            "params": {str(k): str(v) for k, v in _decode_obj(t.get("params")).items()},
            "cursor": str(t.get("cursor") or ""),
            "owner": str(t.get("owner") or ""),
            "tenant": str(t.get("tenant") or ""),
            "interval_seconds": int(t.get("interval_seconds") or 0),
            "created": float(t.get("created") or 0.0),
        })
    out.sort(key=lambda t: (t["created"], t["name"]))
    return out


def get_trigger(name: str, *, tenant: str | None = None) -> dict | None:
    matches = _matching(list_triggers(), name, tenant)
    return matches[0] if len(matches) == 1 else None


def _dump(triggers: list[dict]) -> str:
    lines = [
        "# Dashboard-managed polled event triggers. Edit via the dashboard,",
        "# not by hand. Your config.toml is never touched.",
        "",
    ]
    for t in sorted(triggers, key=lambda x: (x.get("tenant", ""), x["name"])):
        lines.append(f"[trigger.{_record_key(t['name'], t.get('tenant', ''))}]")
        lines.append(f"name = {json.dumps(str(t['name']))}")
        lines.append(f"template = {json.dumps(str(t['template']))}")
        lines.append(f"flow = {json.dumps(str(t.get('flow') or ''))}")
        lines.append(f"flow_owner = {json.dumps(str(t.get('flow_owner') or ''))}")
        lines.append(f"flow_revision = {json.dumps(str(t.get('flow_revision') or ''))}")
        lines.append(
            "template_snapshot = "
            + json.dumps(json.dumps(t.get("template_snapshot"), sort_keys=True))
        )
        lines.append(
            f"template_revision = {json.dumps(str(t.get('template_revision') or ''))}"
        )
        lines.append(f"source = {json.dumps(str(t.get('source') or ''))}")
        lines.append(f"config = {json.dumps(json.dumps(t.get('config') or {}, sort_keys=True))}")
        lines.append(f"params = {json.dumps(json.dumps(t.get('params') or {}, sort_keys=True))}")
        lines.append(f"cursor = {json.dumps(str(t.get('cursor') or ''))}")
        lines.append(f"owner = {json.dumps(str(t.get('owner') or ''))}")
        lines.append(f"tenant = {json.dumps(str(t.get('tenant') or ''))}")
        lines.append(f"interval_seconds = {int(t.get('interval_seconds') or 0)}")
        lines.append(f"created = {float(t.get('created') or 0.0)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write(triggers: list[dict]) -> None:
    from maverick.file_lock import atomic_write_text
    atomic_write_text(_path(), _dump(triggers))


def set_trigger(name: str, template: str, source: str, *, config: dict | None = None,
                params: dict | None = None, owner: str = "", cursor: str = "",
                interval_seconds: int = 0, tenant: str = "", flow: str = "",
                flow_owner: str = "", flow_revision: str = "",
                template_snapshot: dict | None = None) -> dict:
    """Create or replace an event trigger (by slugified name). Raises ValueError
    on an un-sluggable name. Preserves the existing cursor on replace unless one
    is passed, so re-saving a trigger doesn't replay its whole backlog.
    ``interval_seconds`` is the per-trigger poll cadence (0 = the scheduler
    default). ``tenant`` binds the trigger to the tenant that created it, so a
    polled OAuth source reads that tenant's vault (never a cross-tenant token).
    ``flow`` targets a flow instead of a ``template`` (the two are exclusive)."""
    slug = slugify(name)
    if not _NAME_RE.match(slug):
        raise ValueError("trigger name must contain a letter or digit (a-z, 0-9, -)")
    if flow and template_snapshot is not None:
        raise ValueError("flow triggers cannot include a template snapshot")
    # Direct store callers get the same creation-time pinning as the REST API
    # whenever their target exists. A missing target remains a legacy/unbound
    # row and is refused by the fire path until it is re-saved safely.
    if template and template_snapshot is None:
        from maverick.paths import reset_tenant, set_tenant
        from maverick.templates import load_template

        token = set_tenant(str(tenant) or None)
        try:
            template_snapshot = snapshot_from_template(load_template(template))
        except (FileNotFoundError, ValueError):
            template_snapshot = None
        finally:
            reset_tenant(token)
    normalised_snapshot = None
    template_rev = ""
    if template_snapshot is not None:
        normalised_snapshot = validated_template_snapshot(template_snapshot)
        if normalised_snapshot["name"] != str(template):
            raise ValueError("template snapshot does not match the trigger target")
        template_rev = snapshot_revision(normalised_snapshot)
    with _locked():
        existing = list_triggers()
        tenant = str(tenant)
        kept = [
            t for t in existing
            if not (t["name"] == slug and t.get("tenant", "") == tenant)
        ]
        prior = next(
            (
                t for t in existing
                if t["name"] == slug and t.get("tenant", "") == tenant
            ),
            None,
        )
        rec = {
            "name": slug,
            "template": str(template),
            "flow": str(flow),
            "flow_owner": str(flow_owner),
            "flow_revision": str(flow_revision),
            "template_snapshot": normalised_snapshot,
            "template_revision": template_rev,
            "source": str(source),
            "config": dict(config or {}),
            "params": {str(k): str(v) for k, v in (params or {}).items()},
            "cursor": cursor or (prior["cursor"] if prior else ""),
            # Preserve the prior owner on a re-save that doesn't pass one, like
            # cursor/created, so an update can't silently orphan the trigger.
            "owner": str(owner) if owner else (prior["owner"] if prior else ""),
            "tenant": tenant,
            # The create form always submits this field, so take it verbatim --
            # that's what lets an operator clear a custom cadence back to the
            # scheduler default (0) by blanking the box.
            "interval_seconds": int(interval_seconds),
            "created": (prior["created"] if prior else time.time()),
        }
        kept.append(rec)
        _write(kept)
    return rec


def set_cursor(name: str, cursor: str, *, tenant: str | None = None) -> bool:
    """Advance a trigger's cursor after a poll. Returns False if it's gone.
    Skips the rewrite when the cursor is unchanged, so an idle poll (no new
    items) doesn't rewrite the whole store every tick."""
    cursor = str(cursor)
    with _locked():
        triggers = list_triggers()
        matches = _matching(triggers, name, tenant)
        if len(matches) != 1:
            return False
        match = matches[0]
        if match["cursor"] != cursor:
            match["cursor"] = cursor
            _write(triggers)
        return True


def delete_trigger(
    name: str,
    owner: str | None = None,
    *,
    tenant: str | None = None,
) -> bool:
    """Remove a trigger. When ``owner`` is given, only if it belongs to that
    owner (ownership check + removal under one lock)."""
    with _locked():
        triggers = list_triggers()
        matches = _matching(triggers, name, tenant)
        if len(matches) != 1:
            return False
        match = matches[0]
        if owner is not None and match.get("owner", "") != owner:
            return False
        _write([t for t in triggers if t is not match])
    return True
