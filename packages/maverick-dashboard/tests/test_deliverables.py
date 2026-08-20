"""The matter deliverable inbox and attorney signoff queue."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient
from maverick_dashboard.app import app
from maverick_dashboard.deliverables import build_inbox

client = TestClient(app, headers={"Origin": "http://testserver"})


@dataclass
class _Run:
    id: int
    title: str
    status: str
    updated_at: float


_OBLIGATIONS = {
    "domain": "legal_obligations",
    "deliverable": "obligations & renewals tracker",
    "shape": "table",
    "consumers": ["legal_counsel", "contracts_manager"],
    "cadence": "weekly",
    "gate": "review",
    "suite": "legal",
}
_BRIEF = {
    "domain": "legal_briefs",
    "deliverable": "draft brief with table of authorities",
    "shape": "prose",
    "consumers": ["attorney"],
    "cadence": "on-demand",
    "gate": "review",
    "suite": "legal",
}


class TestBuildInbox:
    def test_finished_gated_run_is_awaiting_signoff(self):
        runs = {"legal_obligations": [_Run(7, "Review renewals", "done", 100.0)]}
        m = build_inbox([_OBLIGATIONS], runs)
        assert len(m["awaiting"]) == 1
        assert m["awaiting"][0]["id"] == 7
        assert m["awaiting"][0]["deliverable"] == "obligations & renewals tracker"
        assert m["items"][0]["awaiting_count"] == 1

    def test_running_or_ungated_run_is_not_awaiting(self):
        # in-flight run: not finished -> not awaiting
        running = {"legal_obligations": [_Run(8, "x", "running", 1.0)]}
        assert build_inbox([_OBLIGATIONS], running)["awaiting"] == []
        # finished but the pack declares no gate -> nothing to sign off
        ungated = dict(_OBLIGATIONS, gate=None)
        done = {"legal_obligations": [_Run(9, "x", "done", 1.0)]}
        assert build_inbox([ungated], done)["awaiting"] == []

    def test_items_with_signoffs_float_to_top(self):
        runs = {"legal_obligations": [_Run(1, "o", "done", 5.0)],
                "legal_briefs": [_Run(2, "b", "running", 6.0)]}
        m = build_inbox([_BRIEF, _OBLIGATIONS], runs)
        assert m["items"][0]["domain"] == "legal_obligations"

    def test_signed_off_run_drops_out_of_awaiting(self):
        runs = {"legal_obligations": [_Run(7, "Review renewals", "done", 100.0)]}
        # finished + gated, but reviewed -> no longer awaiting
        m = build_inbox([_OBLIGATIONS], runs, signoffs={7: "approved"})
        assert m["awaiting"] == []
        assert m["items"][0]["awaiting_count"] == 0
        assert m["items"][0]["runs"][0]["signoff"] == "approved"

class TestDeliverablesPage:
    def _world(self, tmp_path, monkeypatch):
        from maverick import world_model
        monkeypatch.setattr(world_model, "DEFAULT_DB", tmp_path / "world.db")
        return world_model.WorldModel(tmp_path / "world.db")

    def test_page_renders_retained_legal_deliverables(self, tmp_path, monkeypatch):
        self._world(tmp_path, monkeypatch)
        r = client.get("/deliverables")
        assert r.status_code == 200
        assert "Deliverables" in r.text
        assert "legal_obligations" in r.text
        assert "obligations &amp; renewals tracker" in r.text

    def test_finished_legal_draft_shows_in_signoff_queue(self, tmp_path, monkeypatch):
        w = self._world(tmp_path, monkeypatch)
        gid = w.create_goal("Review obligations", "", domain="legal_obligations")
        w.set_goal_status(gid, "done", result="| Date | Duty |\n| --- | --- |\n| May 1 | Notice |\n")
        t = client.get("/deliverables").text
        assert "Awaiting attorney sign-off" in t
        assert f'href="/chat/goal/{gid}"' in t   # Review links to the deliverable view
