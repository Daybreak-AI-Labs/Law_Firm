"""Adversarial audit-evidence export regressions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from maverick.file_lock import (
    atomic_write_text,
    ensure_private_directory,
    private_path_is_restricted,
)
from maverick.paths import data_dir, tenant_scope
from maverick.replay import export as replay_export


def _write_rows(root: Path, rows: list[dict], *, day: str = "2026-01-01") -> Path:
    ensure_private_directory(root)
    path = root / f"{day}.ndjson"
    atomic_write_text(
        path,
        "".join(json.dumps(row) + "\n" for row in rows),
        mode=0o600,
    )
    return path


def test_export_resolves_audit_root_for_each_tenant_call(tmp_path, monkeypatch):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / "home"))

    with tenant_scope(tenant="tenant-a"):
        _write_rows(
            data_dir("audit"),
            [{"goal_id": 7, "kind": "note", "value": "tenant-a"}],
        )
    with tenant_scope(tenant="tenant-b"):
        _write_rows(
            data_dir("audit"),
            [{"goal_id": 7, "kind": "note", "value": "tenant-b"}],
        )

    with tenant_scope(tenant="tenant-a"):
        out_a = tmp_path / "out-a.json"
        assert replay_export.export_json(7, out_a) == 1
        payload_a = json.loads(out_a.read_text(encoding="utf-8"))
    with tenant_scope(tenant="tenant-b"):
        out_b = tmp_path / "out-b.json"
        assert replay_export.export_json(7, out_b) == 1
        payload_b = json.loads(out_b.read_text(encoding="utf-8"))

    assert payload_a["events"][0]["value"] == "tenant-a"
    assert payload_b["events"][0]["value"] == "tenant-b"
    assert payload_a["proof"]["status"] == "unsigned"
    assert private_path_is_restricted(out_a)
    assert private_path_is_restricted(out_b)


def test_export_rejects_malformed_middle_without_replacing_output(
    tmp_path, monkeypatch,
):
    audit = ensure_private_directory(tmp_path / "audit")
    path = audit / "2026-01-01.ndjson"
    atomic_write_text(
        path,
        '{"goal_id":7,"kind":"start"}\nnot-json\n'
        '{"goal_id":7,"kind":"end"}\n',
    )
    monkeypatch.setattr(replay_export, "_AUDIT_DIR", audit)
    out = tmp_path / "replay.json"
    atomic_write_text(out, "existing-private-output")

    with pytest.raises(replay_export.ReplayEvidenceError, match="malformed row"):
        replay_export.export_json(7, out)

    assert out.read_text(encoding="utf-8") == "existing-private-output"


def test_export_rejects_forged_proof_fields(tmp_path, monkeypatch):
    audit = tmp_path / "audit"
    _write_rows(
        audit,
        [{
            "goal_id": 7,
            "kind": "forged",
            "prev_hash": "",
            "hash": "0" * 64,
            "sig": "0" * 128,
            "key_id": "0" * 16,
        }],
    )
    monkeypatch.setattr(replay_export, "_AUDIT_DIR", audit)

    with pytest.raises(replay_export.ReplayEvidenceError, match="day-chain"):
        replay_export.export_json(7, tmp_path / "forged.json")


def test_export_verifies_signed_day_and_cross_day_anchor_then_detects_tamper(
    tmp_path, monkeypatch,
):
    pytest.importorskip("cryptography")
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / "home"))
    from maverick.audit.signing import AuditSigner, ensure_anchors

    with tenant_scope(tenant="signed-tenant"):
        audit = data_dir("audit")
        day_file = audit / "2026-07-16.ndjson"
        assert AuditSigner(day_file).write(
            {"goal_id": 9, "kind": "goal_end", "status": "succeeded"}
        )
        assert ensure_anchors(audit) == 1
        out = tmp_path / "verified.json"
        assert replay_export.export_json(9, out) == 1
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["proof"]["status"] == "verified"
        assert payload["proof"]["anchors"] == "verified"

        original_row = json.loads(day_file.read_text(encoding="utf-8"))
        stripped = {
            key: value
            for key, value in original_row.items()
            if key not in {"prev_hash", "hash", "sig", "key_id"}
        }
        atomic_write_text(day_file, json.dumps(stripped) + "\n")
        with pytest.raises(replay_export.ReplayEvidenceError, match="trust material"):
            replay_export.export_json(9, out)

        row = dict(original_row)
        row["status"] = "tampered"
        atomic_write_text(day_file, json.dumps(row) + "\n")
        atomic_write_text(out, "verified-output-must-survive")
        with pytest.raises(replay_export.ReplayEvidenceError, match="day-chain"):
            replay_export.export_json(9, out)
        assert out.read_text(encoding="utf-8") == "verified-output-must-survive"


def test_export_replaces_symlink_itself_never_referent(tmp_path, monkeypatch):
    audit = tmp_path / "audit"
    _write_rows(audit, [{"goal_id": 1, "kind": "note"}])
    monkeypatch.setattr(replay_export, "_AUDIT_DIR", audit)
    referent = tmp_path / "referent.txt"
    referent.write_text("do-not-touch", encoding="utf-8")
    out = tmp_path / "replay.json"
    try:
        out.symlink_to(referent)
    except OSError:
        pytest.skip("symlink creation is not available")

    replay_export.export_json(1, out)

    assert referent.read_text(encoding="utf-8") == "do-not-touch"
    assert not out.is_symlink()
    assert private_path_is_restricted(out)
