"""maverick encryption migrate: seal pre-existing plaintext."""
from __future__ import annotations

import importlib.util
import sqlite3

import pytest
from maverick import crypto_at_rest as car

requires_crypto = pytest.mark.skipif(
    importlib.util.find_spec("cryptography") is None,
    reason="cryptography extra is not installed",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("MAVERICK_ENCRYPT_AT_REST", raising=False)
    monkeypatch.delenv("MAVERICK_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("MAVERICK_ENTERPRISE", raising=False)
    monkeypatch.setattr("maverick.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr(car, "_KEY_PATH", tmp_path / "keys" / "at_rest.key")


def test_migrate_requires_encryption_enabled(tmp_path):
    from maverick.crypto_at_rest import EncryptionUnavailable
    from maverick.encryption_migrate import migrate_world_db

    with pytest.raises(EncryptionUnavailable):
        migrate_world_db(tmp_path / "world.db")  # at_rest off -> refuse, never plaintext


@requires_crypto
def test_migrate_seals_existing_plaintext(monkeypatch, tmp_path):
    from maverick.encryption_migrate import migrate_world_db
    from maverick.world_model import WorldModel

    db = tmp_path / "world.db"
    # Write everything as plaintext (encryption off).
    wm = WorldModel(db)
    conv = wm.get_or_create_conversation("slack", "u")
    wm.append_turn(conv.id, "user", "turn SSN 123-45-6789")
    wm.upsert_fact("k", "fact 4111111111111111")
    gid = wm.create_goal("g", "d")
    wm.append_message(gid, "user", "message secret")
    qid = wm.ask("question secret", goal_id=gid)
    wm.answer(qid, "answer secret")

    # Enable encryption and seal the existing rows.
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    assert migrate_world_db(db) == {
        "turns.content": 1, "facts.value": 1, "messages.content": 1,
        "questions.question": 1, "questions.answer": 1,
        "goals.title": 1, "goals.description": 1, "goals.result": 0,
        "goal_events.content": 0,
        "episodes.summary": 0, "episodes.outcome": 0,
        "approvals.action": 0, "approvals.scope": 0, "approvals.detail": 0,
        "signoffs.note": 0, "artifacts.title": 0, "artifacts.content": 0,
        "artifacts.title_key": 0,
        "clients.name": 0,
        "projects.name": 0, "projects.description": 0,
        "projects.matter_number": 0, "projects.jurisdiction": 0,
        "matter_parties.name": 0,
        "attachments.filename": 0, "attachments.path": 0,
        "fact_history.value": 0,
        "matter_turns.content": 0, "goal_feedback.note": 0,
        "harness_corpus.row": 0,
        "attachments.files_renamed": 0,
    }

    # On disk the columns are now sealed (ciphertext, no plaintext).
    c = sqlite3.connect(str(db))
    turn = c.execute("SELECT content FROM turns").fetchone()[0]
    assert turn.startswith("MVKAR1:") and "123-45-6789" not in turn
    assert c.execute("SELECT value FROM facts").fetchone()[0].startswith("MVKAR1:")
    assert c.execute("SELECT title FROM goals").fetchone()[0].startswith("MVKAR1:")

    # Reads still return plaintext.
    wm2 = WorldModel(db)
    assert wm2.recent_turns(conv.id)[-1].content == "turn SSN 123-45-6789"
    assert wm2.get_fact("k") == "fact 4111111111111111"
    assert wm2.get_goal(gid).title == "g"          # goal content round-trips

    # Idempotent: a second run seals nothing.
    assert migrate_world_db(db) == {
        "turns.content": 0, "facts.value": 0, "messages.content": 0,
        "questions.question": 0, "questions.answer": 0,
        "goals.title": 0, "goals.description": 0, "goals.result": 0,
        "goal_events.content": 0,
        "episodes.summary": 0, "episodes.outcome": 0,
        "approvals.action": 0, "approvals.scope": 0, "approvals.detail": 0,
        "signoffs.note": 0, "artifacts.title": 0, "artifacts.content": 0,
        "artifacts.title_key": 0,
        "clients.name": 0,
        "projects.name": 0, "projects.description": 0,
        "projects.matter_number": 0, "projects.jurisdiction": 0,
        "matter_parties.name": 0,
        "attachments.filename": 0, "attachments.path": 0,
        "fact_history.value": 0,
        "matter_turns.content": 0, "goal_feedback.note": 0,
        "harness_corpus.row": 0,
        "attachments.files_renamed": 0,
    }


@requires_crypto
def test_migrate_seals_artifacts_projects_signoffs_and_fact_history(monkeypatch, tmp_path):
    """Regression: _SEALED_COLUMNS omitted artifact titles/content, projects.name/
    description, signoffs.note and fact_history.value, so legacy plaintext in
    them survived `maverick encryption migrate` (and strict mode then withheld
    it forever)."""
    from maverick.encryption_migrate import migrate_world_db
    from maverick.world_model import WorldModel

    db = tmp_path / "world.db"
    monkeypatch.setenv("MAVERICK_TEMPORAL_MEMORY", "1")   # populate fact_history
    # Write everything as plaintext (encryption off).
    wm = WorldModel(db)
    principal = "user:attorney@example.test"
    pid = wm.create_project(
        "project name secret",
        description="project desc secret",
        owner=principal,
        domain="legal",
    )
    gid = wm.create_goal("g", "d", project_id=pid, domain="legal")
    wm.add_artifact(gid, "text", "t", "artifact body secret")
    wm.set_goal_status(gid, "done", result="draft")
    wm.record_signoff(gid, "approved", note="signoff note secret")
    wm.upsert_fact("k", "fact history secret")
    conversation = wm.get_or_create_matter_conversation(
        "dashboard", "attorney", pid, principal=principal
    )
    assert conversation is not None
    assert wm.append_matter_turn(
        conversation.id,
        project_id=pid,
        principal=principal,
        role="user",
        content="matter turn secret",
        goal_id=gid,
    )
    assert wm.record_matter_feedback(
        gid,
        principal=principal,
        rating="up",
        value=1.0,
        note="feedback note secret",
    )
    wm.conn.close()

    # Enable encryption and seal the existing rows.
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    blocked = WorldModel(db)
    with pytest.raises(RuntimeError, match="artifact title migration required"):
        blocked.add_artifact(gid, "text", "t", "must not split version history")
    blocked.conn.close()
    report = migrate_world_db(db)
    assert report["artifacts.title"] == 1
    assert report["artifacts.content"] == 1
    assert report["artifacts.title_key"] == 1
    assert report["projects.name"] == 1
    assert report["projects.description"] == 1
    assert report["signoffs.note"] == 1
    assert report["fact_history.value"] == 1
    assert report["matter_turns.content"] == 1
    assert report["goal_feedback.note"] == 1

    # On disk the columns are now sealed ciphertext (no plaintext residue).
    c = sqlite3.connect(str(db))
    title, title_key, content = c.execute(
        "SELECT title, title_key, content FROM artifacts"
    ).fetchone()
    assert car.is_sealed_str(title) and title != "t"
    assert title_key.startswith("h1:") and len(title_key) == 67
    assert car.is_sealed_str(content) and "secret" not in content
    name, desc = c.execute("SELECT name, description FROM projects").fetchone()
    assert car.is_sealed_str(name) and car.is_sealed_str(desc)
    assert car.is_sealed_str(c.execute("SELECT note FROM signoffs").fetchone()[0])
    assert car.is_sealed_str(c.execute("SELECT value FROM fact_history").fetchone()[0])
    assert car.is_sealed_str(c.execute("SELECT content FROM matter_turns").fetchone()[0])
    assert car.is_sealed_str(c.execute("SELECT note FROM goal_feedback").fetchone()[0])
    c.close()

    # Reads still return plaintext.
    wm2 = WorldModel(db)
    artifacts = wm2.artifacts_for_goal(gid)
    assert artifacts[0]["title"] == "t"
    assert artifacts[0]["content"] == "artifact body secret"
    assert wm2.signoff_for(gid)["note"] == "signoff note secret"
    wm2.add_artifact(gid, "text", "t", "second version")
    artifacts = wm2.artifacts_for_goal(gid)
    assert [artifact["version"] for artifact in artifacts] == [1, 2]
    other_gid = wm2.create_goal(
        "other", "", project_id=pid, domain="legal"
    )
    wm2.add_artifact(other_gid, "text", "t", "same title, other goal")
    with sqlite3.connect(str(db)) as raw:
        same_goal_rows = raw.execute(
            "SELECT title, title_key FROM artifacts WHERE goal_id = ? "
            "ORDER BY version",
            (gid,),
        ).fetchall()
        other_key = raw.execute(
            "SELECT title_key FROM artifacts WHERE goal_id = ?", (other_gid,)
        ).fetchone()[0]
    assert all(
        car.is_sealed_str(row[0]) and row[0] != "t"
        for row in same_goal_rows
    )
    assert [row[1] for row in same_goal_rows] == [title_key, title_key]
    assert other_key != title_key
    p = wm2.get_project(pid)
    assert p["name"] == "project name secret"
    assert p["description"] == "project desc secret"
    assert wm2.signoff_for(gid) is None  # new version invalidates prior review
    assert wm2.fact_history("k")[0].value == "fact history secret"
    assert wm2.recent_matter_turns(
        conversation.id, project_id=pid, principal=principal
    )[0].content == "matter turn secret"
    assert wm2.matter_feedback_for_goal(
        gid, principal=principal
    )["note"] == "feedback note secret"


@requires_crypto
def test_migrate_upgrades_v39_and_backfills_artifact_title_in_one_run(
    monkeypatch, tmp_path,
):
    from maverick.encryption_migrate import migrate_world_db
    from maverick.world_model import SCHEMA_VERSION, WorldModel

    db = tmp_path / "world.db"
    world = WorldModel(db)
    goal_id = world.create_goal("legacy", "")
    world.add_artifact(goal_id, "text", "Privileged strategy", "secret body")
    world.close()
    with sqlite3.connect(str(db)) as legacy:
        legacy.execute("DROP INDEX idx_artifacts_goal_title")
        legacy.execute("ALTER TABLE artifacts DROP COLUMN title_key")
        legacy.execute("UPDATE schema_version SET version = 39")
        legacy.commit()

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    report = migrate_world_db(db)

    assert report["artifacts.title"] == 1
    assert report["artifacts.title_key"] == 1
    with sqlite3.connect(str(db)) as current:
        assert current.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0] == SCHEMA_VERSION
        title, title_key = current.execute(
            "SELECT title, title_key FROM artifacts WHERE goal_id = ?", (goal_id,)
        ).fetchone()
    assert car.is_sealed_str(title)
    assert title_key.startswith("h1:") and len(title_key) == 67


@requires_crypto
def test_migrate_seals_attachment_metadata_and_removes_legacy_filename(
    monkeypatch, tmp_path,
):
    from maverick.attachments import store
    from maverick.encryption_migrate import migrate_world_db
    from maverick.world_model import WorldModel

    db = tmp_path / "world.db"
    wm = WorldModel(db)
    goal_id = wm.create_goal("attachment migration", "")
    stored = store(
        goal_id,
        "Privileged Client Memo.txt",
        "text/plain",
        b"attorney work product",
        root=tmp_path / "attachments",
    )
    legacy = stored.path.with_name(
        f"{stored.sha256[:16]}-Privileged Client Memo.txt"
    )
    stored.path.rename(legacy)
    attachment_id = wm.add_attachment(
        goal_id,
        stored.filename,
        stored.mime,
        stored.size_bytes,
        stored.sha256,
        str(legacy),
    )
    wm.conn.close()
    assert legacy.exists()

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    report = migrate_world_db(db)
    assert report["attachments.filename"] == 1
    assert report["attachments.path"] == 1
    assert report["attachments.files_renamed"] == 1

    opaque = legacy.with_name(f"{stored.sha256}-{attachment_id}")
    assert opaque.exists()
    assert not legacy.exists()
    with sqlite3.connect(str(db)) as connection:
        raw_filename, raw_path = connection.execute(
            "SELECT filename, path FROM attachments"
        ).fetchone()
    assert car.is_sealed_str(raw_filename)
    assert car.is_sealed_str(raw_path)
    assert "Privileged Client Memo" not in raw_filename
    assert "Privileged Client Memo" not in raw_path

    attachment = WorldModel(db).list_attachments(goal_id)[0]
    assert attachment.filename == "Privileged Client Memo.txt"
    assert attachment.path == str(opaque)


def _legacy_named_attachment(tmp_path):
    from maverick.attachments import store
    from maverick.world_model import WorldModel

    db = tmp_path / "world.db"
    world = WorldModel(db)
    goal_id = world.create_goal("attachment migration", "")
    stored = store(
        goal_id,
        "Privileged Client Memo.txt",
        "text/plain",
        b"attorney work product",
        root=tmp_path / "attachments",
    )
    legacy = stored.path.with_name(
        f"{stored.sha256[:16]}-Privileged Client Memo.txt"
    )
    stored.path.rename(legacy)
    attachment_id = world.add_attachment(
        goal_id,
        stored.filename,
        stored.mime,
        stored.size_bytes,
        stored.sha256,
        str(legacy),
    )
    world.close()
    opaque = legacy.with_name(f"{stored.sha256}-{attachment_id}")
    return db, goal_id, attachment_id, legacy, opaque, stored.sha256


@requires_crypto
def test_attachment_rename_journal_recovers_crash_before_db_commit(
    monkeypatch, tmp_path,
):
    import maverick.encryption_migrate as migration

    db, goal_id, _, legacy, opaque, _ = _legacy_named_attachment(tmp_path)
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    original_apply = migration._apply_attachment_file_moves

    def crash_after_rename(moves):
        original_apply(moves)
        raise KeyboardInterrupt("simulated process death before metadata commit")

    monkeypatch.setattr(
        migration, "_apply_attachment_file_moves", crash_after_rename
    )
    with pytest.raises(KeyboardInterrupt, match="before metadata commit"):
        migration.migrate_world_db(db)

    journal = db.with_name(db.name + ".attachment-migration.journal")
    journal_token = journal.read_text(encoding="ascii")
    assert car.is_sealed_str(journal_token)
    assert "Privileged Client Memo" not in journal_token
    assert opaque.exists() and not legacy.exists()

    monkeypatch.setattr(
        migration, "_apply_attachment_file_moves", original_apply
    )
    report = migration.migrate_world_db(db)
    assert report["attachments.files_renamed"] == 1
    assert opaque.exists() and not legacy.exists()
    assert not journal.exists()
    from maverick.world_model import WorldModel

    world = WorldModel(db)
    assert world.list_attachments(goal_id)[0].path == str(opaque)
    world.close()


@requires_crypto
def test_attachment_rename_journal_recovers_crash_after_db_commit(
    monkeypatch, tmp_path,
):
    import maverick.encryption_migrate as migration

    db, goal_id, _, legacy, opaque, _ = _legacy_named_attachment(tmp_path)
    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    original_recover = migration._recover_attachment_move_journal
    calls = 0

    def crash_before_journal_cleanup(conn, db_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("simulated process death after metadata commit")
        return original_recover(conn, db_path)

    monkeypatch.setattr(
        migration, "_recover_attachment_move_journal", crash_before_journal_cleanup
    )
    with pytest.raises(KeyboardInterrupt, match="after metadata commit"):
        migration.migrate_world_db(db)

    journal = db.with_name(db.name + ".attachment-migration.journal")
    assert journal.exists()
    assert opaque.exists() and not legacy.exists()
    with sqlite3.connect(str(db)) as connection:
        stored_path = connection.execute(
            "SELECT path FROM attachments"
        ).fetchone()[0]
    assert car.unseal_from_str(stored_path) == str(opaque)

    monkeypatch.setattr(
        migration, "_recover_attachment_move_journal", original_recover
    )
    report = migration.migrate_world_db(db)
    assert report["attachments.files_renamed"] == 0
    assert opaque.exists() and not legacy.exists()
    assert not journal.exists()
    from maverick.world_model import WorldModel

    world = WorldModel(db)
    assert world.list_attachments(goal_id)[0].path == str(opaque)
    world.close()


@requires_crypto
def test_migrate_seals_malformed_prefixed_plaintext(monkeypatch, tmp_path):
    from maverick.encryption_migrate import migrate_world_db
    from maverick.world_model import WorldModel

    db = tmp_path / "world.db"
    wm = WorldModel(db)
    gid = wm.create_goal("MVKAR1:legacy prefixed title", "d")
    wm.append_event(gid, "agent-1", "note", "MVKAR1:legacy prefixed event")

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    report = migrate_world_db(db)
    assert report["goals.title"] == 1
    assert report["goal_events.content"] == 1

    c = sqlite3.connect(str(db))
    raw_title = c.execute("SELECT title FROM goals WHERE id=?", (gid,)).fetchone()[0]
    raw_event = c.execute("SELECT content FROM goal_events WHERE goal_id=?", (gid,)).fetchone()[0]
    assert car.is_sealed_str(raw_title)
    assert car.is_sealed_str(raw_event)
    assert raw_title != "MVKAR1:legacy prefixed title"
    assert raw_event != "MVKAR1:legacy prefixed event"

    wm2 = WorldModel(db)
    assert wm2.get_goal(gid).title == "MVKAR1:legacy prefixed title"
    assert wm2.goal_events(gid)[0].content == "MVKAR1:legacy prefixed event"


@requires_crypto
def test_migrate_dry_run_writes_nothing(monkeypatch, tmp_path):
    from maverick.encryption_migrate import migrate_world_db
    from maverick.world_model import WorldModel

    db = tmp_path / "world.db"
    wm = WorldModel(db)
    conv = wm.get_or_create_conversation("slack", "u")
    wm.append_turn(conv.id, "user", "still plaintext")

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    assert migrate_world_db(db, dry_run=True)["turns.content"] == 1
    # Unchanged on disk.
    raw = sqlite3.connect(str(db)).execute("SELECT content FROM turns").fetchone()[0]
    assert raw == "still plaintext"


def _all_db_bytes(db):
    """The DB main file + its WAL/SHM sidecars, concatenated."""
    blob = db.read_bytes() if db.exists() else b""
    for suf in ("-wal", "-shm"):
        p = db.with_name(db.name + suf)
        if p.exists():
            blob += p.read_bytes()
    return blob


@requires_crypto
def test_migrate_default_writes_no_backup(monkeypatch, tmp_path):
    from maverick.encryption_migrate import migrate_world_db
    from maverick.world_model import WorldModel

    db = tmp_path / "world.db"
    wm = WorldModel(db)
    wm.create_goal("g", "d")
    wm.conn.close()

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    migrate_world_db(db)
    assert list(tmp_path.glob("world.db.pre-encrypt-*.bak")) == []
    live = sqlite3.connect(str(db)).execute("SELECT title FROM goals").fetchone()[0]
    assert car.is_sealed_str(live)


@requires_crypto
def test_migrate_dry_run_makes_no_backup(monkeypatch, tmp_path):
    from maverick.encryption_migrate import migrate_world_db
    from maverick.world_model import WorldModel

    db = tmp_path / "world.db"
    wm = WorldModel(db)
    wm.create_goal("g 4111111111111111", "d")
    wm.conn.close()

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    migrate_world_db(db, dry_run=True)
    assert list(tmp_path.glob("world.db.pre-encrypt-*.bak")) == []


@requires_crypto
def test_migrate_leaves_no_plaintext_residue_in_the_db_file(monkeypatch, tmp_path):
    from maverick.encryption_migrate import migrate_world_db
    from maverick.world_model import WorldModel

    db = tmp_path / "world.db"
    wm = WorldModel(db)
    marker = "RESIDUE-MARKER-4111111111111111"
    wm.create_goal(marker, "desc")
    wm.conn.close()                                   # release the DB (offline migrate)
    assert marker.encode() in _all_db_bytes(db)       # plaintext present before

    monkeypatch.setenv("MAVERICK_ENCRYPT_AT_REST", "1")
    migrate_world_db(db)

    # secure_delete zeroed the freed cells + VACUUM/checkpoint rebuilt the file:
    # the pre-encryption plaintext is gone from the DB file and the WAL sidecar.
    assert marker.encode() not in _all_db_bytes(db)
