"""`maverick governance lineage|impact` -- the governed-action audit trail CLI."""
from __future__ import annotations

import json


def test_governance_impact_ignores_tampered_attribution(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from maverick import governed_actions as ga
    ga.record_tool_lineage(43, "shell", {"cmd": "x"}, skills=("sk",), sources=("kb",))
    ledger = tmp_path / ".maverick" / "lineage" / "43.ndjson"
    link = json.loads(ledger.read_text(encoding="utf-8"))
    link["skills"] = ["evil"]
    ledger.write_text(json.dumps(link) + "\n", encoding="utf-8")

    assert ga.impact_of("evil", kind="skill") == []
