"""Cron-scheduled flows: a saved flow carrying a ``schedule`` cron expression is
armed as a recurring ``flow_cron`` job that fires a fresh flow run each tick."""
from __future__ import annotations

import types

import pytest


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_FLOWS", "1")
    monkeypatch.delenv("MAVERICK_DASHBOARD_TOKEN", raising=False)
    from maverick import world_model
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")


def _reset_aq(monkeypatch, aq):
    monkeypatch.setattr(aq, "_queue", None)
    monkeypatch.setattr(aq, "_worker", None)
    monkeypatch.setattr(aq, "_worker_threads", [])


def _save_flow(fid="digest", schedule="", *, publish=True):
    from maverick.flow import store
    from maverick.flow.ir import Flow, FlowNode
    f = Flow(id=fid, name="Digest", start="a",
             nodes={"a": FlowNode(id="a", kind="agent", brief="daily digest")})
    f.schedule = schedule
    saved = store.save_flow(f)
    if publish:
        store.publish_flow(
            fid,
            expected_version=saved.version,
            expected_revision=saved.revision,
        )
    return saved


def _cron_payload(fid="digest"):
    from maverick.flow import store

    flow, release = store.load_published_bundle(fid)
    return {
        "flow_id": fid,
        "__cron__": flow.schedule,
        "__tz__": flow.timezone,
        "__flow_revision__": release["release_id"],
    }


def test_reconcile_arms_one_cron_per_scheduled_flow(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maverick_dashboard import automation_queue as aq
    _reset_aq(monkeypatch, aq)
    _save_flow("digest", "0 9 * * *")
    _save_flow("manual", "")                    # no schedule -> no cron
    aq._reconcile_flow_crons()
    crons = aq._pending(aq.FLOW_CRON_KIND)
    assert len(crons) == 1
    assert crons[0].payload["flow_id"] == "digest"
    assert crons[0].payload["__cron__"] == "0 9 * * *"


def test_reconcile_is_idempotent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maverick_dashboard import automation_queue as aq
    _reset_aq(monkeypatch, aq)
    _save_flow("digest", "0 9 * * *")
    aq._reconcile_flow_crons()
    aq._reconcile_flow_crons()                  # second pass must not duplicate
    assert len(aq._pending(aq.FLOW_CRON_KIND)) == 1


def test_reconcile_cancels_cron_when_schedule_removed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maverick_dashboard import automation_queue as aq
    _reset_aq(monkeypatch, aq)
    _save_flow("digest", "0 9 * * *")
    aq._reconcile_flow_crons()
    assert len(aq._pending(aq.FLOW_CRON_KIND)) == 1
    _save_flow("digest", "")                    # schedule cleared
    aq._reconcile_flow_crons()
    assert aq._pending(aq.FLOW_CRON_KIND) == []


def test_reconcile_reseeds_when_expression_changes(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maverick_dashboard import automation_queue as aq
    _reset_aq(monkeypatch, aq)
    _save_flow("digest", "0 9 * * *")
    aq._reconcile_flow_crons()
    draft = _save_flow("digest", "0 17 * * *", publish=False)
    aq._reconcile_flow_crons()
    # Saving is authoring, not activation: the old release stays armed.
    crons = aq._pending(aq.FLOW_CRON_KIND)
    assert len(crons) == 1 and crons[0].payload["__cron__"] == "0 9 * * *"
    from maverick.flow import store
    store.publish_flow(
        draft.id,
        expected_version=draft.version,
        expected_revision=draft.revision,
    )
    aq._reconcile_flow_crons()
    crons = aq._pending(aq.FLOW_CRON_KIND)
    assert len(crons) == 1 and crons[0].payload["__cron__"] == "0 17 * * *"


def test_reconcile_skips_unarmable_schedule(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maverick_dashboard import automation_queue as aq
    _reset_aq(monkeypatch, aq)
    draft = _save_flow("digest", "0 0 31 2 *", publish=False)
    from maverick.flow import store
    with pytest.raises(store.FlowSnapshotError, match="cannot be published"):
        store.publish_flow(
            draft.id,
            expected_version=draft.version,
            expected_revision=draft.revision,
        )
    aq._reconcile_flow_crons()
    assert aq._pending(aq.FLOW_CRON_KIND) == []


def test_handle_flow_cron_enqueues_a_fresh_run(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maverick_dashboard import automation_queue as aq
    _reset_aq(monkeypatch, aq)
    _save_flow("digest", "0 9 * * *")
    job = types.SimpleNamespace(payload=_cron_payload())
    aq._handle_flow_cron(job)
    runs = aq._pending(aq.FLOW_RUN_KIND)
    assert len(runs) == 1 and runs[0].payload["flow_id"] == "digest"


def test_handle_flow_cron_noops_on_stale_schedule(monkeypatch, tmp_path):
    # The flow's schedule changed since this cron was armed -> don't fire.
    _isolate(monkeypatch, tmp_path)
    from maverick_dashboard import automation_queue as aq
    _reset_aq(monkeypatch, aq)
    _save_flow("digest", "0 17 * * *")
    stale = _cron_payload()
    stale["__cron__"] = "0 9 * * *"
    job = types.SimpleNamespace(payload=stale)
    aq._handle_flow_cron(job)
    assert aq._pending(aq.FLOW_RUN_KIND) == []


def test_named_tenant_cron_is_reconciled_and_fires_in_same_tenant(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from maverick.paths import reset_tenant, set_tenant
    from maverick.tenant import registry
    from maverick_dashboard import automation_queue as aq

    _reset_aq(monkeypatch, aq)
    registry.create_tenant("acme")
    tenant_token = set_tenant("acme")
    try:
        _save_flow("digest", "0 9 * * *")
    finally:
        reset_tenant(tenant_token)

    aq._reconcile_flow_crons()
    crons = aq._pending(aq.FLOW_CRON_KIND)
    assert len(crons) == 1 and crons[0].payload["tenant"] == "acme"

    aq._handle_flow_cron(types.SimpleNamespace(payload=crons[0].payload))
    runs = aq._pending(aq.FLOW_RUN_KIND)
    assert len(runs) == 1
    assert runs[0].payload["tenant"] == "acme"
    assert runs[0].payload["flow_id"] == "digest"


def test_reconcile_clears_ambient_tenant_before_shared_collection(
    monkeypatch, tmp_path,
):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    monkeypatch.setattr("maverick.client.client_id", lambda: None)
    from maverick.paths import current_tenant_id, reset_tenant, set_tenant
    from maverick.tenant import registry
    from maverick_dashboard import automation_queue as aq

    _reset_aq(monkeypatch, aq)
    registry.create_tenant("acme")
    _save_flow("shared", "0 8 * * *")
    token = set_tenant("acme")
    try:
        _save_flow("private", "0 9 * * *")
        aq._reconcile_flow_crons()
        assert current_tenant_id() == "acme"
    finally:
        reset_tenant(token)

    payloads = {job.payload["flow_id"]: job.payload for job in aq._pending(aq.FLOW_CRON_KIND)}
    assert "tenant" not in payloads["shared"]
    assert payloads["private"]["tenant"] == "acme"
