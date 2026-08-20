"""Backup / restore / DR for a single-client deployment: consistent snapshot of
the client data root, round-trip restore, the cross-client fail-closed guard,
and path-traversal-safe extraction."""
from __future__ import annotations

import io
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from maverick import backup, client, file_lock


@pytest.fixture(autouse=True)
def _bound_client(monkeypatch, tmp_path):
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path))
    monkeypatch.setenv("MAVERICK_CLIENT_ID", "acme")
    monkeypatch.setenv("MAVERICK_BACKUP_SIGNING_KEY", bytes(range(32)).hex())
    monkeypatch.setenv(
        "MAVERICK_BACKUP_ENCRYPTION_KEY",
        bytes(range(32, 64)).hex(),
    )
    monkeypatch.delenv("MAVERICK_TENANT", raising=False)
    client.reset_client_cache()
    yield
    client.reset_client_cache()


def _seed(root):
    """Write a tiny world DB + audit-ish files into the client data root."""
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(root / "world.db"))
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('secret-data')")
    con.commit()
    con.close()
    (root / "audit").mkdir(exist_ok=True)
    (root / "audit" / "2026-06-18.ndjson").write_text('{"kind":"x"}\n')
    (root / "agent_trust.json").write_text('[{"id":"vega"}]')


@contextmanager
def _plaintext_archive_path(archive):
    """Expose a decrypted inner tar only inside a test-private temp tree."""
    with tempfile.TemporaryDirectory(prefix="mvk-backup-test-") as temporary:
        inner = Path(temporary) / "archive.tgz"
        with backup._plaintext_archive(
            archive,
            allow_legacy_auth_off=False,
        ) as (stream, legacy, _key):
            assert legacy is False
            with inner.open("wb") as output:
                shutil.copyfileobj(stream, output)
        yield inner


def _encrypt_plain_archive(source, destination):
    key = backup._encryption_key(required=True)
    assert key is not None
    backup._encrypt_archive_file(Path(source), Path(destination), key=key)
    return Path(destination)


def test_create_and_restore_round_trip():
    from maverick.paths import data_dir
    root = data_dir()
    _seed(root)
    tar = backup.create_backup()
    assert tar.exists()
    assert file_lock.private_path_is_restricted(tar)

    # Wipe + restore.
    (root / "world.db").unlink()
    (root / "agent_trust.json").unlink()
    backup.restore_backup(tar)

    con = sqlite3.connect(str(root / "world.db"))
    assert con.execute("SELECT v FROM t").fetchone()[0] == "secret-data"
    con.close()
    assert (root / "agent_trust.json").read_text() == '[{"id":"vega"}]'
    assert file_lock.private_path_is_restricted(root / "world.db")
    assert file_lock.private_path_is_restricted(root / "agent_trust.json")


def test_manifest_records_client():
    from maverick.paths import data_dir
    _seed(data_dir())
    tar = backup.create_backup()
    m = backup.read_manifest(tar)
    assert m["client_id"] == "acme" and m["schema"] == backup.SCHEMA
    assert "world.db" in m["files"]


def test_operator_cli_create_verify_and_deliberate_restore(tmp_path):
    from click.testing import CliRunner
    from maverick.cli import main
    from maverick.paths import data_dir

    root = data_dir()
    _seed(root)
    archive = tmp_path / "operator-dr.mvkb"
    runner = CliRunner()

    created = runner.invoke(main, ["backup", "create", str(archive)])
    assert created.exit_code == 0, created.output
    assert archive.exists()
    assert "Encrypted backup created" in created.output

    verified = runner.invoke(main, ["backup", "verify", str(archive)])
    assert verified.exit_code == 0, verified.output
    assert "client='acme'" in verified.output

    (root / "agent_trust.json").unlink()
    declined = runner.invoke(
        main,
        ["backup", "restore", str(archive)],
        input="n\n",
    )
    assert declined.exit_code != 0
    assert not (root / "agent_trust.json").exists()

    restored = runner.invoke(
        main,
        ["backup", "restore", "--yes", str(archive)],
    )
    assert restored.exit_code == 0, restored.output
    assert (root / "agent_trust.json").read_text() == '[{"id":"vega"}]'


def test_restore_refuses_cross_client(monkeypatch):
    from maverick.paths import data_dir
    _seed(data_dir())
    tar = backup.create_backup()  # client_id = acme
    # Now this deployment is a DIFFERENT client.
    monkeypatch.setenv("MAVERICK_CLIENT_ID", "beta")
    client.reset_client_cache()
    with pytest.raises(backup.BackupError):
        backup.restore_backup(tar)
    # force overrides.
    backup.restore_backup(tar, force=True)


def test_restore_rejects_file_not_in_manifest(tmp_path):
    # The manifest is an exhaustive allow-list. A payload file with no manifest
    # entry (and thus no recorded SHA-256) is a corrupt/tampered archive trying
    # to write an unverified file into the live root; restore must reject it,
    # not silently pass it through (the old `want is not None` guard skipped it).
    from maverick.paths import data_dir
    _seed(data_dir())
    tar = backup.create_backup()

    tampered_inner = tmp_path / "tampered-inner.tgz"
    tampered = tmp_path / "tampered.mvkb"
    extra = tmp_path / "sneaky"
    extra.write_text("unverified payload")
    with _plaintext_archive_path(tar) as inner:
        with tarfile.open(inner, "r:gz") as src, tarfile.open(
            tampered_inner, "w:gz",
        ) as dst:
            for m in src.getmembers():
                dst.addfile(m, src.extractfile(m) if m.isfile() else None)
            dst.add(extra, arcname="data/sneaky.txt")
    _encrypt_plain_archive(tampered_inner, tampered)

    with pytest.raises(backup.BackupError, match="not in the manifest"):
        backup.restore_backup(tampered)


def test_restore_rejects_path_traversal(tmp_path):
    # Hand-craft a malicious tarball with a ../ member.
    bad = tmp_path / "evil.tgz"
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "x").write_text("data")
    import json
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema": 1, "client_id": "acme", "files": {}}))
    with tarfile.open(bad, "w:gz") as tar:
        tar.add(manifest, arcname="manifest.json")
        tar.add(payload / "x", arcname="data/../../escape")
    with pytest.raises(backup.BackupError):
        backup.restore_backup(bad, allow_legacy_auth_off=True)


def test_backup_excludes_prior_backups(monkeypatch):
    """The backups/ subtree lives under the client root; it must NOT be swept
    into a new backup, or each backup would contain every earlier one and grow
    quadratically."""
    from maverick.paths import data_dir
    _seed(data_dir())
    first = backup.create_backup()
    assert first.exists()
    # A second backup must not contain the first .tgz under data/backups/.
    second = backup.create_backup()
    with _plaintext_archive_path(second) as inner:
        with tarfile.open(inner, "r:gz") as tar:
            members = tar.getnames()
    assert not any(name.startswith("data/backups/") for name in members), members
    assert "data/world.db" in members


def _repack(
    src_tar,
    dst_tar,
    *,
    mutate_manifest=None,
    mutate_data=None,
    resign=False,
    encrypt=True,
):
    """Rebuild a backup tarball, optionally mutating the manifest dict or a
    named data file's bytes — to forge corrupt / schema-incompatible backups."""
    import io
    import json
    with _plaintext_archive_path(src_tar) as inner:
        with tarfile.open(inner, "r:gz") as t:
            members = t.getmembers()
            blobs = {
                m.name: (t.extractfile(m).read() if m.isfile() else None)
                for m in members
            }
    manifest = json.loads(blobs["manifest.json"].decode())
    if mutate_manifest:
        mutate_manifest(manifest)
    if resign:
        manifest.pop("authentication", None)
        key = backup._operator_key(required=True)
        manifest = backup._authenticate_for_write(manifest, key)
    blobs["manifest.json"] = json.dumps(manifest).encode()
    if mutate_data:
        name, data = mutate_data
        blobs[name] = data
    with tempfile.TemporaryDirectory(prefix="mvk-backup-repack-") as temporary:
        plaintext = Path(temporary) / "archive.tgz"
        with tarfile.open(plaintext, "w:gz") as t:
            for m in members:
                if not m.isfile():
                    continue
                info = tarfile.TarInfo(m.name)
                info.size = len(blobs[m.name])
                t.addfile(info, io.BytesIO(blobs[m.name]))
        if encrypt:
            _encrypt_plain_archive(plaintext, dst_tar)
        else:
            shutil.copyfile(plaintext, dst_tar)


def test_restore_refuses_forward_schema(tmp_path, monkeypatch):
    from maverick.paths import data_dir
    from maverick.world_model import SCHEMA_VERSION
    _seed(data_dir())
    tar = backup.create_backup()
    forward = tmp_path / "forward.tgz"
    _repack(tar, forward,
            mutate_manifest=lambda m: m.update(world_schema_version=SCHEMA_VERSION + 5),
            resign=True)
    (data_dir() / "world.db").unlink()
    with pytest.raises(backup.BackupError, match="newer than this binary"):
        backup.restore_backup(forward)
    # force overrides the guard.
    backup.restore_backup(forward, force=True)


def test_restore_detects_corruption(tmp_path):
    from maverick.paths import data_dir
    _seed(data_dir())
    tar = backup.create_backup()
    corrupt = tmp_path / "corrupt.tgz"
    # Flip the agent_trust.json bytes but keep the manifest's recorded SHA-256.
    _repack(tar, corrupt, mutate_data=("data/agent_trust.json", b"TAMPERED"))
    original = (data_dir() / "agent_trust.json").read_text()
    with pytest.raises(backup.BackupError, match="integrity check failed"):
        backup.restore_backup(corrupt)
    # Verify-then-write: the live root was NOT partially overwritten.
    assert (data_dir() / "agent_trust.json").read_text() == original


def test_restore_clears_stale_wal_sidecar():
    """DR scenario: a stale, uncheckpointed world.db-wal in the live root must
    NOT survive a restore and replay post-backup mutations back over the
    restored DB. Backups exclude the -wal/-shm sidecars on purpose (the
    consistent .db copy already folds them in), so restore is responsible for
    removing any pre-existing ones in the live root."""
    from maverick.paths import data_dir
    root = data_dir()

    # Baseline world.db (WAL mode, fully checkpointed) holding only the content
    # the backup will capture.
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(root / "world.db"))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (v TEXT)")
    con.execute("INSERT INTO t VALUES ('secret-data')")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    (root / "agent_trust.json").write_text('[{"id":"vega"}]')

    tar = backup.create_backup()  # snapshots the clean baseline only

    # A post-backup mutation that lives ONLY in the WAL (never checkpointed):
    # keep the connection open so SQLite cannot checkpoint, capture the live
    # sidecar bytes, then close. Writing those bytes back recreates exactly the
    # post-crash stale-WAL-on-disk state (close-time checkpoint truncates it,
    # so we must reconstruct it to model the hard-crash case).
    con = sqlite3.connect(str(root / "world.db"))
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("INSERT INTO t VALUES ('post-backup-mutation')")
    con.commit()
    wal_bytes = (root / "world.db-wal").read_bytes()
    shm_path = root / "world.db-shm"
    shm_bytes = shm_path.read_bytes() if shm_path.exists() else None
    assert wal_bytes, "test needs a non-empty stale WAL"
    con.close()
    (root / "world.db-wal").write_bytes(wal_bytes)
    if shm_bytes is not None:
        shm_path.write_bytes(shm_bytes)
    assert (root / "world.db-wal").stat().st_size > 0

    backup.restore_backup(tar, force=True)

    # The stale WAL must be gone, and reopening must show ONLY the backed-up
    # content — the post-backup mutation must not have been replayed back in.
    assert not (root / "world.db-wal").exists()
    con = sqlite3.connect(str(root / "world.db"))
    rows = {r[0] for r in con.execute("SELECT v FROM t").fetchall()}
    con.close()
    assert rows == {"secret-data"}, rows


def test_restore_is_atomic_and_leaves_no_part_temp(monkeypatch):
    """Crash-consistent restore: each file lands via a fsync'd temp + os.replace
    (not a torn direct write), no .part temp survives, and a DB's stale sidecar
    is cleared BEFORE the restored .db lands -- so no crash instant pairs the new
    .db with the old WAL."""
    import os as _os

    from maverick.paths import data_dir
    root = data_dir()
    _seed(root)
    tar = backup.create_backup()

    # A stale, pre-existing sidecar in the live root (the post-crash DR case).
    (root / "world.db").unlink()
    (root / "world.db-wal").write_bytes(b"stale-wal-bytes")

    real_replace = _os.replace
    seen = {"replaced": [], "wal_present_at_db_replace": None}

    def _spy_replace(a, b, **kw):
        b_path = Path(b)
        seen["replaced"].append(b_path.name)
        if b_path.suffix == ".db":
            seen["wal_present_at_db_replace"] = (root / "world.db-wal").exists()
        return real_replace(a, b, **kw)

    monkeypatch.setattr(backup.os, "replace", _spy_replace)
    backup.restore_backup(tar, force=True)

    # Correct contents restored.
    con = sqlite3.connect(str(root / "world.db"))
    assert con.execute("SELECT v FROM t").fetchone()[0] == "secret-data"
    con.close()
    assert (root / "agent_trust.json").read_text() == '[{"id":"vega"}]'

    # No leftover temp anywhere under the root.
    assert list(root.rglob("*.part")) == []

    # Every restored file went through os.replace (atomic write), and by the time
    # the .db was swapped in its stale sidecar was already gone.
    assert "world.db" in seen["replaced"]
    assert seen["wal_present_at_db_replace"] is False
    assert not (root / "world.db-wal").exists()


def test_create_errors_when_no_data(monkeypatch, tmp_path):
    # Point at an empty home with a fresh client -> no data root.
    monkeypatch.setenv("MAVERICK_HOME", str(tmp_path / "empty"))
    client.reset_client_cache()
    with pytest.raises(backup.BackupError):
        backup.create_backup()




def test_create_requires_operator_key_by_default(monkeypatch):
    from maverick.paths import data_dir

    _seed(data_dir())
    monkeypatch.delenv("MAVERICK_BACKUP_SIGNING_KEY")

    with pytest.raises(backup.BackupError, match="MAVERICK_BACKUP_SIGNING_KEY"):
        backup.create_backup()


def test_create_and_open_require_separate_encryption_key(monkeypatch):
    from maverick.paths import data_dir

    _seed(data_dir())
    archive = backup.create_backup()
    monkeypatch.delenv("MAVERICK_BACKUP_ENCRYPTION_KEY")

    with pytest.raises(backup.BackupError, match="MAVERICK_BACKUP_ENCRYPTION_KEY"):
        backup.read_manifest(archive)
    with pytest.raises(backup.BackupError, match="MAVERICK_BACKUP_ENCRYPTION_KEY"):
        backup.create_backup()


def test_signing_and_encryption_key_values_must_be_independent(monkeypatch):
    from maverick.paths import data_dir

    _seed(data_dir())
    signing = os.environ["MAVERICK_BACKUP_SIGNING_KEY"]
    monkeypatch.setenv("MAVERICK_BACKUP_ENCRYPTION_KEY", signing)

    with pytest.raises(backup.BackupError, match="independent key values"):
        backup.create_backup()


def test_archive_hides_plaintext_and_excludes_data_root_keys():
    from maverick.paths import data_dir

    root = data_dir()
    _seed(root)
    (root / "keys").mkdir()
    (root / "keys" / "at_rest.key").write_bytes(b"AT-REST-KEY-MATERIAL")
    (root / "audit" / "keys").mkdir()
    (root / "audit" / "keys" / "signing.key").write_bytes(
        b"AUDIT-SIGNING-KEY-MATERIAL"
    )

    archive = backup.create_backup()
    encrypted = archive.read_bytes()
    assert archive.suffix == ".mvkb"
    assert encrypted.startswith(backup._ENCRYPTED_MAGIC)
    assert b"secret-data" not in encrypted
    assert b'[{"id":"vega"}]' not in encrypted
    assert b"AT-REST-KEY-MATERIAL" not in encrypted
    assert b"AUDIT-SIGNING-KEY-MATERIAL" not in encrypted
    for environment in (
        "MAVERICK_BACKUP_SIGNING_KEY",
        "MAVERICK_BACKUP_ENCRYPTION_KEY",
    ):
        encoded_key = os.environ[environment].encode("ascii")
        raw_key = bytes.fromhex(os.environ[environment])
        assert encoded_key not in encrypted
        assert raw_key not in encrypted

    manifest = backup.read_manifest(archive)
    assert not any(
        rel == "keys" or rel.startswith("keys/") or rel.startswith("audit/keys/")
        for rel in manifest["files"]
    )

    shutil.rmtree(root / "keys")
    shutil.rmtree(root / "audit" / "keys")
    backup.restore_backup(archive)
    assert not (root / "keys").exists()
    assert not (root / "audit" / "keys").exists()


def test_wrong_encryption_key_fails_before_restore_staging(monkeypatch):
    from maverick.paths import data_dir

    _seed(data_dir())
    archive = backup.create_backup()
    monkeypatch.setenv(
        "MAVERICK_BACKUP_ENCRYPTION_KEY",
        bytes(range(64, 96)).hex(),
    )

    def _must_not_stage(*_args, **_kwargs):
        raise AssertionError("payload staging ran before GCM authentication")

    monkeypatch.setattr(backup, "_extract_payload_stream", _must_not_stage)
    with pytest.raises(backup.BackupError, match="wrong key or tampering"):
        backup.restore_backup(archive)


def test_ciphertext_tamper_fails_before_restore_staging(monkeypatch, tmp_path):
    from maverick.paths import data_dir

    _seed(data_dir())
    archive = backup.create_backup()
    forged = tmp_path / "tampered.mvkb"
    data = bytearray(archive.read_bytes())
    data[backup._ENVELOPE_HEADER_BYTES + 7] ^= 0x80
    forged.write_bytes(data)

    def _must_not_stage(*_args, **_kwargs):
        raise AssertionError("payload staging ran before GCM authentication")

    monkeypatch.setattr(backup, "_extract_payload_stream", _must_not_stage)
    with pytest.raises(backup.BackupError, match="wrong key or tampering"):
        backup.restore_backup(forged)


def test_unsigned_unencrypted_compatibility_requires_explicit_auth_off(tmp_path):
    from maverick.paths import data_dir

    root = data_dir()
    _seed(root)
    archive = backup.create_backup()
    legacy = tmp_path / "legacy-unsigned.tgz"

    def _make_unsigned(manifest):
        manifest["schema"] = 2
        manifest["authentication"] = {"algorithm": "none"}

    _repack(
        archive,
        legacy,
        mutate_manifest=_make_unsigned,
        encrypt=False,
    )

    with pytest.raises(backup.BackupError, match="not encrypted"):
        backup.read_manifest(legacy)
    manifest = backup.read_manifest(legacy, allow_legacy_auth_off=True)
    assert manifest["authentication"] == {"algorithm": "none"}

    (root / "agent_trust.json").unlink()
    with pytest.raises(backup.BackupError, match="not encrypted"):
        backup.restore_backup(legacy)
    backup.restore_backup(legacy, allow_legacy_auth_off=True)
    assert (root / "agent_trust.json").read_text() == '[{"id":"vega"}]'


def test_legacy_schema_one_restore_is_explicitly_unsigned(tmp_path):
    from maverick.paths import data_dir

    root = data_dir()
    _seed(root)
    archive = backup.create_backup()
    legacy = tmp_path / "legacy-schema-one.tgz"

    def _to_legacy(manifest):
        manifest["schema"] = 1
        manifest["files"] = {
            name: record["sha256"] for name, record in manifest["files"].items()
        }
        manifest.pop("authentication", None)

    _repack(archive, legacy, mutate_manifest=_to_legacy, encrypt=False)
    (root / "agent_trust.json").unlink()

    with pytest.raises(backup.BackupError, match="not encrypted"):
        backup.restore_backup(legacy)
    backup.restore_backup(legacy, allow_legacy_auth_off=True)
    assert (root / "agent_trust.json").exists()


def test_manifest_tamper_is_rejected_even_with_force(tmp_path):
    from maverick.paths import data_dir

    _seed(data_dir())
    archive = backup.create_backup()
    tampered = tmp_path / "tampered-manifest.tgz"
    _repack(
        archive,
        tampered,
        mutate_manifest=lambda manifest: manifest.update(client_id="attacker"),
    )

    with pytest.raises(backup.BackupError, match="signature verification failed"):
        backup.restore_backup(tampered, force=True)


def test_wrong_operator_key_cannot_verify_archive(monkeypatch):
    from maverick.paths import data_dir

    _seed(data_dir())
    archive = backup.create_backup()
    monkeypatch.setenv(
        "MAVERICK_BACKUP_SIGNING_KEY",
        bytes(reversed(range(32))).hex(),
    )

    with pytest.raises(backup.BackupError, match="configured operator key"):
        backup.read_manifest(archive)


def test_restore_rejects_symlink_archive_member(tmp_path):
    from maverick.paths import data_dir

    _seed(data_dir())
    archive = backup.create_backup()
    malicious_inner = tmp_path / "symlink-member-inner.tgz"
    malicious = tmp_path / "symlink-member.mvkb"
    with _plaintext_archive_path(archive) as inner:
        with tarfile.open(inner, "r:gz") as source, tarfile.open(
            malicious_inner, "w:gz",
        ) as out:
            for member in source.getmembers():
                out.addfile(
                    member,
                    source.extractfile(member) if member.isfile() else None,
                )
            link = tarfile.TarInfo("data/linked-secret")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../outside"
            out.addfile(link)
    _encrypt_plain_archive(malicious_inner, malicious)

    with pytest.raises(backup.BackupError, match="unsupported archive member type"):
        backup.restore_backup(malicious)


def test_restore_rejects_duplicate_archive_member(tmp_path):
    from maverick.paths import data_dir

    _seed(data_dir())
    archive = backup.create_backup()
    malicious_inner = tmp_path / "duplicate-member-inner.tgz"
    malicious = tmp_path / "duplicate-member.mvkb"
    with _plaintext_archive_path(archive) as inner:
        with tarfile.open(inner, "r:gz") as source:
            members = source.getmembers()
            blobs = {
                member.name: source.extractfile(member).read()
                for member in members
                if member.isfile()
            }
    with tarfile.open(malicious_inner, "w:gz") as out:
        for member in members:
            out.addfile(member, io.BytesIO(blobs[member.name]) if member.isfile() else None)
        duplicate = tarfile.TarInfo("data/agent_trust.json")
        duplicate.size = len(blobs["data/agent_trust.json"])
        out.addfile(duplicate, io.BytesIO(blobs["data/agent_trust.json"]))
    _encrypt_plain_archive(malicious_inner, malicious)

    with pytest.raises(backup.BackupError, match="duplicate"):
        backup.restore_backup(malicious)


@pytest.mark.parametrize(
    "member_name",
    [
        "data/report:alternate-stream.txt",
        "data/CON.txt",
        "data/a//b.txt",
        "data/.restore-transactions/forged-journal",
        "data/keys/at_rest.key",
        "data/audit/keys/signing.key",
        "data/" + "a" * 256,
    ],
)
def test_restore_rejects_nonportable_or_reserved_member_paths(
    tmp_path, member_name,
):
    from maverick.paths import data_dir

    _seed(data_dir())
    archive = backup.create_backup()
    malicious_inner = tmp_path / f"bad-path-{abs(hash(member_name))}-inner.tgz"
    malicious = tmp_path / f"bad-path-{abs(hash(member_name))}.mvkb"
    with _plaintext_archive_path(archive) as inner:
        with tarfile.open(inner, "r:gz") as source, tarfile.open(
            malicious_inner, "w:gz",
        ) as out:
            for member in source.getmembers():
                out.addfile(
                    member,
                    source.extractfile(member) if member.isfile() else None,
                )
            extra = tarfile.TarInfo(member_name)
            extra.size = 1
            out.addfile(extra, io.BytesIO(b"x"))
    _encrypt_plain_archive(malicious_inner, malicious)

    with pytest.raises(backup.BackupError, match="unsafe|reserved|too long"):
        backup.restore_backup(malicious)


def test_restore_rejects_casefold_path_collision(tmp_path):
    from maverick.paths import data_dir

    _seed(data_dir())
    archive = backup.create_backup()
    malicious_inner = tmp_path / "case-collision-inner.tgz"
    malicious = tmp_path / "case-collision.mvkb"
    with _plaintext_archive_path(archive) as inner:
        with tarfile.open(inner, "r:gz") as source, tarfile.open(
            malicious_inner, "w:gz",
        ) as out:
            for member in source.getmembers():
                out.addfile(
                    member,
                    source.extractfile(member) if member.isfile() else None,
                )
            for name in ("data/Readme.txt", "data/README.TXT"):
                extra = tarfile.TarInfo(name)
                extra.size = 1
                out.addfile(extra, io.BytesIO(b"x"))
    _encrypt_plain_archive(malicious_inner, malicious)

    with pytest.raises(backup.BackupError, match="case-insensitive"):
        backup.restore_backup(malicious)


def test_restore_enforces_member_and_expanded_size_bounds(monkeypatch):
    from maverick.paths import data_dir

    _seed(data_dir())
    archive = backup.create_backup()

    monkeypatch.setattr(backup, "MAX_ARCHIVE_MEMBERS", 2)
    with pytest.raises(backup.BackupError, match="member-count"):
        backup.read_manifest(archive)

    monkeypatch.setattr(backup, "MAX_ARCHIVE_MEMBERS", 100_000)
    monkeypatch.setattr(backup, "MAX_ARCHIVE_FILE_BYTES", 4)
    with pytest.raises(backup.BackupError, match="per-file"):
        backup.read_manifest(archive)

    monkeypatch.setattr(backup, "MAX_ARCHIVE_FILE_BYTES", 8 * 1024**3)
    monkeypatch.setattr(backup, "MAX_ARCHIVE_EXPANDED_BYTES", 10)
    with pytest.raises(backup.BackupError, match="expanded-size"):
        backup.read_manifest(archive)

    monkeypatch.setattr(backup, "MAX_ARCHIVE_EXPANDED_BYTES", 256 * 1024**3)
    monkeypatch.setattr(backup, "MAX_ARCHIVE_COMPRESSED_BYTES", 1)
    with pytest.raises(backup.BackupError, match="bounded"):
        backup.read_manifest(archive)


def test_restore_streams_without_getmembers_or_extractall(monkeypatch):
    from maverick.paths import data_dir

    root = data_dir()
    _seed(root)
    archive = backup.create_backup()
    (root / "agent_trust.json").unlink()

    def _forbidden(*_args, **_kwargs):  # pragma: no cover - assertion is the test
        raise AssertionError("restore must use bounded streaming tar iteration")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", _forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extractall", _forbidden)
    backup.restore_backup(archive)
    assert (root / "agent_trust.json").exists()


def test_unique_backup_staging_ignores_planted_legacy_part(tmp_path):
    from maverick.paths import data_dir

    _seed(data_dir())
    output_dir = file_lock.ensure_private_directory(tmp_path / "output")
    destination = output_dir / "snapshot.tgz"
    legacy_part = destination.with_suffix(destination.suffix + ".part")
    file_lock.atomic_create_bytes(legacy_part, b"planted")

    created = backup.create_backup(destination)

    assert created == destination
    assert legacy_part.read_bytes() == b"planted"
    assert file_lock.private_path_is_restricted(created)


def test_create_refuses_planted_regular_destination(tmp_path):
    from maverick.paths import data_dir

    _seed(data_dir())
    output_dir = file_lock.ensure_private_directory(tmp_path / "output")
    destination = output_dir / "snapshot.tgz"
    file_lock.atomic_create_bytes(destination, b"planted")

    with pytest.raises(backup.BackupError, match="existing backup destination"):
        backup.create_backup(destination)
    assert destination.read_bytes() == b"planted"


def test_create_refuses_planted_final_symlink(tmp_path):
    from maverick.paths import data_dir

    _seed(data_dir())
    output_dir = file_lock.ensure_private_directory(tmp_path / "output")
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"do not overwrite")
    destination = output_dir / "snapshot.tgz"
    try:
        os.symlink(victim, destination)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(backup.BackupError, match="existing backup destination"):
        backup.create_backup(destination)
    assert victim.read_bytes() == b"do not overwrite"


def test_restore_refuses_planted_live_symlink(tmp_path):
    from maverick.paths import data_dir

    root = data_dir()
    _seed(root)
    archive = backup.create_backup()
    target = root / "agent_trust.json"
    target.unlink()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"do not overwrite")
    try:
        os.symlink(victim, target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(backup.BackupError, match="planted or aliased"):
        backup.restore_backup(archive)
    assert victim.read_bytes() == b"do not overwrite"


def test_partial_restore_failure_rolls_back_entire_file_set(monkeypatch):
    from maverick.paths import data_dir

    root = data_dir()
    _seed(root)
    archive = backup.create_backup()

    (root / "agent_trust.json").write_text('[{"id":"live-new"}]')
    (root / "audit" / "2026-06-18.ndjson").write_text('{"kind":"live-new"}\n')
    connection = sqlite3.connect(str(root / "world.db"))
    connection.execute("UPDATE t SET v = 'live-new'")
    connection.commit()
    connection.close()
    wal = root / "world.db-wal"
    wal.write_bytes(b"pre-restore-sidecar")
    before = {
        "trust": (root / "agent_trust.json").read_bytes(),
        "audit": (root / "audit" / "2026-06-18.ndjson").read_bytes(),
        "db": (root / "world.db").read_bytes(),
        "wal": wal.read_bytes(),
    }

    real_publish = backup._publish_staged_file
    failed = False

    def _fail_once(source, destination):
        nonlocal failed
        if destination.suffix == ".db" and not failed:
            failed = True
            raise OSError("injected apply failure")
        return real_publish(source, destination)

    monkeypatch.setattr(backup, "_publish_staged_file", _fail_once)
    with pytest.raises(OSError, match="injected apply failure"):
        backup.restore_backup(archive)

    assert (root / "agent_trust.json").read_bytes() == before["trust"]
    assert (root / "audit" / "2026-06-18.ndjson").read_bytes() == before["audit"]
    assert (root / "world.db").read_bytes() == before["db"]
    assert wal.read_bytes() == before["wal"]
    assert not (root / backup._RESTORE_TX_ROOT).exists()


def test_commit_journal_failure_rolls_back(monkeypatch):
    from maverick.paths import data_dir

    root = data_dir()
    _seed(root)
    archive = backup.create_backup()
    (root / "agent_trust.json").write_text('[{"id":"live-new"}]')
    before = (root / "agent_trust.json").read_bytes()
    real_write = backup._write_journal
    failed = False

    def _fail_commit(tx, body, key):
        nonlocal failed
        if body.get("state") == "committed" and not failed:
            failed = True
            raise OSError("journal commit unavailable")
        return real_write(tx, body, key)

    monkeypatch.setattr(backup, "_write_journal", _fail_commit)
    with pytest.raises(OSError, match="journal commit unavailable"):
        backup.restore_backup(archive)

    assert (root / "agent_trust.json").read_bytes() == before
    assert not (root / backup._RESTORE_TX_ROOT).exists()


def test_next_restore_recovers_retained_applying_journal(monkeypatch):
    from maverick.paths import data_dir

    root = data_dir()
    _seed(root)
    archive = backup.create_backup()
    (root / "agent_trust.json").write_text('[{"id":"live-new"}]')

    real_publish = backup._publish_staged_file
    real_rollback = backup._rollback
    failed = False

    def _fail_db_once(source, destination):
        nonlocal failed
        if destination.suffix == ".db" and not failed:
            failed = True
            raise OSError("injected apply failure")
        return real_publish(source, destination)

    def _rollback_unavailable(*_args, **_kwargs):
        raise OSError("injected rollback outage")

    monkeypatch.setattr(backup, "_publish_staged_file", _fail_db_once)
    monkeypatch.setattr(backup, "_rollback", _rollback_unavailable)
    with pytest.raises(backup.BackupError, match="rollback is incomplete"):
        backup.restore_backup(archive)
    assert (root / backup._RESTORE_TX_ROOT).exists()

    recoveries = 0

    def _observe_recovery(*args, **kwargs):
        nonlocal recoveries
        recoveries += 1
        return real_rollback(*args, **kwargs)

    monkeypatch.setattr(backup, "_publish_staged_file", real_publish)
    monkeypatch.setattr(backup, "_rollback", _observe_recovery)
    backup.restore_backup(archive)

    assert recoveries == 1
    assert (root / "agent_trust.json").read_text() == '[{"id":"vega"}]'
    assert not (root / backup._RESTORE_TX_ROOT).exists()
