"""P1 multi-tenancy: per-tenant ``world.db`` isolation.

Completes the world-model leg of per-user tenancy. Cross-session memory and the
audit log already resolve their dirs via :func:`maverick.paths.data_dir`; this
gives each tenant its OWN world.db the same way. Default off -> single-tenant
behaviour (one shared ``~/.maverick/world.db``) is unchanged.

HOME is isolated per test by the autouse ``_isolate_maverick_home`` conftest
fixture, so these are hermetic (no real ``~/.maverick`` touched, no network).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from maverick.paths import maverick_home
from maverick.world_model import world_for_tenant


@pytest.fixture(autouse=True)
def _clear_world_cache():
    """Drop the process-wide per-tenant WorldModel cache around each test.

    The cache is keyed by resolved DB path; HOME is per-test so paths already
    differ, but clearing (and closing) keeps SQLite FDs from leaking across the
    suite and makes instance-identity assertions deterministic.
    """
    import maverick.world_model as wm

    def _drain():
        for w in list(wm._tenant_worlds.values()):
            try:
                w.close()
            except Exception:
                pass
        wm._tenant_worlds.clear()

    _drain()
    yield
    _drain()


# --- path resolution -------------------------------------------------------

def test_no_tenant_is_legacy_world_db(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    w = world_for_tenant(None)
    assert w.path == maverick_home() / "world.db"
    assert "tenants" not in w.path.parts


def test_tenant_world_db_under_tenants_dir(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    w = world_for_tenant("acme")
    assert w.path == maverick_home() / "tenants" / "acme" / "world.db"


def test_same_tenant_returns_cached_instance(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    assert world_for_tenant("acme") is world_for_tenant("acme")
    # None (shared default) is likewise cached/stable.
    assert world_for_tenant(None) is world_for_tenant(None)


def test_unicode_normal_forms_share_one_canonical_world(monkeypatch):
    import unicodedata

    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")

    assert nfc != nfd
    assert world_for_tenant(nfc) is world_for_tenant(nfd)


def test_case_alias_cannot_open_or_read_existing_world(monkeypatch):
    from maverick.paths import TenantNamespaceCollision

    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    owner = world_for_tenant("Acme")
    goal_id = owner.create_goal("owner-only", "secret")

    with pytest.raises(TenantNamespaceCollision, match="aliases namespace owned"):
        world_for_tenant("acme")

    assert owner.get_goal(goal_id).description == "secret"


def test_legacy_directory_requires_original_casing(monkeypatch):
    from maverick.paths import TenantNamespaceCollision

    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    legacy_root = maverick_home() / "tenants" / "Acme"
    legacy_root.mkdir(parents=True)

    with pytest.raises(TenantNamespaceCollision, match="existing namespace"):
        world_for_tenant("acme")

    # Exact-cased legacy data is adopted and receives a durable claim.
    assert world_for_tenant("Acme").path == legacy_root / "world.db"


def test_different_tenants_get_different_worlds(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    a = world_for_tenant("acme")
    b = world_for_tenant("globex")
    assert a is not b
    assert a.path != b.path
    assert world_for_tenant(None) is not a  # shared default distinct from a tenant


def test_default_constructor_resolves_tenant_and_home_per_call(tmp_path, monkeypatch):
    """Import-time defaults must never pin a later request to another tenant."""
    from maverick import client
    from maverick.paths import tenant_scope
    from maverick.world_model import WorldModel

    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_CLIENT_ID", raising=False)
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    client.reset_client_cache()

    with tenant_scope(tenant="tenant-a"):
        tenant_world = WorldModel()
    shared_world = WorldModel()
    try:
        assert tenant_world.path == (
            tmp_path / "tenants" / "tenant-a" / "world.db"
        )
        assert shared_world.path == tmp_path / "world.db"
        assert tenant_world.path != shared_world.path
    finally:
        tenant_world.close()
        shared_world.close()


# --- data isolation --------------------------------------------------------

def test_goals_are_isolated_across_tenants(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    world_a = world_for_tenant("tenant-a")
    world_b = world_for_tenant("tenant-b")

    gid = world_a.create_goal("a-only", "secret for tenant a")

    # Tenant A sees its goal; tenant B's world is empty -> no cross-tenant leak.
    assert world_a.get_goal(gid) is not None
    assert world_a.get_goal(gid).title == "a-only"
    assert world_b.get_goal(gid) is None
    assert world_b.list_goals() == []


# --- server routing (the actual wiring) ------------------------------------

@dataclass
class _Msg:
    text: str
    channel: str
    user_id: str


def _build_server(monkeypatch):
    """A minimal Server whose shared world is the legacy ~/.maverick/world.db."""
    from maverick import server as server_mod
    from maverick.world_model import WorldModel

    monkeypatch.setattr("maverick.config.load_config", dict)
    shared = WorldModel(maverick_home() / "world.db")
    srv = server_mod.Server.__new__(server_mod.Server)
    srv.world = shared
    srv.llm = object()
    srv.sandbox = object()
    srv.max_depth = 3
    srv._channels = []
    srv._tasks = []
    srv._shield = None  # shield off -> input/output scans skipped
    return server_mod, srv, shared


def test_world_for_tenant_rejects_overlong_tenant(monkeypatch):
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)

    with pytest.raises(ValueError, match="tenant id is too long"):
        world_for_tenant("x" * 201)


def test_world_for_tenant_caps_cached_tenant_worlds(monkeypatch):
    # The cache is now an LRU: reaching MAX_TENANT_WORLDS evicts the
    # least-recently-used tenant instead of raising, so a busy fleet of many
    # tenants keeps working (audit M7).
    import maverick.world_model as wm

    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    wm._tenant_worlds.clear()
    monkeypatch.setattr(wm, "MAX_TENANT_WORLDS", 2)

    first = world_for_tenant("tenant-1")
    assert world_for_tenant("tenant-2") is not first
    # Touch tenant-1 so tenant-2 becomes the least-recently-used.
    assert world_for_tenant("tenant-1") is first
    # A third tenant does NOT raise; it evicts the LRU (tenant-2) and is cached.
    third = world_for_tenant("tenant-3")
    assert third is not first
    keys = list(wm._tenant_worlds)
    normalized = [Path(k).as_posix() for k in keys]
    assert len(wm._tenant_worlds) == 2
    assert not any(k.endswith("tenant-2/world.db") for k in normalized)  # evicted
    assert any(k.endswith("tenant-1/world.db") for k in normalized)  # kept
    assert any(k.endswith("tenant-3/world.db") for k in normalized)  # added
    wm._tenant_worlds.clear()


def test_evicted_cache_owned_world_is_not_closed_by_borrower(monkeypatch):
    import maverick.world_model as wm

    monkeypatch.setattr(wm, "MAX_TENANT_WORLDS", 1)
    first = world_for_tenant("tenant-1")
    goal_id = first.create_goal("still in flight")
    world_for_tenant("tenant-2")  # evicts tenant-1 from LRU membership
    assert first not in wm._tenant_worlds.values()

    wm.close_world_if_owned(first)
    try:
        assert first.get_goal(goal_id).title == "still in flight"
    finally:
        first.close()


def test_cli_defaults_world_db_to_active_tenant(monkeypatch):
    from click.testing import CliRunner
    from maverick.cli import main

    monkeypatch.delenv("MAVERICK_HOME", raising=False)
    monkeypatch.setenv("MAVERICK_TENANT", "acme")

    # `status` opens the world at the resolved default db (no --db passed).
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0, result.output

    # The run history landed in acme's OWN world.db, not the shared default.
    assert (maverick_home() / "tenants" / "acme" / "world.db").exists()
    assert not (maverick_home() / "world.db").exists()


def test_cli_no_tenant_uses_legacy_world_db(monkeypatch):
    from click.testing import CliRunner
    from maverick.cli import main

    monkeypatch.delenv("MAVERICK_HOME", raising=False)
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)

    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0, result.output

    # Single-tenant unchanged: the legacy shared world.db, no per-tenant dir.
    assert (maverick_home() / "world.db").exists()
    assert not (maverick_home() / "tenants").exists()


def test_cli_explicit_db_overrides_tenant(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from maverick.cli import main

    monkeypatch.delenv("MAVERICK_HOME", raising=False)
    monkeypatch.setenv("MAVERICK_TENANT", "acme")
    explicit = tmp_path / "explicit" / "world.db"

    result = CliRunner().invoke(main, ["--db", str(explicit), "status"])
    assert result.exit_code == 0, result.output

    # An explicit --db always wins over the tenant default.
    assert explicit.exists()
    assert not (maverick_home() / "tenants" / "acme" / "world.db").exists()
