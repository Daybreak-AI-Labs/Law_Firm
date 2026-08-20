"""Q3 2026 batch 5: Android tool, iOS Simulator tool,
spend report tool, replay export."""
from __future__ import annotations

import json

# ---------- Android tool ----------

















# ---------- Spend report tool ----------

class _FakeEp:
    def __init__(self, id, role, cost, finished_at):
        self.id = id
        self.role = role
        self.cost_dollars = cost
        self.finished_at = finished_at


def _patch_world(monkeypatch, episodes):
    class _W:
        def list_episodes(self, limit=200):
            return episodes[:limit]

    import maverick.world_model
    monkeypatch.setattr(maverick.world_model, "WorldModel", lambda: _W())












# ---------- Replay export ----------

def _seed_audit(dir_path, goal_id, events):
    dir_path.mkdir(parents=True, exist_ok=True)
    f = dir_path / "2026-05-28.ndjson"
    with open(f, "w", encoding="utf-8") as out:
        for ev in events:
            line = dict(ev)
            line.setdefault("goal_id", goal_id)
            out.write(json.dumps(line) + "\n")
    return f


def test_replay_export_html(tmp_path, monkeypatch):
    audit = tmp_path / "audit"
    _seed_audit(audit, 42, [
        {"kind": "goal_start", "ts": 1700000000, "title": "do thing"},
        {"kind": "tool_call", "ts": 1700000010, "tool": "shell", "args": "ls"},
        {"kind": "goal_end", "ts": 1700000020, "status": "done"},
        # An event from a different goal should be excluded.
        {"kind": "goal_start", "ts": 1700000030, "goal_id": 99, "title": "other"},
    ])
    import maverick.replay.export as rex
    monkeypatch.setattr(rex, "_AUDIT_DIR", audit)
    out_file = tmp_path / "replay.html"
    n = rex.export_html(42, out_file)
    assert n == 3
    html = out_file.read_text(encoding="utf-8")
    assert "goal 42" in html
    assert "3 event" in html
    assert "tool_call" in html
    assert "other" not in html


def test_replay_export_json(tmp_path, monkeypatch):
    audit = tmp_path / "audit"
    _seed_audit(audit, 7, [
        {"kind": "goal_start", "ts": 0},
        {"kind": "tool_call", "ts": 1, "tool": "shell"},
    ])
    import maverick.replay.export as rex
    monkeypatch.setattr(rex, "_AUDIT_DIR", audit)
    out_file = tmp_path / "replay.json"
    n = rex.export_json(7, out_file)
    assert n == 2
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["goal_id"] == 7
    assert len(data["events"]) == 2


def test_replay_export_empty_goal(tmp_path, monkeypatch):
    audit = tmp_path / "audit"
    audit.mkdir()
    import maverick.replay.export as rex
    monkeypatch.setattr(rex, "_AUDIT_DIR", audit)
    out_file = tmp_path / "replay.html"
    n = rex.export_html(123, out_file)
    assert n == 0
    assert "No events recorded" in out_file.read_text(encoding="utf-8")
