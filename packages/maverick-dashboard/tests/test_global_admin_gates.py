"""A route that writes a deployment-global sink must use the global gate.

The bug this pins: ``POST /users/set`` wrote ``dashboard-users.json`` -- the
deployment-wide RBAC roster, at ``maverick_home()/dashboard-users.json`` with no
tenant segment -- behind ``auth.require_permission``, which honours tenant
memberships. ``auth.require_global_permission`` exists precisely because
"tenant memberships are intentionally ignored so a tenant-local admin cannot
satisfy global admin gates" (auth.has_global_permission). So a tenant-local
admin could call it and assign themselves a global role in one request; the same
mismatch let one write an attacker-controlled provider ``base_url`` into the
deployment-global config overlay, routing every tenant's prompts through them.

The inconsistency was visible inside one file: ``/users/suites/set`` used the
global gate while ``/users/set``, two routes above it, did not.

This test is written as an INVARIANT rather than a list of route names, because
a list is what drifted. It derives the requirement from the write sink: if a
handler calls a mutator on a module whose store is deployment-global, the gate
must be the global one. Adding a route that writes a global sink therefore fails
here without anyone remembering to update a list.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ADMIN_PAGES = (
    Path(__file__).resolve().parents[1]
    / "maverick_dashboard" / "admin_pages.py"
)

#: Modules whose stores carry no tenant segment, verified by reading them:
#:   rbac.store_path()      -> maverick_home()/dashboard-users.json
#:   invites.store_path()   -> maverick_home()/dashboard-invites.json
#:   settings_store._write  -> config.dashboard_overrides_path(), which is
#:                             config_path().parent/<basename>
#:   suite_grants, ui_visibility -> same maverick_home() root
#:   runtime_overrides      -> OVERRIDES_PATH, a module-level constant bound at
#:                             IMPORT time. data_dir() is tenant-aware, but
#:                             freezing it at import captures the tenant active
#:                             then (none, in practice), so every write lands in
#:                             the shared file whatever tenant is active later.
#:                             That freeze is its own latent bug; until it is
#:                             fixed the sink is global and the gate must match.
GLOBAL_SINK_MODULES = frozenset({
    "rbac", "invites", "settings_store", "suite_grants", "ui_visibility",
    "runtime_overrides",
})

#: Mutators on those modules. Read-only helpers are irrelevant here.
MUTATOR_PREFIXES = ("set_", "clear_", "remove_", "revoke_", "create_",
                    "delete_", "update_", "save_", "write_")

MUTATING_METHODS = frozenset({"post", "put", "patch", "delete"})


def _routes() -> list[tuple[str, str, ast.AST]]:
    """(method, path, function-node) for every mutating route in admin_pages."""
    tree = ast.parse(ADMIN_PAGES.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr in MUTATING_METHODS and d.args
                    and isinstance(d.args[0], ast.Constant)):
                out.append((d.func.attr.upper(), d.args[0].value, node))
    return out


def _global_sinks_written(fn: ast.AST) -> set[str]:
    """Global-sink mutators this handler calls, as ``module.function``."""
    found = set()
    for c in ast.walk(fn):
        if not isinstance(c, ast.Call):
            continue
        # module.mutator(...)
        if isinstance(c.func, ast.Attribute) and isinstance(c.func.value, ast.Name):
            mod, name = c.func.value.id, c.func.attr
            if mod in GLOBAL_SINK_MODULES and name.startswith(MUTATOR_PREFIXES):
                found.add(f"{mod}.{name}")
        # bare mutator(...) imported from a global-sink module inside the handler
        elif isinstance(c.func, ast.Name) and c.func.id.startswith(MUTATOR_PREFIXES):
            for imp in ast.walk(fn):
                if isinstance(imp, ast.ImportFrom) and imp.module:
                    tail = imp.module.rsplit(".", 1)[-1]
                    if tail in GLOBAL_SINK_MODULES and any(
                            a.name == c.func.id for a in imp.names):
                        found.add(f"{tail}.{c.func.id}")
    return found


def _gates(fn: ast.AST) -> set[str]:
    return {
        c.func.attr if isinstance(c.func, ast.Attribute) else c.func.id
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
        and (getattr(c.func, "attr", None) or getattr(c.func, "id", None))
        in {"require_permission", "require_global_permission"}
    }


ROUTES_WRITING_GLOBAL = [
    (m, p, fn, sinks) for m, p, fn in _routes()
    if (sinks := _global_sinks_written(fn))
]


def test_the_scan_actually_finds_routes() -> None:
    """Anti-vacuity: an invariant over an empty set proves nothing.

    If a refactor moves these handlers elsewhere, this fails loudly rather than
    letting the file below it pass by inspecting zero routes.
    """
    assert len(ROUTES_WRITING_GLOBAL) >= 12, [
        (m, p) for m, p, _, _ in ROUTES_WRITING_GLOBAL]


@pytest.mark.parametrize(
    "method,path,sinks",
    [(m, p, sorted(s)) for m, p, _, s in ROUTES_WRITING_GLOBAL],
    ids=[f"{m} {p}" for m, p, _, _ in ROUTES_WRITING_GLOBAL],
)
def test_global_sink_routes_use_the_global_gate(method, path, sinks) -> None:
    fn = next(f for m, p, f, _ in ROUTES_WRITING_GLOBAL if (m, p) == (method, path))
    gates = _gates(fn)
    assert "require_global_permission" in gates, (
        f"{method} {path} writes deployment-global state ({', '.join(sinks)}) "
        f"but gates with {gates or '{nothing}'}. require_permission honours "
        "tenant memberships, so a tenant-local admin passes it -- and for a "
        "global sink that is privilege escalation. Use "
        "auth.require_global_permission(request, 'admin')."
    )


def test_the_two_gates_have_not_become_the_same_function() -> None:
    """The whole invariant rests on them differing. Pin that they still do."""
    import inspect

    from maverick_dashboard import auth

    assert inspect.getsource(auth.has_global_permission) != inspect.getsource(
        auth.has_permission)
    # The distinguishing behaviour, stated in has_global_permission's docstring.
    assert "memberships" in (auth.has_global_permission.__doc__ or "")
