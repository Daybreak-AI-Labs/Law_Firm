"""Tenant-aware data paths and per-user tenant scoping.

A tenant namespaces on-disk state. With no tenant active, paths resolve to the
legacy ~/.maverick locations (single-tenant unchanged).
"""
import maverick.paths as paths
import pytest
from maverick.paths import current_tenant, data_dir, reset_tenant, set_tenant

# --- tenant resolution -----------------------------------------------------

def test_no_tenant_by_default(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    assert current_tenant() is None


def test_tenant_from_env(monkeypatch):
    monkeypatch.setenv("MAVERICK_TENANT", "acme")
    assert current_tenant() == "acme"


def test_explicit_scope_beats_env(monkeypatch):
    monkeypatch.setenv("MAVERICK_TENANT", "acme")
    tok = set_tenant("globex")
    try:
        assert current_tenant() == "globex"
    finally:
        reset_tenant(tok)
    assert current_tenant() == "acme"  # restored


def test_tenant_encoding_prevents_traversal_without_collisions(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    tok = set_tenant("../../etc")  # path-traversal attempt
    try:
        t = current_tenant()
        assert t == "..%2F..%2Fetc"
        assert "/" not in t  # remains a single safe path segment
        # The real invariant: the resolved data path can't escape the tenants root.
        resolved = data_dir("x").resolve()
        assert (paths.maverick_home() / "tenants").resolve() in resolved.parents
    finally:
        reset_tenant(tok)


def test_tenant_encoding_is_collision_resistant(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    assert data_dir("audit", tenant="ac/me") != data_dir("audit", tenant="ac_me")
    assert data_dir("audit", tenant="/") != data_dir("audit", tenant="default")
    assert data_dir("audit", tenant=".").resolve().parent == (
        paths.maverick_home() / "tenants" / "%2E"
    ).resolve()


def test_tenant_segment_nfc_normalised(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    import unicodedata
    nfc = unicodedata.normalize("NFC", "josé")   # "josé" precomposed
    nfd = unicodedata.normalize("NFD", "josé")   # "josé" decomposed
    assert nfc != nfd  # distinct byte sequences pre-normalisation
    # ...but the same on-disk namespace, so one tenant's data isn't split in two.
    assert data_dir("audit", tenant=nfc) == data_dir("audit", tenant=nfd)


def test_data_dir_refuses_case_alias_before_any_store_access(monkeypatch):
    from maverick.paths import TenantNamespaceCollision

    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    owner_path = data_dir("audit", tenant="Acme")
    with pytest.raises(TenantNamespaceCollision, match="aliases namespace owned"):
        data_dir("audit", tenant="acme")
    assert owner_path == paths.maverick_home() / "tenants" / "Acme" / "audit"


# --- data_dir --------------------------------------------------------------

def test_data_dir_no_tenant(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    p = data_dir("audit")
    assert p == paths.maverick_home() / "audit"
    assert "tenants" not in p.parts


def test_data_dir_with_tenant(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    tok = set_tenant("acme")
    try:
        p = data_dir("audit")
        assert p == paths.maverick_home() / "tenants" / "acme" / "audit"
    finally:
        reset_tenant(tok)


def test_data_dir_force_shared(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    tok = set_tenant("acme")
    try:
        # tenant=None forces the shared location even when a tenant is active.
        assert data_dir("audit", tenant=None) == paths.maverick_home() / "audit"
    finally:
        reset_tenant(tok)


# --- per-user tenant scope (server wiring) ---------------------------------

def test_tenant_by_user_disabled_by_default(monkeypatch):
    monkeypatch.setattr("maverick.config.load_config", dict)
    monkeypatch.delenv("MAVERICK_TENANT_BY_USER", raising=False)
    from maverick.paths import tenant_by_user_enabled
    assert tenant_by_user_enabled() is False


def test_tenant_by_user_via_env(monkeypatch):
    monkeypatch.setattr("maverick.config.load_config", dict)
    monkeypatch.setenv("MAVERICK_TENANT_BY_USER", "1")
    from maverick.paths import tenant_by_user_enabled
    assert tenant_by_user_enabled() is True


def test_tenant_scope_noop_when_disabled(monkeypatch):
    monkeypatch.setattr("maverick.config.load_config", dict)
    monkeypatch.delenv("MAVERICK_TENANT_BY_USER", raising=False)
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    from maverick.paths import tenant_scope
    with tenant_scope(channel="tg", user_id="42"):
        assert current_tenant() is None  # disabled -> no isolation


def test_tenant_scope_sets_and_restores(monkeypatch):
    monkeypatch.setattr("maverick.config.load_config", dict)
    monkeypatch.setenv("MAVERICK_TENANT_BY_USER", "1")
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    from maverick.paths import tenant_scope
    assert current_tenant() is None
    with tenant_scope(channel="tg", user_id="42"):
        assert current_tenant() == "tg%3A42"  # ':' is percent-encoded
    assert current_tenant() is None  # restored after the block


def test_tenant_scope_explicit_tenant_ignores_flag(monkeypatch):
    monkeypatch.setattr("maverick.config.load_config", dict)
    monkeypatch.delenv("MAVERICK_TENANT_BY_USER", raising=False)
    from maverick.paths import tenant_scope
    with tenant_scope(tenant="acme"):
        assert current_tenant() == "acme"  # explicit tenant works even when flag off


def test_lazy_skill_distillation_store_follows_active_tenant(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))

    from maverick.paths import tenant_scope
    from maverick.skill import distillation_local, distillation_v2

    skill = {
        "name": "tenant-skill",
        "triggers": ["tenant skill"],
        "tools_needed": [],
        "summary": "tenant skill",
        "n_examples": 1,
    }

    with tenant_scope(tenant="tenant-a"):
        path_a = distillation_local.save_skill(skill)
        assert path_a.parent == tmp_path / "tenants" / "tenant-a" / "learned-skills"
        assert distillation_v2.signatures_from_store()

    with tenant_scope(tenant="tenant-b"):
        path_b = distillation_local.save_skill(skill)
        assert path_b.parent == tmp_path / "tenants" / "tenant-b" / "learned-skills"
        assert distillation_v2.signatures_from_store()
        assert not (tmp_path / "tenants" / "tenant-b" / "learned-skills" / "missing.md").exists()
