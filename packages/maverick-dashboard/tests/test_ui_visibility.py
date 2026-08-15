"""Role-based UI visibility: the page registry, the admin override store, the
role-filtered sidebar, and the page-level enforcement dependency.

Safety invariants under test (mirror maverick_dashboard.rbac's):
  * auth OFF (no principal) -> full nav, nothing enforced (single-user mode);
  * overrides can never show a page past the role's permission floor;
  * admins can never lose /settings or /users (lockout guard);
  * page policy governs UI pages only -- /api routes are untouched.
"""
from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient


# ---------- registry + pure logic (no HTTP) ----------

def test_registry_is_well_formed():
    from maverick_dashboard import rbac, ui_visibility
    perms = {"view", "operate", "audit", "admin"}
    seen_paths = set()
    for page in ui_visibility.PAGES:
        assert page["path"].startswith("/") and page["path"] not in seen_paths
        seen_paths.add(page["path"])
        assert page["floor"] in perms and page["default"] in perms
        # the default can never be weaker than the floor: every role that sees
        # the page by default must also clear its security floor.
        for role in rbac.ROLES:
            if ui_visibility.default_visible(page, role):
                assert ui_visibility.eligible(page, role)
    # locked pages exist and are admin-floor (they gate the undo path).
    for path, role in ui_visibility.LOCKED:
        assert path in seen_paths and role == "admin"


def test_registry_icons_exist_in_base_template():
    from pathlib import Path

    from maverick_dashboard import ui_visibility
    base = (
        Path(ui_visibility.__file__).parent / "templates" / "base.html"
    ).read_text(encoding="utf-8")
    for page in ui_visibility.PAGES:
        assert f'id="i-{page["icon"]}"' in base, page["path"]


def test_page_for_prefix_matching():
    from maverick_dashboard import ui_visibility
    assert ui_visibility.page_for("/goals")["path"] == "/goals"
    assert ui_visibility.page_for("/goals/3/plan")["path"] == "/goals"
    assert ui_visibility.page_for("/goal-builder")["path"] == "/goal-builder"
    assert ui_visibility.page_for("/flows/designer/7")["path"] == "/flows/designer"
    assert ui_visibility.page_for("/tenants/overview")["path"] == "/tenants"
    # unregistered surfaces stay ungoverned: APIs, share links, signed
    # approvals, probes, and flow-run detail pages.
    for path in ("/api/v1/goals", "/share/tok", "/flow/approve",
                 "/healthz", "/flows/5/runs/2", "/"):
        assert ui_visibility.page_for(path) is None


def test_store_roundtrip_diffs_and_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick_dashboard import ui_visibility
    assert ui_visibility.overrides() == {}
    benchmarks = next(p for p in ui_visibility.PAGES if p["path"] == "/benchmarks")
    # storing the default is a no-op (diff-only store)...
    ui_visibility.set_visibility("/benchmarks", "operator", True)
    assert ui_visibility.overrides() == {}
    # ...and a real change persists and resolves.
    ui_visibility.set_visibility("/benchmarks", "operator", False)
    assert ui_visibility.overrides() == {"/benchmarks": {"operator": False}}
    assert ui_visibility.is_visible(benchmarks, "operator") is False
    assert ui_visibility.is_visible(benchmarks, "admin") is True
    # flipping back to the default removes the stored diff entirely.
    ui_visibility.set_visibility("/benchmarks", "operator", True)
    assert ui_visibility.overrides() == {}
    with pytest.raises(ValueError):
        ui_visibility.set_visibility("/nope", "operator", False)
    with pytest.raises(ValueError):
        ui_visibility.set_visibility("/benchmarks", "superuser", False)


def test_corrupt_or_foreign_store_entries_are_dropped(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick_dashboard import ui_visibility
    p = ui_visibility.store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "/benchmarks": {"operator": False, "superuser": False, "viewer": "yes"},
        "/not-a-page": {"viewer": False},
        "/spend": "broken",
    }))
    assert ui_visibility.overrides() == {"/benchmarks": {"operator": False}}
    p.write_text("{not json")
    assert ui_visibility.overrides() == {}


def test_overrides_cannot_outrun_the_permission_floor(monkeypatch, tmp_path):
    # Even a hand-edited store showing /settings to viewers must not stick:
    # visibility is presentation policy, the floor is authz.
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick_dashboard import ui_visibility
    with pytest.raises(ValueError):
        ui_visibility.set_visibility("/settings", "viewer", True)
    p = ui_visibility.store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"/settings": {"viewer": True}}))
    settings = next(pg for pg in ui_visibility.PAGES if pg["path"] == "/settings")
    assert ui_visibility.is_visible(settings, "viewer") is False


def test_lockout_guard_ignores_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick_dashboard import ui_visibility
    with pytest.raises(ValueError):
        ui_visibility.set_visibility("/settings", "admin", False)
    p = ui_visibility.store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"/settings": {"admin": False}, "/users": {"admin": False}}))
    for path in ("/settings", "/users"):
        page = next(pg for pg in ui_visibility.PAGES if pg["path"] == path)
        assert ui_visibility.is_visible(page, "admin") is True


def test_nav_groups_per_role(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick_dashboard import ui_visibility

    def hrefs(role):
        return {ln["href"] for g in ui_visibility.nav_groups(role) for ln in g["links"]}

    in_nav = {p["path"] for p in ui_visibility.PAGES if p.get("in_nav", True)}
    # auth off: every sidebar page (in_nav: False pages are reached from
    # in-page links, never the sidebar).
    assert hrefs(None) == in_nav
    # The redesigned Flows builder is the primary sidebar entry; the older
    # workflow index/builder and the alt goal composer are reached in-page.
    assert "/flows/designer" in in_nav
    assert "/goal-builder" not in in_nav and "/workflows" not in in_nav \
        and "/workflow-builder" not in in_nav
    # viewer: read-only pages only -- no run/build/admin surfaces, and the
    # groups left empty (Extend, Admin) are dropped whole.
    v = hrefs("viewer")
    assert {"/goals", "/overview", "/spend", "/safety", "/learning"} <= v
    assert v.isdisjoint({"/chat", "/ekko", "/automations", "/settings", "/users", "/mcp", "/audit"})
    assert {g["label"] for g in ui_visibility.nav_groups("viewer")} == {
        "Operate", "Observe", "Govern"}
    # auditor: the audit read surfaces, nothing operational. (/replay is folded
    # out of the sidebar in the moderate declutter; reached from Audit in-page.)
    a = hrefs("auditor")
    assert {"/audit", "/compliance", "/overview"} <= a
    assert a.isdisjoint({"/chat", "/settings", "/automations", "/replay"})
    # operator: operate surfaces but no admin plumbing.
    o = hrefs("operator")
    assert {"/chat", "/goals", "/ekko", "/automations", "/approvals", "/skills"} <= o
    assert o.isdisjoint({"/settings", "/users", "/tenants", "/mcp", "/channels",
                         "/cache", "/audit"})
    # admin: every sidebar page.
    assert hrefs("admin") == in_nav


# ---------- HTTP enforcement + settings matrix ----------

def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    from maverick_dashboard import app as dash_app
    dash_app._world_cache.clear()
    return TestClient(dash_app.app, headers={"Origin": "http://testserver"})


def _as(monkeypatch, principal):
    from maverick_dashboard import auth
    monkeypatch.setattr(auth, "caller_principal", lambda request: principal)


def test_auth_off_full_nav_and_no_enforcement(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    body = c.get("/goals").text
    assert 'href="/users"' in body and 'href="/settings"' in body
    assert c.get("/facts").status_code == 200
    # even an explicit override is inert with auth off (single-user mode).
    from maverick_dashboard import ui_visibility
    ui_visibility.set_visibility("/benchmarks", "operator", False)
    assert c.get("/benchmarks").status_code == 200


def test_viewer_pages_hidden_and_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_DASHBOARD_ADMINS", "")
    c = _client(monkeypatch, tmp_path)
    from maverick_dashboard import rbac
    rbac.set_role("user:vy", "viewer")
    _as(monkeypatch, "user:vy")
    r = c.get("/goals")
    assert r.status_code == 200
    assert 'href="/settings"' not in r.text
    assert 'href="/automations"' not in r.text
    assert 'data-grp="Admin"' not in r.text
    assert c.get("/automations").status_code == 403
    assert c.get("/chat").status_code == 403


def test_operator_blocked_from_admin_group_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_DASHBOARD_ADMINS", "")
    c = _client(monkeypatch, tmp_path)
    from maverick_dashboard import rbac
    rbac.set_role("user:op", "operator")
    _as(monkeypatch, "user:op")
    assert c.get("/automations").status_code == 200
    assert c.get("/cache").status_code == 403
    assert c.get("/tenants").status_code == 403


def test_admin_override_hides_page_but_not_api(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_DASHBOARD_ADMINS", "")
    c = _client(monkeypatch, tmp_path)
    from maverick_dashboard import rbac, ui_visibility
    rbac.set_role("user:aud", "auditor")
    ui_visibility.set_visibility("/audit", "auditor", False)
    _as(monkeypatch, "user:aud")
    # the page is gone (nav + direct), but the API surface it fronts is still
    # governed by RBAC permissions alone -- page policy never gates /api.
    assert c.get("/audit").status_code == 403
    assert c.get("/api/v1/audit/tail").status_code == 200


def test_settings_matrix_save_reset_and_authz(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_DASHBOARD_ADMINS", "user:boss")
    c = _client(monkeypatch, tmp_path)
    from maverick_dashboard import rbac, ui_visibility
    _as(monkeypatch, "user:boss")
    # build the full checked set from the resolved matrix, then untick one cell.
    checked = [
        f"{cell['role']}:{pg['path']}"
        for grp in ui_visibility.matrix() for pg in grp["pages"]
        for cell in pg["cells"]
        if cell["visible"] and cell["eligible"] and not cell["locked"]
    ]
    checked.remove("operator:/benchmarks")
    r = c.post("/settings/ui-visibility", data={"cell": checked})
    assert r.status_code == 200  # follows the 303 back to /settings
    assert ui_visibility.overrides() == {"/benchmarks": {"operator": False}}
    rbac.set_role("user:op", "operator")
    _as(monkeypatch, "user:op")
    assert c.get("/benchmarks").status_code == 403
    assert 'href="/benchmarks"' not in c.get("/goals").text
    # a non-admin cannot edit or reset the matrix.
    assert c.post("/settings/ui-visibility", data={"cell": []}).status_code == 403
    assert c.post("/settings/ui-visibility/reset").status_code == 403
    # reset restores the defaults.
    _as(monkeypatch, "user:boss")
    assert c.get("/benchmarks").status_code == 200
    assert c.post("/settings/ui-visibility/reset").status_code == 200
    assert ui_visibility.overrides() == {}
    _as(monkeypatch, "user:op")
    assert c.get("/benchmarks").status_code == 200


def test_settings_matrix_requires_global_admin_for_tenant_local_admin(
    monkeypatch, tmp_path
):
    # The visibility matrix is global control-plane state, so a tenant-local
    # admin must not be allowed to change it for every other tenant/user.
    monkeypatch.setenv("MAVERICK_DASHBOARD_ADMINS", "")
    c = _client(monkeypatch, tmp_path)
    from maverick.paths import reset_tenant, set_tenant
    from maverick_dashboard import rbac, ui_visibility

    rbac.set_role("user:tenant-admin", "viewer")
    rbac.set_tenant_role("acme", "user:tenant-admin", "admin")
    _as(monkeypatch, "user:tenant-admin")

    tok = set_tenant("acme")
    try:
        assert c.post("/settings/ui-visibility", data={"cell": []}).status_code == 403
        assert c.post("/settings/ui-visibility/reset").status_code == 403
        assert ui_visibility.overrides() == {}
    finally:
        reset_tenant(tok)


def test_matrix_save_cannot_lock_admins_out(monkeypatch, tmp_path):
    # A save with NOTHING checked hides everything hideable -- and the admin
    # must still reach /settings and /users to undo it.
    monkeypatch.setenv("MAVERICK_DASHBOARD_ADMINS", "user:boss")
    c = _client(monkeypatch, tmp_path)
    _as(monkeypatch, "user:boss")
    assert c.post("/settings/ui-visibility", data={}).status_code == 200
    assert c.get("/settings").status_code == 200
    assert c.get("/users").status_code == 200
    body = c.get("/settings").text
    assert 'href="/users"' in body
    # a smuggled grant past the floor is ignored on save, not persisted.
    c.post("/settings/ui-visibility", data={"cell": ["viewer:/settings"]})
    from maverick_dashboard import rbac, ui_visibility
    assert ui_visibility.overrides().get("/settings") is None
    rbac.set_role("user:vy", "viewer")
    _as(monkeypatch, "user:vy")
    assert c.get("/settings").status_code == 403


def test_settings_page_renders_matrix(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    body = c.get("/settings").text
    assert "Page visibility by role" in body
    assert 'action="/settings/ui-visibility"' in body
    assert 'value="operator:/benchmarks"' in body
    # locked admin cells render disabled, not as editable inputs.
    assert "Always available to admins" in body


def test_nav_and_matrix_groups_are_unique():
    """Regression: a page whose group differs from its list neighbours (e.g. an
    entry re-homed to Admin mid-list) must merge into its group, not split the
    sidebar into duplicate EXTEND/ADMIN sections (a shipped visual bug)."""
    from maverick_dashboard import rbac, ui_visibility
    for role in [None, *rbac.ROLES]:
        labels = [g["label"] for g in ui_visibility.nav_groups(role)]
        assert len(labels) == len(set(labels)), f"duplicate nav groups for {role}: {labels}"
    mlabels = [g["label"] for g in ui_visibility.matrix()]
    assert len(mlabels) == len(set(mlabels)), f"duplicate matrix groups: {mlabels}"
