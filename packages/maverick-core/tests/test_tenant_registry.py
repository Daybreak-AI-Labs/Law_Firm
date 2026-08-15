"""Tenant lifecycle / provisioning registry (ROADMAP platform spine)."""
from __future__ import annotations

import pytest
from maverick.tenant import registry as tr


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)


def test_create_lists_and_makes_workspace(tmp_path):
    rec = tr.create_tenant("acme", plan="pro", display_name="Acme Inc", max_daily_dollars=50)
    assert rec.id == "acme" and rec.status == tr.ACTIVE and rec.plan == "pro"
    assert rec.max_daily_dollars == 50.0
    assert [t.id for t in tr.list_tenants()] == ["acme"]
    # The tenant's workspace dir was materialized under tenants/.
    assert (tmp_path / "tenants" / "acme").exists()


def test_create_duplicate_rejected():
    tr.create_tenant("acme")
    with pytest.raises(ValueError):
        tr.create_tenant("acme")


def test_create_rejects_casefold_namespace_alias(tmp_path):
    from maverick.paths import TenantNamespaceCollision

    tr.create_tenant("Acme")
    with pytest.raises((ValueError, TenantNamespaceCollision), match="alias"):
        tr.create_tenant("acme")
    assert [record.id for record in tr.list_tenants()] == ["Acme"]
    assert (tmp_path / "tenants" / "Acme").is_dir()


def test_create_normalizes_nfd_and_rejects_duplicate_identity():
    import unicodedata

    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert tr.create_tenant(nfd).id == nfc
    assert tr.get_tenant(nfd).id == nfc
    with pytest.raises(ValueError, match="already exists"):
        tr.create_tenant(nfc)


def test_deleted_namespace_claim_prevents_case_alias_reuse():
    from maverick.paths import TenantNamespaceCollision

    tr.create_tenant("Acme")
    assert tr.delete_tenant("Acme", purge=True)
    with pytest.raises(TenantNamespaceCollision, match="aliases namespace owned"):
        tr.create_tenant("acme")


def test_create_blank_id_rejected():
    with pytest.raises(ValueError):
        tr.create_tenant("  ")


def test_suspend_resume_flips_active_and_enforcement():
    tr.create_tenant("acme")
    assert tr.is_active("acme") is True
    tr.assert_tenant_active("acme")  # no raise

    tr.suspend_tenant("acme")
    assert tr.is_active("acme") is False
    assert tr.get_tenant("acme").status == tr.SUSPENDED
    with pytest.raises(tr.TenantSuspended):
        tr.assert_tenant_active("acme")

    tr.resume_tenant("acme")
    assert tr.is_active("acme") is True


def test_enforcement_is_noop_for_unprovisioned_and_none():
    # No registry file at all -> everything is active (opt-in).
    assert tr.is_active("never-provisioned") is True
    assert tr.is_active(None) is True
    tr.assert_tenant_active(None)  # no raise
    tr.assert_tenant_active("never-provisioned")  # no raise


def test_unknown_tenant_refused_once_registry_exists():
    tr.create_tenant("acme")
    assert tr.is_active(None) is True
    assert tr.is_active("ghost") is False
    with pytest.raises(tr.TenantSuspended):
        tr.assert_tenant_active("ghost")


def test_set_quota_and_plan():
    tr.create_tenant("acme")
    assert tr.set_quota("acme", 12.5).max_daily_dollars == 12.5
    assert tr.set_plan("acme", "enterprise").plan == "enterprise"


def test_mutate_unknown_tenant_raises():
    with pytest.raises(tr.UnknownTenant):
        tr.suspend_tenant("ghost")


def test_delete_without_purge_keeps_data(tmp_path):
    tr.create_tenant("acme")
    data = tmp_path / "tenants" / "acme"
    (data / "world.db").write_text("x", encoding="utf-8") if data.exists() else None
    assert tr.delete_tenant("acme") is True
    assert tr.get_tenant("acme") is None
    # Data dir survives a non-purging delete.
    assert data.exists()
    assert tr.is_active("acme") is False
    with pytest.raises(tr.TenantSuspended):
        tr.assert_tenant_active("acme")
    assert tr.delete_tenant("acme") is False  # already gone


def test_delete_with_purge_removes_data(tmp_path):
    tr.create_tenant("acme")
    data = tmp_path / "tenants" / "acme"
    (data / "world.db").write_text("x", encoding="utf-8")
    assert tr.delete_tenant("acme", purge=True) is True
    assert not data.exists()


def test_registry_round_trips_on_disk():
    tr.create_tenant("acme", plan="pro")
    tr.create_tenant("beta")
    # Fresh read from disk preserves both records, sorted.
    ids = [t.id for t in tr.list_tenants()]
    assert ids == ["acme", "beta"]
    assert tr.get_tenant("acme").plan == "pro"


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '{"tenants":[{"id":"acme","status":"suspnded"}]}',
        '{"tenants":[{"id":"acme","status":"active","max_daily_dollars":NaN}]}',
        '{"tenants":[],"tenants":[]}',
    ],
)
def test_corrupt_registry_fails_closed_and_is_not_overwritten(content, tmp_path):
    path = tmp_path / "tenant_registry.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(tr.TenantRegistryError):
        tr.list_tenants()
    assert tr.is_active("acme") is False
    with pytest.raises(tr.TenantRegistryError):
        tr.assert_tenant_active("acme")
    with pytest.raises(tr.TenantRegistryError):
        tr.create_tenant("new")
    assert path.read_text(encoding="utf-8") == content


# ---- atomic persistence + concurrency-safe roster edits ----

def test_save_is_atomic_0600_no_temp(tmp_path):
    from maverick.file_lock import private_path_is_restricted

    tr.create_tenant("acme")
    path = tmp_path / "tenant_registry.json"
    assert private_path_is_restricted(path)
    # A valid, fully-written JSON roster -- never a truncated file.
    import json
    assert "acme" in {t["id"] for t in json.loads(path.read_text())["tenants"]}
    # No stray temp droppings from the atomic write.
    assert list(tmp_path.glob("*.tmp")) == []


def test_create_tenant_provisions_private_workspace():
    from maverick.file_lock import private_path_is_restricted
    from maverick.workspace import Workspace

    tr.create_tenant("private-acme")
    root = Workspace("private-acme").root
    assert root.is_dir()
    assert private_path_is_restricted(root, 0o700)


def test_failed_workspace_provision_never_publishes_active_tenant(tmp_path):
    blocked = tmp_path / "tenants" / "blocked"
    blocked.parent.mkdir()
    blocked.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError):
        tr.create_tenant("blocked")
    assert tr.get_tenant("blocked") is None

    blocked.unlink()
    rec = tr.create_tenant("blocked")
    assert rec.active


def test_concurrent_creates_do_not_lose_tenants():
    """Each create_tenant does a load-modify-save; without the lock two
    concurrent creates both load the same roster and the second save clobbers
    the first's tenant. All N distinct tenants must survive."""
    import threading

    n = 24
    errors: list[Exception] = []

    def make(i: int):
        try:
            tr.create_tenant(f"t{i:03d}")
        except (ValueError, OSError) as e:
            errors.append(e)

    threads = [threading.Thread(target=make, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors[:3]
    assert len(tr.list_tenants()) == n


def test_concurrent_suspend_and_quota_both_apply():
    """A suspend racing a set_quota must not lose either change (last-writer
    clobber). After both, the tenant is suspended AND carries the new quota."""
    import threading

    tr.create_tenant("acme")
    barrier = threading.Barrier(2)

    def suspend():
        barrier.wait()
        tr.suspend_tenant("acme")

    def quota():
        barrier.wait()
        tr.set_quota("acme", 99.0)

    ts = [threading.Thread(target=suspend), threading.Thread(target=quota)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    rec = tr.get_tenant("acme")
    assert rec.status == tr.SUSPENDED
    assert rec.max_daily_dollars == 99.0
