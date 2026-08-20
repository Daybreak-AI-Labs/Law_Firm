"""Migration governance — the Alembic-grade ratchet over the world-model
migration ladder.

``schema_migrations.py`` lints the ladder for online/offline safety. This
module governs the *integrity and evolution* of the SQLite ladder
(``world_model.MIGRATIONS`` + ``SCHEMA_VERSION``) — the way
Alembic governs a revision graph:

* **Immutability of released migrations.** Each version's statements are
  fingerprinted (sha256). A committed lock manifest (``migrations.lock.json``)
  pins those fingerprints. CI fails if an *already-released* version's checksum
  changes — editing a shipped migration silently diverges every DB that already
  applied the old text. Appending a NEW version is fine; it shows up as a
  reviewable lock diff (regenerate with ``--regen``).
* **Head parity.** The ladder head must equal the declared
  ``SCHEMA_VERSION`` constant.
* **Additive-only for new versions.** A version added since the lock may not
  carry a destructive statement (``DROP TABLE``/``DROP COLUMN``/``RENAME``):
  those break a rolling deploy where old replicas still read the old schema.
  An exact, code-reviewed data-retirement allowlist is the sole exception and
  remains an offline maintenance step. Released versions are grandfathered
  (governed by the checksum, not re-judged).

Pure and offline (no DB). Surfaced as ``python -m maverick.migration_governance
--ci`` (CI gate) and ``--regen`` (rewrite the lock after an intentional add).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# Destructive shapes that must not appear in a NEW migration: they break a
# rolling deploy (old replicas still expect the column/table) and are
# non-additive. Grandfathered for already-released versions via the lock.
# The base-schema seed. SQLite applies its base CREATE at db creation and
# starts its MIGRATIONS dict at v2; the base carries version 1.
_BASE_VERSION = 1

_DESTRUCTIVE = (
    # DROP of any schema object an old replica may still depend on. CONSTRAINT
    # and the rebuildable objects (INDEX/VIEW/TRIGGER/SEQUENCE) are non-additive
    # in a rolling deploy and were missed before.
    re.compile(r"\bDROP\s+(TABLE|COLUMN|CONSTRAINT|INDEX|VIEW|TRIGGER|SEQUENCE)\b",
               re.IGNORECASE),
    # Any RENAME under ALTER TABLE. Match RENAME anywhere after ALTER TABLE
    # (not `ALTER TABLE \S+ RENAME`) so a quoted/bracketed identifier containing
    # a space -- ALTER TABLE "my table" RENAME TO y -- can't slip past \S+.
    re.compile(r"\bALTER\s+TABLE\b[^;]*\bRENAME\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bRENAME\s+COLUMN\b", re.IGNORECASE),
)

# Deliberately tiny exception set for reviewed data retirement.  Matching is
# exact after whitespace normalization: an extra statement, another backend,
# another version, or another object remains blocked.  schema_migrations still
# classifies these statements as offline and requires a maintenance window.
_APPROVED_DESTRUCTIVE_RETIREMENTS = {
    ("sqlite", 39): ("DROP TABLE IF EXISTS harness_transfer_tried",),
}


def lock_path() -> Path:
    """The committed lock manifest, next to this module."""
    return Path(__file__).with_name("migrations.lock.json")


def _normalize(statement: str) -> str:
    """Whitespace-stable form of a statement so cosmetic reformatting (indent,
    trailing semicolon) does not move a checksum, but a real SQL change does."""
    return re.sub(r"\s+", " ", statement.strip().rstrip(";")).strip()


def version_checksum(statements: list[str]) -> str:
    """Stable sha256 (16 hex chars) over a version's normalized statements,
    order-significant."""
    joined = "\n".join(_normalize(s) for s in statements)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _sqlite_ladder() -> dict[int, list[str]]:
    from .world_model import MIGRATIONS, SCHEMA
    # Fingerprint the base SCHEMA as version _BASE_VERSION so the ratchet governs
    # the tables it -- and only it -- creates (share_links, signoffs, artifacts,
    # projects, halt, provider_spend, harness_*, ...). The MIGRATIONS dict starts
    # at v2, so without this an edit to a base table definition changed the schema
    # with no checksum to catch it -- those tables were entirely ungoverned.
    ladder: dict[int, list[str]] = {_BASE_VERSION: [SCHEMA]}
    ladder.update({int(v): list(stmts) for v, stmts in MIGRATIONS.items()})
    return ladder


def _declared_heads() -> dict[str, int]:
    from .world_model import SCHEMA_VERSION
    return {"sqlite": int(SCHEMA_VERSION)}


def ladders() -> dict[str, dict[int, list[str]]]:
    return {"sqlite": _sqlite_ladder()}


def fingerprint(lads: dict[str, dict[int, list[str]]] | None = None) -> dict:
    """``{"heads": {...}, "checksums": {backend: {version: cksum}}}`` — the
    canonical, JSON-stable description of both ladders."""
    lads = lads if lads is not None else ladders()
    checksums = {
        backend: {str(v): version_checksum(stmts) for v, stmts in sorted(steps.items())}
        for backend, steps in lads.items()
    }
    heads = {backend: max(steps) if steps else 0 for backend, steps in lads.items()}
    return {"heads": heads, "checksums": checksums}


def structural_problems(lads: dict[str, dict[int, list[str]]] | None = None) -> list[str]:
    """Coherence checks that don't need the lock: head parity vs the
    declared constant."""
    lads = lads if lads is not None else ladders()
    problems: list[str] = []
    declared = _declared_heads()

    for backend, steps in lads.items():
        if not steps:
            problems.append(f"{backend}: migration ladder is empty")
            continue
        head = max(steps)
        if head != declared[backend]:
            problems.append(
                f"{backend}: head migration v{head} != declared SCHEMA_VERSION "
                f"{declared[backend]} (bump the constant with the migration)")
    return problems


def _destructive_statements(statements: list[str]) -> list[str]:
    hits = []
    for stmt in statements:
        if any(p.search(stmt) for p in _DESTRUCTIVE):
            hits.append(_normalize(stmt)[:70])
    return hits


def _approved_destructive_retirement(
    backend: str, version: int, statements: list[str],
) -> bool:
    expected = _APPROVED_DESTRUCTIVE_RETIREMENTS.get((backend, version))
    if expected is None:
        return False
    return tuple(_normalize(stmt) for stmt in statements) == tuple(
        _normalize(stmt) for stmt in expected
    )


def lock_problems(
    lock: dict | None, lads: dict[str, dict[int, list[str]]] | None = None,
) -> list[str]:
    """Immutability + additive-only checks against the committed lock.

    A changed checksum on a version present in the lock = an edit to a released
    migration (hard fail). A version absent from the lock is *new*: allowed, but
    it must be additive (no destructive statement) and the operator must
    regenerate the lock so the add is reviewed.
    """
    lads = lads if lads is not None else ladders()
    fp = fingerprint(lads)
    if not lock:
        return ["no migrations.lock.json — run `--regen` to create the baseline"]
    locked = lock.get("checksums", {})
    problems: list[str] = []
    pending_regen = False

    for backend, current in fp["checksums"].items():
        prior = locked.get(backend, {})
        for version, cksum in current.items():
            if version in prior:
                if prior[version] != cksum:
                    problems.append(
                        f"{backend}: migration v{version} checksum changed "
                        f"({prior[version]} -> {cksum}) — a released migration was "
                        "edited; released migrations are immutable, add a new "
                        "version instead")
            else:
                pending_regen = True
                destructive = _destructive_statements(lads[backend][int(version)])
                if destructive and not _approved_destructive_retirement(
                    backend, int(version), lads[backend][int(version)],
                ):
                    problems.append(
                        f"{backend}: new migration v{version} has destructive "
                        f"statement(s) {destructive}; new migrations must be "
                        "additive (old replicas still read the old schema)")
        for version in prior:
            if version not in current:
                problems.append(
                    f"{backend}: migration v{version} is in the lock but gone "
                    "from the ladder — released migrations may not be removed")
    if pending_regen and not problems:
        problems.append(
            "new migration version(s) detected — run `--regen` and commit "
            "migrations.lock.json so the addition is reviewed")
    return problems


def validate() -> list[str]:
    lads = ladders()
    return structural_problems(lads) + lock_problems(load_lock(), lads)


def load_lock() -> dict | None:
    try:
        return json.loads(lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def regen_blockers(
    lads: dict[str, dict[int, list[str]]] | None = None,
    existing_lock: dict | None = None,
) -> list[str]:
    """New versions (absent from ``existing_lock``) that carry a destructive
    statement. ``--regen`` refuses these: the additive-only gate in
    :func:`lock_problems` only inspects not-yet-locked versions, so writing a
    destructive new migration into the lock unchecked would grandfather it into
    immutability and it would never be re-judged."""
    lads = lads if lads is not None else ladders()
    if existing_lock is None:
        existing_lock = load_lock()
    locked = (existing_lock or {}).get("checksums", {})
    fp = fingerprint(lads)
    blockers: list[str] = []
    for backend, current in fp["checksums"].items():
        prior = locked.get(backend, {})
        for version in current:
            if version not in prior:
                dead = _destructive_statements(lads[backend][int(version)])
                if dead and not _approved_destructive_retirement(
                    backend, int(version), lads[backend][int(version)],
                ):
                    blockers.append(
                        f"{backend}: new migration v{version} has destructive "
                        f"statement(s) {dead}")
    return blockers


def write_lock() -> Path:
    lads = ladders()
    # Refuse to launder a destructive NEW migration into the grandfathered set.
    blockers = regen_blockers(lads)
    if blockers:
        raise ValueError(
            "refusing to regenerate migrations.lock.json -- a new migration is "
            "destructive and would be grandfathered into immutability. Make it "
            "additive (old replicas still read the old schema):\n  - "
            + "\n  - ".join(blockers))
    path = lock_path()
    payload = fingerprint(lads)
    payload["_comment"] = (
        "Committed fingerprint of the world-model migration ladders. Generated "
        "by `python -m maverick.migration_governance --regen`. Do not hand-edit; "
        "a changed checksum on a released version fails CI."
    )
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def render() -> str:
    fp = fingerprint()
    lines = [f"migration ladders: heads {fp['heads']}"]
    for backend, cks in fp["checksums"].items():
        lines.append(f"  {backend}: {len(cks)} versions, head checksum "
                     f"{cks[max(cks, key=int)] if cks else '-'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- CLI shell
    import argparse
    p = argparse.ArgumentParser(
        prog="maverick.migration_governance",
        description="Govern the world-model migration ladders (checksums, "
                    "head parity, additive-only).")
    p.add_argument("--ci", action="store_true",
                   help="exit 1 on any governance violation")
    p.add_argument("--regen", action="store_true",
                   help="rewrite migrations.lock.json from the current ladders")
    args = p.parse_args(argv)

    if args.regen:
        try:
            path = write_lock()
        except ValueError as e:
            print(f"migration governance: {e}")
            return 1
        print(f"wrote {path}")
        return 0

    problems = validate()
    if problems:
        print("migration governance: PROBLEMS")
        for prob in problems:
            print(f"  - {prob}")
    else:
        print("migration governance: OK")
        print(render())
    if args.ci and problems:
        return 1
    return 0


__all__ = [
    "lock_path", "version_checksum", "ladders", "fingerprint",
    "structural_problems", "lock_problems", "validate", "load_lock",
    "write_lock", "render", "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
