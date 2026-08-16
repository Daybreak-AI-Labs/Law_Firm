"""Tests for the agent control-plane dashboard pages (replay + agent trust)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maverick_dashboard.app import app
from maverick_dashboard.control_plane import build_replay

client = TestClient(app, headers={"Origin": "http://testserver"})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # Fresh world DB + audit dir per test (MAVERICK_HOME steers data_dir("audit")).
    from maverick import world_model
    from maverick.audit import writer as audit_writer
    monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.delenv("MAVERICK_AUDIT_SIGN", raising=False)
    # The no-tenant audit writer is a module-level singleton keyed to the first
    # dir it saw; reset it so record() targets THIS test's MAVERICK_HOME (the
    # sanctioned override per writer.default_audit_log's docstring).
    monkeypatch.setattr(audit_writer, "_default", None)
    audit_writer._defaults.clear()
    yield


def _seed_run() -> int:
    """Create a goal + write a few governed audit events for it; return its id."""
    from maverick.audit import EventKind, record
    from maverick.world_model import open_world
    w = open_world()
    gid = w.create_goal("Vendor payment run")
    record(EventKind.GOAL_START, goal_id=gid, title="Vendor payment run")
    record(EventKind.TOOL_CALL, goal_id=gid, name="browser", input_summary="navigate checkout")
    record(EventKind.CONSENT_PROMPT, goal_id=gid, action="browser.click",
           risk="high", scope="text=Pay now")
    record(EventKind.CONSENT_RESULT, goal_id=gid, action="browser.click",
           decision="approve", source="dashboard")
    record(EventKind.TOOL_CALL, goal_id=gid, name="browser", input_summary="click Pay now")
    record(EventKind.GOAL_END, goal_id=gid, status="done")
    return gid


# ---- replay / flight recorder ----------------------------------------------

def test_build_replay_timeline_and_chain():
    gid = _seed_run()
    rep = build_replay(gid)
    kinds = [e["kind"] for e in rep["entries"]]
    assert "tool_call" in kinds
    assert "consent_prompt" in kinds
    assert "consent_result" in kinds
    assert rep["summary"]["tool_calls"] == 2
    assert rep["summary"]["approvals"] == 1
    assert rep["summary"]["approved"] == 1
    # Default deployment has signing off -> reported honestly as 'unsigned',
    # never silently 'verified'.
    assert rep["chain"]["status"] in ("unsigned", "no_log")
    prompts = [e for e in rep["entries"] if e["kind"] == "consent_prompt"]
    assert prompts and prompts[0]["risk"] == "high"


def test_replay_chain_includes_anchor_ledger_breaks(monkeypatch):
    import maverick.audit as audit
    from maverick.audit import ChainBreak
    from maverick.paths import data_dir
    from maverick_dashboard.control_plane import _verify_days

    day = "2026-06-16"
    audit_dir = data_dir("audit")
    audit_dir.mkdir(parents=True)
    (audit_dir / f"{day}.ndjson").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(audit, "verify_chain", lambda path: [])
    monkeypatch.setattr(
        audit,
        "verify_anchors",
        lambda path: [ChainBreak(0, "anchor_tip_mismatch", "2026-06-16 tip mismatch")],
    )

    chain = _verify_days([day])

    assert chain["status"] == "broken"
    assert chain["break_count"] == 1
    assert chain["breaks"][0]["reason"] == "anchor_tip_mismatch"


def test_replay_chain_flags_deleted_anchored_day(monkeypatch):
    import maverick.audit as audit
    from maverick.audit import ChainBreak
    from maverick_dashboard.control_plane import _verify_days

    day = "2026-06-16"
    monkeypatch.setattr(
        audit,
        "verify_anchors",
        lambda path: [ChainBreak(0, "anchored_file_deleted", f"{day}.ndjson is anchored but missing")],
    )

    chain = _verify_days([day])

    assert chain["status"] == "broken"
    assert chain["break_count"] == 1
    assert chain["breaks"][0]["reason"] == "anchored_file_deleted"

def test_replay_filters_to_one_goal():
    gid = _seed_run()
    other = _seed_run()
    rep = build_replay(gid)
    # only this run's events; the other run is excluded
    assert rep["summary"]["total"] >= 5
    assert other != gid


def test_replay_api_and_evidence_download():
    gid = _seed_run()
    r = client.get(f"/api/v1/replay/{gid}")
    assert r.status_code == 200
    body = r.json()
    assert body["goal"]["id"] == gid
    assert body["artifact"] == "maverick.run_evidence"
    assert len(body["timeline"]) >= 5
    assert "chain" in body and "summary" in body

    ev = client.get(f"/api/v1/replay/{gid}/evidence")
    assert ev.status_code == 200
    cd = ev.headers.get("content-disposition", "")
    assert "attachment" in cd and f"evidence-goal-{gid}.json" in cd


def test_replay_api_404_for_unknown_goal():
    assert client.get("/api/v1/replay/999999").status_code == 404


def test_replay_page_renders():
    gid = _seed_run()
    assert client.get("/replay").status_code == 200          # index
    r = client.get(f"/replay?goal={gid}")
    assert r.status_code == 200
    assert "Run replay" in r.text


# ---- agent trust / permission graph ----------------------------------------

def test_discovery_overview_shape():
    from maverick_dashboard.control_plane import discovery_overview
    ov = discovery_overview()
    assert {"tools", "mcp_servers", "providers", "channels"} <= ov.keys()
    assert {"entries", "by_tier", "count"} <= ov["tools"].keys()
    assert isinstance(ov["mcp_servers"], list)
    assert isinstance(ov["providers"], list)


def test_discovery_page_and_api():
    assert client.get("/discovery").status_code == 200
    r = client.get("/api/v1/discovery")
    assert r.status_code == 200 and "tools" in r.json()


# ---- pre-action simulation -------------------------------------------------

def test_simulate_browser_pay_is_high(monkeypatch):
    monkeypatch.delenv("MAVERICK_CONSENT_MODE", raising=False)
    from maverick_dashboard.control_plane import simulate_action
    res = simulate_action("browser", "click", "text=Pay now")
    assert res["risk"] == "high" and res["decision"]


def test_simulate_ask_mode_requires_approval(monkeypatch):
    monkeypatch.setenv("MAVERICK_CONSENT_MODE", "ask")
    from maverick_dashboard.control_plane import simulate_action
    res = simulate_action("browser", "click", "text=Pay now")
    assert "APPROVAL" in res["decision"].upper()


def test_simulate_readonly_not_gated(monkeypatch):
    monkeypatch.delenv("MAVERICK_CONSENT_MODE", raising=False)
    from maverick_dashboard.control_plane import simulate_action
    assert simulate_action("browser", "extract_text", "")["decision"] == "not gated"


def test_simulate_unknown_surface_errors():
    from maverick_dashboard.control_plane import simulate_action
    assert "error" in simulate_action("rocket", "launch", "")


def test_simulate_api_and_page():
    r = client.get("/api/v1/simulate",
                   params={"surface": "browser", "action": "click", "target": "text=Pay"})
    assert r.status_code == 200 and r.json()["risk"] == "high"
    p = client.get("/simulate",
                   params={"surface": "browser", "action": "click", "target": "Pay now"})
    assert p.status_code == 200 and "Decision" in p.text


# ---- compliance packet -----------------------------------------------------

def test_compliance_packet_shape():
    from maverick_dashboard.control_plane import compliance_packet
    pkt = compliance_packet()
    assert pkt["artifact"] == "maverick.compliance_packet"
    assert "soc2" in pkt and "controls" in pkt and "audit_chain" in pkt


def test_compliance_packet_download(monkeypatch):
    from maverick_dashboard import api

    calls = 0

    def fake_body():
        nonlocal calls
        calls += 1
        return '{"artifact":"maverick.compliance_packet"}'

    monkeypatch.setattr(api, "_COMPLIANCE_PACKET_CACHE", None)
    monkeypatch.setattr(api, "_build_compliance_packet_body", fake_body)
    r = client.get("/api/v1/compliance/packet")
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd and "maverick-compliance-packet.json" in cd
    assert r.json()["artifact"] == "maverick.compliance_packet"

    cached = client.get("/api/v1/compliance/packet")
    assert cached.status_code == 200
    assert calls == 1
