"""Trust-plane CLI groups: capability, client, backup, attest.

Split out of cli/__init__.py. Registered by importing this module at the end
of the package __init__ so the @main.group decorators fire on package import.
"""
from __future__ import annotations

import click

from . import main


@main.group("capability")
def capability_group() -> None:
    """Revoke / restore capability grants (kill a grant before its TTL)."""


@capability_group.command("revoke")
@click.argument("principal")
@click.option("--reason", default="", help="Audit reason for the revocation.")
def capability_revoke_cmd(principal: str, reason: str) -> None:
    """Revoke PRINCIPAL now. Its next tool call is denied even mid-run.

    Propagates to running agents (the registry is re-read on change) when
    capability enforcement is on ([capabilities] enforce = true).
    """
    from ..revocation import shared
    rev = shared().revoke(principal, reason=reason)
    click.echo(click.style(f"revoked {principal!r}", fg="yellow")
               + (f" — {rev.reason}" if rev.reason else ""))


@capability_group.command("unrevoke")
@click.argument("principal")
def capability_unrevoke_cmd(principal: str) -> None:
    """Restore PRINCIPAL (remove it from the revocation list)."""
    from ..revocation import shared
    if shared().unrevoke(principal):
        click.echo(click.style(f"restored {principal!r}", fg="green"))
    else:
        click.echo(f"{principal!r} was not revoked")


@capability_group.command("revocations")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def capability_revocations_cmd(as_json: bool) -> None:
    """List revoked principals."""
    import json as _json

    from ..revocation import shared
    revs = shared().revoked()
    if as_json:
        click.echo(_json.dumps(
            {p: {"revoked_at": r.revoked_at, "reason": r.reason}
             for p, r in revs.items()}, default=str))
        return
    if not revs:
        click.echo("no revoked principals")
        return
    for p, r in sorted(revs.items()):
        click.echo(f"  {p}  (at {r.revoked_at:.0f})"
                   + (f"  — {r.reason}" if r.reason else ""))


@main.group("client")
def client_group() -> None:
    """Inspect / export / erase THIS deployment's bound client (one per Maverick).

    Since one deployment serves exactly one client, the client's whole data set
    lives under one root — so export and right-to-erasure are provably complete."""


@client_group.command("status")
def client_status_cmd() -> None:
    """Show the client binding + data root."""
    from ..client import status as client_status
    st = client_status()
    if st["bound"]:
        click.echo(click.style(f"bound to {st['client_id']!r}", fg="green")
                   + f"  (enforced={st['enforced']})")
        click.echo(f"data root: {st['data_root']}")
    else:
        msg = "NOT bound to a client (shared root)"
        click.echo(click.style(msg, fg="red" if st["enforced"] else "yellow"))


@client_group.command("export")
@click.option("--out", default=None, help="Destination .tgz (default: under data root).")
def client_export_cmd(out) -> None:
    """Export all of this client's data (data portability / DSAR)."""
    from ..backup import BackupError, create_backup
    try:
        path = create_backup(out)
    except BackupError as e:
        raise click.ClickException(str(e)) from e
    click.echo(click.style(f"client export written: {path}", fg="green"))


@client_group.command("erase")
@click.option("--confirm", is_flag=True, help="Required: actually erase the data.")
@click.option("--keep-audit", is_flag=True,
              help="Preserve the signed audit chain (legal retention).")
def client_erase_cmd(confirm, keep_audit) -> None:
    """Erase ALL of this client's data (offboarding / right-to-erasure).

    Wipes the client's data root (world DB, memory, fleet, trust registry, and
    — unless --keep-audit — the audit chain). Refuses unless a client is bound
    so it can never target the shared root. Irreversible: export first."""
    from ..client import ClientBindingError, client_id, erase_client
    cid = client_id()
    if not cid:
        raise click.ClickException("no client bound — refusing to erase the shared root")
    if not confirm:
        raise click.ClickException(
            f"this ERASES all data for client {cid!r} and is irreversible. "
            "Re-run with --confirm (and consider `maverick client export` first).")
    try:
        res = erase_client(keep_audit=keep_audit)
    except ClientBindingError as e:
        raise click.ClickException(str(e)) from e
    click.echo(click.style(
        f"erased client {res['client_id']!r}: {res['removed']} path(s) removed"
        + (" (audit kept)" if res["kept_audit"] else ""), fg="yellow"))


@main.group("backup")
def backup_group() -> None:
    """Back up / restore this client's data for DR + standby failover.

    Snapshots the client-scoped data root (world DB via the SQLite online backup
    API, signed audit chain + keys, memory, fleet, managed trust registry) into
    a portable ``.tgz`` with an authenticated manifest. Set the same 32-byte
    ``MAVERICK_BACKUP_SIGNING_KEY`` on the active and standby nodes. Restore is
    transactional and fail-closed on authenticity, bounds, and client id."""


@backup_group.command("create")
@click.option("--out", default=None, help="Destination .tgz (default: under data root).")
def backup_create_cmd(out) -> None:
    """Create a consistent backup of this client's data."""
    from ..backup import BackupError, create_backup
    try:
        path = create_backup(out)
    except BackupError as e:
        raise click.ClickException(str(e)) from e
    click.echo(click.style(f"backup written: {path}", fg="green"))


@backup_group.command("restore")
@click.argument("tarball")
@click.option("--force", is_flag=True,
              help="Restore even if the backup's client id differs (dangerous).")
def backup_restore_cmd(tarball, force) -> None:
    """Restore TARBALL into this client's data root (fail-closed on client id)."""
    from ..backup import BackupError, restore_backup
    try:
        root = restore_backup(tarball, force=force)
    except BackupError as e:
        raise click.ClickException(str(e)) from e
    click.echo(click.style(f"restored into {root}", fg="green"))


@backup_group.command("info")
@click.argument("tarball")
def backup_info_cmd(tarball) -> None:
    """Show a backup's manifest (client id, time, file count, schema version)."""
    from ..backup import BackupError, read_manifest
    try:
        m = read_manifest(tarball)
    except BackupError as e:
        raise click.ClickException(str(e)) from e
    import datetime
    when = datetime.datetime.fromtimestamp(
        m.get("created_at", 0), datetime.timezone.utc).isoformat()
    click.echo(f"client_id:            {m.get('client_id')}")
    click.echo(f"created_at:           {when}")
    click.echo(f"world_schema_version: {m.get('world_schema_version')}")
    click.echo(f"files:                {len(m.get('files') or {})}")


@main.group("attest")
def attest_group() -> None:
    """Portable attestation: prove past behaviour to somebody who trusts nobody.

    A bundle binds three claims -- actions stayed inside the declared policy
    envelope, self-improvement never widened its own authority, the decision
    history is intact -- to evidence a third party can re-derive. Verification
    requires the publisher's key obtained OUT OF BAND (``attest key``); a key
    read out of the bundle proves only that somebody signed it.
    """


@attest_group.command("key")
def attest_key_cmd() -> None:
    """Print this instance's public key -- publish it out of band."""
    from ..attestation import publisher_key
    key_id, pub = publisher_key()
    click.echo(f"key_id: {key_id}")
    click.echo(f"pubkey: {pub}")
    click.echo("")
    click.echo("Give a verifier this key through a channel that does NOT run "
               "through the bundle (key page, contract, existing engagement).")


@attest_group.command("export")
@click.argument("out", type=click.Path())
@click.option("--since", type=float, default=None,
              help="Unix timestamp bounding the promotion window.")
@click.option("--audit-dir", type=click.Path(), default=None,
              help="Audit directory to attest (default: this instance's).")
def attest_export_cmd(out: str, since: float | None, audit_dir: str | None) -> None:
    """Build and sign an attestation bundle at OUT."""
    from ..attestation import export
    try:
        path = export(out, audit_dir=audit_dir, since=since)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"attestation -> {path}")
    import json as _json
    bundle = _json.loads(path.read_text(encoding="utf-8"))
    for warning in bundle.get("warnings") or []:
        click.echo(click.style(f"  ! {warning}", fg="yellow"))


@attest_group.command("verify")
@click.argument("bundle", type=click.Path(exists=True))
@click.option("--key", "key_hex", default="", metavar="HEX",
              help="Publisher's Ed25519 public key, obtained OUT OF BAND.")
@click.option("--evidence", type=click.Path(), default=None,
              help="Audit directory, to check commitments against the files.")
def attest_verify_cmd(bundle: str, key_hex: str, evidence: str | None) -> None:
    """Verify BUNDLE against a trusted key (fails closed without one)."""
    from ..attestation import verify
    from ..attestation_verify import format_report
    result = verify(bundle, trusted_key_hex=key_hex, evidence_root=evidence)
    click.echo(format_report(result))
    if not result.ok:
        raise click.ClickException("attestation verification failed")


@attest_group.command("export-verifier")
@click.argument("out", type=click.Path())
def attest_export_verifier_cmd(out: str) -> None:
    """Write the standalone verifier to OUT (runs with no maverick install)."""
    import pathlib

    from ..attestation import verifier_source
    text, digest = verifier_source()
    path = pathlib.Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    click.echo(f"verifier -> {path}")
    click.echo(f"sha256:     {digest}")
    click.echo("")
    click.echo(f"  python {path.name} <bundle.json> --key <pubkey-hex> "
               "[--evidence <audit-dir>]")
    click.echo("Needs only Python and 'cryptography'.")
