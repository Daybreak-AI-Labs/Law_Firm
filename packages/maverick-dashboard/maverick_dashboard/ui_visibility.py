"""Role-based UI visibility: which dashboard pages each RBAC role sees.

Two deliberately distinct layers:

  * ``floor`` -- the page's hard *security* requirement, mirroring the
    permission the route itself enforces (``/audit`` requires "audit",
    ``/settings`` requires "admin", ...). Visibility overrides can NEVER
    unlock a page past its floor: a role whose permissions don't include the
    floor cannot be shown the page, so presentation policy can't outrun authz.
  * ``default`` -- the page's *presentation* default: which permission a role
    must hold to see the page out of the box. This is what keeps a viewer's
    sidebar to read-only pages and an operator's free of admin plumbing. An
    admin may override it per role (within the floor) from Settings.

Safety invariants (mirror ``maverick_dashboard.rbac``):
  * Auth OFF (role ``None``) -> everything visible, nothing enforced -- the
    local single-user operator stays omnipotent, exactly as before.
  * Admins can never lose ``/settings`` or ``/users`` (``LOCKED``): a bad
    matrix save must not be able to lock every admin out of the controls
    that would undo it.
  * The store is one GLOBAL control-plane file, never per-tenant:
    ``~/.maverick/dashboard-ui-visibility.json`` (0600), shaped
    ``{path: {role: bool}}`` and holding only differences from the defaults,
    so un-overridden cells follow default changes across releases.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import rbac

# One registry drives the sidebar nav, the enforcement dependency, and the
# Settings matrix -- order here IS the nav order. ``floor`` mirrors the
# route-enforced gate ("view" when the GET route has none); ``default`` is the
# permission a role must hold to see the page before any admin override.
# ``in_nav: False`` keeps a page governed (enforced + in the Settings matrix)
# but out of the sidebar -- secondary composers/detail surfaces reached from a
# primary page's own links, so the nav stays one entry per job.
PAGES: tuple[dict, ...] = (
    # -- Operate --------------------------------------------------------------
    {"path": "/start", "label": "Get started", "group": "Operate", "icon": "check",
     "floor": "view", "default": "operate"},
    {"path": "/chat", "label": "Chat", "group": "Operate", "icon": "chat",
     "floor": "view", "default": "operate"},
    {"path": "/goals", "label": "Goals", "group": "Operate", "icon": "target",
     "floor": "view", "default": "view"},
    # Alternate goal composers, linked from the Goals page toolbar.
    {"path": "/goal-builder", "label": "Quick Goal", "group": "Operate", "icon": "builder",
     "floor": "view", "default": "operate", "in_nav": False},
    {"path": "/graph-editor", "label": "Goal Map", "group": "Operate", "icon": "graph",
     "floor": "view", "default": "operate", "in_nav": False},
    {"path": "/projects", "label": "Projects", "group": "Operate", "icon": "fleets",
     "floor": "view", "default": "view"},
    {"path": "/deliverables", "label": "Deliverables", "group": "Operate", "icon": "templates",
     "floor": "view", "default": "view"},
    {"path": "/agents", "label": "Agent Factory", "group": "Operate", "icon": "agents",
     "floor": "view", "default": "operate"},
    {"path": "/roles", "label": "Roles", "group": "Operate", "icon": "roles",
     "floor": "view", "default": "operate"},
    {"path": "/workforce", "label": "Workforce", "group": "Operate", "icon": "agents",
     "floor": "view", "default": "view"},
    # Flows: the redesigned builder (/flows/designer) is the primary sidebar
    # entry. The saved-workflows index and the older NL builder are reached from
    # the Flows builder / Automations / Agents pages rather than the sidebar, so
    # the nav shows one clear "Flows" job instead of three near-duplicates.
    {"path": "/flows/designer", "label": "Flows", "group": "Operate", "icon": "workflow",
     "floor": "view", "default": "operate"},
    {"path": "/workflows", "label": "Workflows", "group": "Operate", "icon": "workflow",
     "floor": "view", "default": "operate", "in_nav": False},
    {"path": "/workflow-builder", "label": "Workflow Builder", "group": "Operate",
     "icon": "workflow", "floor": "view", "default": "operate", "in_nav": False},
    {"path": "/automations", "label": "Automations", "group": "Operate", "icon": "clock",
     "floor": "view", "default": "operate"},
    {"path": "/fleets", "label": "Fleets", "group": "Operate", "icon": "fleets",
     "floor": "view", "default": "operate"},
    # -- Observe --------------------------------------------------------------
    {"path": "/overview", "label": "Overview", "group": "Observe", "icon": "overview",
     "floor": "view", "default": "view"},
    {"path": "/ekko", "label": "Ekko", "group": "Observe", "icon": "eye",
     "floor": "operate", "default": "operate"},
    {"path": "/oversight", "label": "Oversight", "group": "Observe", "icon": "eye",
     "floor": "view", "default": "view"},
    {"path": "/discovery", "label": "Discovery", "group": "Observe", "icon": "overview",
     "floor": "view", "default": "view"},
    # Counterfactual review: forked runs and their branches under one root.
    {"path": "/run-tree", "label": "Run Tree", "group": "Observe", "icon": "eye",
     "floor": "view", "default": "view"},
    {"path": "/spend", "label": "Spend", "group": "Observe", "icon": "spend",
     "floor": "view", "default": "view"},
    # ROI: money saved vs the client's own human-cost assumptions.
    {"path": "/savings", "label": "Savings", "group": "Observe", "icon": "spend",
     "floor": "view", "default": "view"},
    # The monthly statement behind Spend's "Billing statement" link.
    {"path": "/billing", "label": "Billing", "group": "Observe", "icon": "spend",
     "floor": "view", "default": "view", "in_nav": False},
    {"path": "/providers", "label": "Providers", "group": "Observe", "icon": "providers",
     "floor": "view", "default": "view"},
    {"path": "/benchmarks", "label": "Benchmarks", "group": "Observe", "icon": "bench",
     "floor": "view", "default": "view"},
    {"path": "/flows/analytics", "label": "Flow Analytics", "group": "Observe", "icon": "bench",
     "floor": "view", "default": "view"},
    {"path": "/learning", "label": "Learning", "group": "Observe", "icon": "learned",
     "floor": "view", "default": "view"},
    # The acquired-capabilities record, linked from the Learning page.
    {"path": "/learned", "label": "Learned", "group": "Observe", "icon": "learned",
     "floor": "view", "default": "view", "in_nav": False},
    # Per-user goal recordings (owner-scoped) -- an operator review surface.
    {"path": "/walkthroughs", "label": "Walkthroughs", "group": "Observe",
     "icon": "walkthroughs", "floor": "view", "default": "operate"},
    {"path": "/facts", "label": "Facts", "group": "Observe", "icon": "facts",
     "floor": "view", "default": "view"},
    {"path": "/plan-tree-3d", "label": "3D Plan View", "group": "Observe", "icon": "cube",
     "floor": "view", "default": "view"},
    # -- Govern ---------------------------------------------------------------
    {"path": "/approvals", "label": "Approvals", "group": "Govern", "icon": "check",
     "floor": "operate", "default": "operate"},
    # The Privacy workspace: the module view over assessment records (PIA /
    # AIRA / vendor risk / framework readiness). Operate floor — records carry
    # answer bodies and reviewer threads, a tier above viewer summaries.
    {"path": "/privacy", "label": "Privacy", "group": "Govern", "icon": "shield",
     "floor": "operate", "default": "operate"},
    # Security is the department workspace. The two hunter consoles are
    # governed child surfaces linked from it, so the sidebar keeps one clear
    # entry while role visibility still protects every direct URL.
    {"path": "/security", "label": "Security & GRC", "group": "Govern",
     "icon": "shield", "floor": "operate", "default": "operate"},
    {"path": "/security/threats", "label": "Platform Threat Hunter",
     "group": "Govern", "icon": "shield", "floor": "operate",
     "default": "operate", "in_nav": False},
    {"path": "/security/soc", "label": "Environment Threat Hunter",
     "group": "Govern", "icon": "shield", "floor": "operate",
     "default": "operate", "in_nav": False},
    # The Finance workspace: the same chassis over the finance assessment
    # types (SOX control / fraud risk / ITGC / credit risk / close readiness).
    # "Financial Risk", not "Finance": this workspace is the SOX / fraud /
    # ITGC / credit-risk control record, not day-to-day finance operations.
    {"path": "/finance", "label": "Financial Risk", "group": "Govern",
     "icon": "spend", "floor": "operate", "default": "operate"},
    # The partner fleet console: client deployments a partner operates.
    # Operate floor -- rows carry probe endpoints; mutations are admin (API).
    {"path": "/partner", "label": "Partner fleet", "group": "Govern",
     "icon": "graph", "floor": "operate", "default": "operate"},
    # Permissions is deployment plumbing, not day-to-day governance work --
    # it lives with the rest of administration.
    {"path": "/permissions", "label": "Permissions", "group": "Admin",
     "icon": "key", "floor": "view", "default": "operate"},
    {"path": "/simulate", "label": "Simulate", "group": "Govern", "icon": "graph",
     "floor": "view", "default": "operate"},
    # Redaction workbench (its preview API requires operate); reached from
    # goal/trajectory surfaces rather than the sidebar.
    {"path": "/redact", "label": "Redaction", "group": "Govern", "icon": "shield",
     "floor": "view", "default": "operate", "in_nav": False},
    {"path": "/safety", "label": "Safety", "group": "Govern", "icon": "shield",
     "floor": "view", "default": "view"},
    # Privacy / security / AI-risk assessments of every agent & flow: auto-drafted
    # from the declared capability surface, reviewed + signed off, re-assessed on
    # change. Reading is audit-level; drafting/reviewing is admin (mutation API).
    {"path": "/assessments", "label": "Assessments", "group": "Govern", "icon": "shield",
     "floor": "audit", "default": "audit"},
    {"path": "/compliance", "label": "Compliance", "group": "Govern", "icon": "compliance",
     "floor": "view", "default": "audit"},
    {"path": "/compartments", "label": "Compartments", "group": "Govern", "icon": "compartments",
     "floor": "view", "default": "view"},
    {"path": "/audit", "label": "Audit", "group": "Govern", "icon": "audit",
     "floor": "audit", "default": "audit"},
    {"path": "/replay", "label": "Replay", "group": "Govern", "icon": "audit",
     "floor": "view", "default": "audit"},
    {"path": "/trust", "label": "Agent Trust", "group": "Govern", "icon": "agents",
     "floor": "view", "default": "view"},
    # Bring-your-own-agent console: enroll + credential + govern agents built
    # on other platforms. Reading the roster is a governance view; every
    # mutation (enroll / mint / revoke) is admin-gated at the API.
    {"path": "/external-agents", "label": "External Agents", "group": "Govern",
     "icon": "agents", "floor": "view", "default": "view"},
    # -- Extend ---------------------------------------------------------------
    {"path": "/skills", "label": "Skills", "group": "Extend", "icon": "skills",
     "floor": "view", "default": "operate"},
    {"path": "/store", "label": "Store", "group": "Extend", "icon": "store",
     "floor": "view", "default": "operate"},
    {"path": "/templates", "label": "Templates", "group": "Extend", "icon": "templates",
     "floor": "view", "default": "operate"},
    {"path": "/tools", "label": "Tools", "group": "Extend", "icon": "tools",
     "floor": "view", "default": "operate"},
    # Connections hold live SaaS credentials/tokens, so managing them is admin
    # plumbing, not an everyday operator surface. Builders still *reference* an
    # existing connection by name in a flow (GET /api/v1/connections stays
    # operate-readable); only create/rotate/delete/test are admin-gated.
    {"path": "/connections", "label": "Connections", "group": "Admin", "icon": "key",
     "floor": "admin", "default": "admin"},
    {"path": "/plugins", "label": "Plugins", "group": "Extend", "icon": "plugins",
     "floor": "view", "default": "operate"},
    # Output-style customization: mutations are operate-gated, so it lives with
    # the other operator-facing extension surfaces, not admin plumbing.
    {"path": "/styles", "label": "Response Styles", "group": "Extend", "icon": "roles",
     "floor": "view", "default": "operate"},
    {"path": "/mcp", "label": "Tool Servers", "group": "Extend", "icon": "mcp",
     "floor": "admin", "default": "admin"},
    {"path": "/channels", "label": "Channels", "group": "Extend", "icon": "channels",
     "floor": "admin", "default": "admin"},
    # -- Admin ----------------------------------------------------------------
    {"path": "/settings", "label": "Settings", "group": "Admin", "icon": "settings",
     "floor": "admin", "default": "admin"},
    {"path": "/users", "label": "Users", "group": "Admin", "icon": "users",
     "floor": "admin", "default": "admin"},
    {"path": "/tenants", "label": "Tenants", "group": "Admin", "icon": "tenants",
     "floor": "admin", "default": "admin"},
    {"path": "/cache", "label": "Cache", "group": "Admin", "icon": "cache",
     "floor": "view", "default": "admin"},
    {"path": "/embed-demo", "label": "Embed Analytics", "group": "Admin", "icon": "embed",
     "floor": "view", "default": "admin"},
)

# Moderate sidebar declutter: keep a focused set of everyday destinations in the
# rail; fold the rest OUT of the sidebar (in_nav=False) while leaving them fully
# reachable in-page and by URL. One place to adjust the balance — move a path
# out of this set to bring its tab back. (Pages already in_nav=False in the
# registry above, e.g. builders reached from a parent page, are unaffected.)
_NAV_FOLD: frozenset[str] = frozenset({
    # Operate — reached from Goals / Agent Factory / Workforce
    "/start", "/projects", "/roles", "/fleets",
    # Observe — analytical/advanced views reached from Overview & friends
    "/discovery", "/providers", "/benchmarks", "/flows/analytics",
    "/walkthroughs", "/facts", "/plan-tree-3d",
    # Govern — advanced governance surfaces
    "/simulate", "/compartments", "/replay", "/trust",
    # Extend — secondary catalogs
    "/store", "/templates", "/tools", "/styles",
    # Admin — rarely-touched plumbing
    "/cache", "/embed-demo",
})
for _pg in PAGES:
    if _pg["path"] in _NAV_FOLD:
        _pg["in_nav"] = False

_PAGES_BY_PATH = {p["path"]: p for p in PAGES}

# Lockout guard: the pages an admin needs to *undo* a bad matrix save. Always
# visible + reachable for the admin role, no matter what the store says.
LOCKED = frozenset({("/settings", "admin"), ("/users", "admin")})

# Serializes the store's load-modify-save in-process; cross_process_lock in
# _locked() extends it across processes (multiple dashboard workers).
_VIS_LOCK = threading.Lock()


def _locked(path: Path):
    from contextlib import ExitStack

    from maverick.file_lock import cross_process_lock
    stack = ExitStack()
    stack.enter_context(_VIS_LOCK)
    stack.enter_context(cross_process_lock(path))
    return stack


def store_path() -> Path:
    from maverick.paths import maverick_home

    return maverick_home() / "dashboard-ui-visibility.json"


def overrides() -> dict[str, dict[str, bool]]:
    """Validated ``{path: {role: bool}}`` admin overrides (diffs from default).

    Re-validated on read so a hand-edited / corrupt store can't inject an
    unknown page, an unknown role, or a non-bool value.
    """
    p = store_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, bool]] = {}
    for path, cells in data.items():
        if path not in _PAGES_BY_PATH or not isinstance(cells, dict):
            continue
        clean = {
            role: v for role, v in cells.items()
            if role in rbac.ROLES and isinstance(v, bool)
        }
        if clean:
            out[path] = clean
    return out


def _write(data: dict[str, dict[str, bool]]) -> None:
    from maverick.file_lock import atomic_write_text
    atomic_write_text(store_path(), json.dumps(data, indent=2, sort_keys=True))


def clear_overrides() -> None:
    """Reset every page to its default visibility."""
    with _locked(store_path()):
        _write({})


def eligible(page: dict, role: str) -> bool:
    """Whether ``role``'s permissions reach the page's security floor at all."""
    return page["floor"] in rbac.permissions_for(role)


def default_visible(page: dict, role: str) -> bool:
    """The page's out-of-the-box visibility for ``role`` (no overrides)."""
    return page["default"] in rbac.permissions_for(role)


def is_visible(page: dict, role: str | None) -> bool:
    """Whether ``role`` sees (and may load) ``page``.

    ``None`` = auth off = single-user local mode: always visible. Otherwise:
    locked pages are always visible to their locked role; a role that lacks
    the floor permission never sees the page; else the admin override wins,
    falling back to the default.
    """
    if role is None:
        return True
    if (page["path"], role) in LOCKED:
        return True
    if not eligible(page, role):
        return False
    cell = overrides().get(page["path"], {}).get(role)
    if cell is not None:
        return cell
    return default_visible(page, role)


def set_visibility(path: str, role: str, visible: bool) -> None:
    """Set one page x role cell (stored only when it differs from the default).

    Raises ``ValueError`` for an unknown page/role, a locked cell, or an
    attempt to show a page past the role's security floor.
    """
    page = _PAGES_BY_PATH.get(path)
    if page is None:
        raise ValueError("unknown page")
    if role not in rbac.ROLES:
        raise ValueError("unknown role")
    if (path, role) in LOCKED:
        raise ValueError("this page is always available to admins")
    if visible and not eligible(page, role):
        raise ValueError("role permissions do not reach this page")
    with _locked(store_path()):
        data = overrides()
        if visible == default_visible(page, role):
            cells = data.get(path)
            if cells:
                cells.pop(role, None)
                if not cells:
                    data.pop(path, None)
        else:
            data.setdefault(path, {})[role] = visible
        _write(data)


def apply_selection(checked: set[str]) -> None:
    """Persist a full Settings-matrix submission in one write.

    ``checked`` holds ``"role:path"`` tokens for every ticked checkbox; every
    eligible, unlocked cell absent from it is hidden. Only differences from
    the defaults are stored, so a later release's default changes still flow
    through un-overridden cells.
    """
    data: dict[str, dict[str, bool]] = {}
    for page in PAGES:
        for role in rbac.ROLES:
            if (page["path"], role) in LOCKED or not eligible(page, role):
                continue
            visible = f"{role}:{page['path']}" in checked
            if visible != default_visible(page, role):
                data.setdefault(page["path"], {})[role] = visible
    with _locked(store_path()):
        _write(data)


def page_for(path: str) -> dict | None:
    """The registry page governing ``path`` (exact or longest segment-prefix
    match, so ``/goals/3/plan`` follows ``/goals``), or None if unregistered."""
    best = None
    for p in PAGES:
        root = p["path"]
        if path == root or path.startswith(root + "/"):
            if best is None or len(root) > len(best["path"]):
                best = p
    return best


def nav_groups(role: str | None) -> list[dict]:
    """The sidebar nav for ``role``: ``[{label, links: [{href, label, icon}]}]``.

    Pages the role doesn't see are dropped; a group with nothing left is
    dropped whole. ``role=None`` (auth off) returns the full nav. Pages marked
    ``in_nav: False`` stay governed but never render a sidebar entry.
    """
    ov = overrides() if role is not None else {}
    groups: list[dict] = []
    for page in PAGES:
        if not page.get("in_nav", True):
            continue
        if role is not None:
            if (page["path"], role) not in LOCKED:
                if not eligible(page, role):
                    continue
                cell = ov.get(page["path"], {}).get(role)
                shown = cell if cell is not None else default_visible(page, role)
                if not shown:
                    continue
        # Merge by group label (first-seen order), not adjacency: a page whose
        # group differs from its list neighbours (e.g. an entry re-homed to
        # Admin) must join its group, not split it into a duplicate section.
        for g in groups:
            if g["label"] == page["group"]:
                break
        else:
            g = {"label": page["group"], "links": []}
            groups.append(g)
        g["links"].append(
            {"href": page["path"], "label": page["label"], "icon": page["icon"]}
        )
    return groups


def matrix() -> list[dict]:
    """The Settings-page editing matrix, grouped like the nav.

    Each page row carries one cell per role (``rbac.ROLES`` order):
    ``eligible`` (permissions reach the floor), ``visible`` (resolved), and
    ``locked`` (lockout-guarded admin cell).
    """
    ov = overrides()
    groups: list[dict] = []
    for page in PAGES:
        cells = []
        for role in rbac.ROLES:
            locked = (page["path"], role) in LOCKED
            el = eligible(page, role)
            cell = ov.get(page["path"], {}).get(role)
            visible = True if locked else (
                el and (cell if cell is not None else default_visible(page, role))
            )
            cells.append({"role": role, "eligible": el, "visible": visible, "locked": locked})
        # Merge by group label like nav_groups (see comment there).
        for g in groups:
            if g["label"] == page["group"]:
                break
        else:
            g = {"label": page["group"], "pages": []}
            groups.append(g)
        g["pages"].append({
            "path": page["path"], "label": page["label"],
            "floor": page["floor"], "in_nav": page.get("in_nav", True),
            "cells": cells,
        })
    return groups


__all__ = [
    "PAGES", "LOCKED", "store_path", "overrides", "clear_overrides",
    "eligible", "default_visible", "is_visible", "set_visibility",
    "apply_selection", "page_for", "nav_groups", "matrix",
]
