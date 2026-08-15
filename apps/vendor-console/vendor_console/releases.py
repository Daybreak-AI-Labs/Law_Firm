"""Release publishing — sign a release manifest and resolve what to serve.

Signs with the **same** publisher Ed25519 key as licenses
(:func:`licensing.signing_key_hex`) via
:func:`maverick.release_update.build_manifest`, so a manifest this console
publishes verifies byte-for-byte with the code a customer runs
(``release_update.verify_manifest`` / ``verify_bundle`` / ``plan_upgrade``).
Artifacts are described by metadata (name + sha256 + size); the binaries
themselves are distributed out-of-band (the manifest is the signed source of
truth, which is exactly what an air-gapped site verifies offline).
"""
from __future__ import annotations

import sqlite3

from maverick import release_update

from . import audit, licensing, store
from .models import Release


def publish_release(conn: sqlite3.Connection, *, version: str, channel: str,
                    min_from: str, notes: str, migrations: list[str],
                    artifacts: list[dict], actor: str = "system",
                    signing_key: str | None = None) -> Release:
    """Sign + persist a release manifest and audit it. ``artifacts`` are
    ``{name, sha256, size}`` dicts."""
    if channel not in ("stable", "edge"):
        raise ValueError(f"unknown channel: {channel!r}")
    if not str(version).strip():
        raise ValueError("version is required")
    manifest = release_update.build_manifest(
        version, artifacts, min_from=min_from, migrations=migrations,
        private_key_hex=signing_key or licensing.signing_key_hex(), notes=notes)
    rid = store.add_release(
        conn, version=version, channel=channel, min_from=min_from, notes=notes,
        migrations=migrations, artifacts=artifacts, manifest=manifest,
        key_id=manifest["key_id"], published_by=actor)
    audit.record(conn, actor=actor, action="release.publish",
                 target=f"{version}@{channel}",
                 detail={"min_from": min_from, "artifacts": len(artifacts),
                         "migrations": migrations})
    rel = store.get_release(conn, rid)
    assert rel is not None
    return rel


def yank_release(conn: sqlite3.Connection, release_id: int, *, actor: str = "system",
                 reason: str = "") -> bool:
    """Pull a release so it stops being served (e.g. a bad build)."""
    rel = store.get_release(conn, release_id)
    if rel is None:
        return False
    ok = store.yank_release(conn, release_id)
    if ok:
        audit.record(conn, actor=actor, action="release.yank",
                     target=f"{rel.version}@{rel.channel}", detail={"reason": reason})
    return ok


def manifest_to_serve(conn: sqlite3.Connection, channel: str) -> dict | None:
    """The signed manifest a deployment on ``channel`` should currently see: the
    newest non-yanked release. The deployment runs ``plan_upgrade(current,
    manifest)`` itself to decide (no-op / downgrade / version-gap / apply), so
    this just answers "what's the latest for my channel?"."""
    rel = store.newest_release(conn, channel)
    return rel.manifest if rel else None
