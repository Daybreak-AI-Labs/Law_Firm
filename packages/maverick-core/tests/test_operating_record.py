"""The Operating Record: decisions as a system of record + signed capsule."""
from __future__ import annotations

import os

import pytest
from maverick import operating_record as orec
from maverick.file_lock import private_path_is_restricted
from maverick.world_model import WorldModel


@pytest.fixture()
def world(tmp_path):
    w = WorldModel(tmp_path / "w.db")
    gid = w.create_goal("Reconcile the quarterly ledger", domain="finance_sox")
    eid = w.start_episode(gid)
    w.end_episode(eid, "done", "success")
    w.set_goal_status(gid, "done", result="tied out")
    aid = w.create_approval("bank_transfer", risk="high",
                            detail="Q3 vendor batch")
    w.decide_approval(aid, "approved", decided_by="user:cfo")
    return w


def test_assemble_threads_goals_and_approvals(world):
    records = orec.assemble(world)
    kinds = {r.kind for r in records}
    assert kinds == {"goal", "approval"}
    goal = next(r for r in records if r.kind == "goal")
    assert goal.department == "finance_sox" and goal.outcome == "done"
    approval = next(r for r in records if r.kind == "approval")
    assert approval.decided_by == "user:cfo"
    s = orec.stats(records)
    assert s.n_goals == 1 and s.n_approvals == 1 and s.n_human_decisions == 1
    assert s.departments == {"finance_sox": 1}


def test_query_finds_every_decision_that_touched_x(world):
    records = orec.assemble(world)
    assert len(orec.query(records, text="ledger")) == 1
    assert len(orec.query(records, actor="user:cfo")) == 1
    assert orec.query(records, department="legal_x") == []


def test_owner_scoped_approvals_overfetch_past_global_limit():
    """list_approvals has no owner SQL filter, so an owner-scoped assemble must
    over-fetch: alice's older approvals must not be dropped just because newer
    approvals by others fill the global top-``limit``."""
    class _Approval:
        def __init__(self, i, who):
            self.decided_at = float(i)
            self.requested_by = who
            self.claimed_by = None
            self.decided_by = who
            self.action = f"act-{i}"
            self.status = "approved"
            self.provenance = None

    class _FakeWorld:
        def __init__(self):
            # alice's one approval is the OLDEST; then 1000 by others (newest).
            self._rows = [_Approval(0, "alice")] + [
                _Approval(i, "bob") for i in range(1, 1001)
            ]

        def list_goals(self, **kw):
            return []

        def list_episodes(self, **kw):
            return []

        def list_approvals(self, *, limit):
            # Newest first, truncated to limit -- exactly the SQL behavior.
            return list(reversed(self._rows))[:limit]

    world = _FakeWorld()
    records = orec.assemble(world, limit=100, owner="alice")
    # Pre-fix: alice's lone (oldest) approval falls outside the newest-100 and
    # is dropped. Post-fix: the over-fetch recovers it.
    assert any(r.decided_by == "alice" for r in records)


def test_capsule_roundtrip_and_tamper_detection(world, tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    import maverick.audit.signing as signing
    monkeypatch.setattr(signing, "KEY_DIR", tmp_path / "keys")
    out_path = tmp_path / "mind.capsule.json"
    out_path.write_text("old private capsule", encoding="utf-8")
    out_path.chmod(0o600)
    old_umask = os.umask(0o022)
    try:
        out = orec.export_capsule(world, out_path, now=5.0)
    finally:
        os.umask(old_umask)
    ok, reason = orec.verify_capsule(out)
    assert ok, reason
    # Written atomically (temp + replace): no .tmp sibling left behind.
    assert not out.with_name(out.name + ".tmp").exists()
    assert private_path_is_restricted(out, 0o600)
    # Tamper with one decision: the capsule must fail verification.
    text = out.read_text(encoding="utf-8").replace(
        "Reconcile the quarterly ledger", "Reconcile the doctored ledger")
    out.write_text(text, encoding="utf-8")
    ok, reason = orec.verify_capsule(out)
    assert not ok and "FAILED" in reason
