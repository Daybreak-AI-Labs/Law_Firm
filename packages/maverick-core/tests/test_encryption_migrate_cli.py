"""The supported CLI exposes the firm at-rest migration without plaintext backups."""
from __future__ import annotations

import sqlite3

from click.testing import CliRunner
from maverick.cli import main


def test_encryption_migrate_is_reachable_from_help():
    runner = CliRunner()

    root = runner.invoke(main, ["--help"])
    assert root.exit_code == 0
    assert "encryption" in root.output

    group = runner.invoke(main, ["encryption", "--help"])
    assert group.exit_code == 0
    assert "migrate" in group.output

    command = runner.invoke(main, ["encryption", "migrate", "--help"])
    assert command.exit_code == 0
    assert "--dry-run" in command.output
    assert "plaintext" in command.output and "recovery copy" in command.output


def test_encryption_migrate_dry_run_reports_and_writes_nothing(monkeypatch, tmp_path):
    from maverick.world_model import WorldModel

    db = tmp_path / "world.db"
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "0")
    world = WorldModel(db)
    goal_id = world.create_goal("Privileged Client Name", "legacy matter brief")
    world.close()

    before = db.read_bytes()
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    result = CliRunner().invoke(
        main,
        ["--db", str(db), "encryption", "migrate", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "Encryption migration dry run" in result.output
    assert "goals.title: 1" in result.output
    assert "goals.description: 1" in result.output
    assert db.read_bytes() == before
    assert not list(tmp_path.glob("*.bak"))
    raw = sqlite3.connect(str(db)).execute(
        "SELECT title FROM goals WHERE id = ?", (goal_id,),
    ).fetchone()[0]
    assert raw == "Privileged Client Name"
