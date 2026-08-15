#!/usr/bin/env python3
"""Independent auditor for the signed promotion ledger -- "don't trust us, verify us".

The governed promotion ladder (:mod:`maverick.self_improvement`) records every
accepted self-change to a signed ledger. This CLI lets a THIRD PARTY re-verify a
run without trusting the party that produced it. It needs only the committed
artifacts (the ledger JSON, the approver public keys, and optionally the
forensics sidecars + the instance manifest) and the ``maverick`` library -- no
network, no LLM, no secret material.

Two independent checks per promotion record:

  A. SIGNATURE -- reconstruct the exact :class:`maverick.approval_signing.ApprovalRequest`
     the approver signed, from the ledger's own (id, rung, payload_sha256), and
     verify the stored Ed25519 ``approval_signature`` against the trusted public
     keys in ``--keys``. Reports VALID / INVALID / UNSIGNED. A record written
     before signatures were persisted (or a promotion with no cryptographic
     approval) is UNSIGNED -- honest, not a failure.

  B. RE-GRADE (optional; needs ``--forensics`` + ``--manifest``) -- for a record
     that is NOT rolled back, load the instance's forensics sidecar, take the
     captured patch, materialize a FRESH copy of the instance repo from the
     manifest, apply the grader ``test_patch`` and then the recorded patch, and
     run the instance's own FAIL_TO_PASS + PASS_TO_PASS tests. Reports RESOLVED
     / NOT-RESOLVED / NO-FORENSICS. This independently confirms the "resolved
     under governance" claim: the fix, replayed on a clean tree, still passes.

Exit is NONZERO if ANY signature is INVALID, ANY re-graded instance is
NOT-RESOLVED, or ANY record is MALFORMED -- a real audit fails loudly. An
UNSIGNED record or a skipped (NO-FORENSICS / rolled-back) re-grade is NOT a
failure.

    # signatures only:
    python benchmarks/audit_ledger.py --ledger ledger.json --keys ./keys
    # signatures + independent re-grade:
    python benchmarks/audit_ledger.py --ledger ledger.json --keys ./keys \
        --forensics ./forensics --manifest instances.jsonl --venvs ./venvs
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "packages" / "maverick-core"))

log = logging.getLogger(__name__)

# Signature verdicts.
VALID, INVALID, UNSIGNED, MALFORMED = "VALID", "INVALID", "UNSIGNED", "MALFORMED"
# Re-grade verdicts.
RESOLVED, NOT_RESOLVED, NO_FORENSICS, SKIPPED = (
    "RESOLVED", "NOT-RESOLVED", "NO-FORENSICS", "SKIPPED")


def load_public_keys(keys_dir: str | Path | None) -> list[str]:
    """Trusted approver public keys (hex) from ``*.pub`` files in ``keys_dir``.

    Reuses the same loader the controller's gate uses
    (:func:`maverick.approval_signing._keys_from_dir`) so the auditor reads keys
    exactly as the deployment does (raw 32-byte Ed25519 pub files -> hex). An
    empty / missing dir yields no keys, which makes every signed record report
    INVALID -- correct: an auditor with no trusted keys cannot vouch for a
    signature."""
    from maverick import approval_signing as asig
    return asig._keys_from_dir(str(keys_dir)) if keys_dir else []


@dataclass
class RecordAudit:
    """The audit outcome for one ledger record."""

    record_id: str
    rung: str = ""
    rolled_back: bool = False
    signature: str = UNSIGNED
    approver_id: str | None = None
    regrade: str = SKIPPED
    detail: str = ""

    @property
    def sig_failed(self) -> bool:
        return self.signature in (INVALID, MALFORMED)

    @property
    def regrade_failed(self) -> bool:
        return self.regrade == NOT_RESOLVED


def _check_signature(rec: dict, public_keys: list[str]) -> tuple[str, str | None]:
    """Return (verdict, approver_id) for a record's stored signature.

    Rebuilds the approver's signed message from the ledger's OWN (id, rung,
    payload_sha256) -- if any of those were tampered with, the signature stops
    verifying. UNSIGNED when no signature/digest was persisted (a pre-signature
    ledger, or a promotion with no cryptographic approval)."""
    from maverick import approval_signing as asig

    sig = rec.get("approval_signature")
    digest = rec.get("payload_sha256")
    if not sig or not digest:
        return UNSIGNED, None
    request = asig.ApprovalRequest(
        candidate_id=str(rec.get("id", "")),
        rung=str(rec.get("rung", "")),
        payload_sha256=str(digest),
    )
    approver = asig.verify(request, str(sig), public_keys)
    if approver:
        return VALID, approver
    return INVALID, None


def _forensics_patch(forensics_dir: Path, record_id: str) -> str | None:
    """The captured patch from ``<forensics>/<id>.json`` (prefer a non-empty
    ``tree_diff`` over ``prose_diff_desanitized``). None => no sidecar."""
    f = Path(forensics_dir) / f"{record_id}.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tree = (data.get("tree_diff") or "").strip()
    prose = (data.get("prose_diff_desanitized") or "").strip()
    return tree or prose or ""


def _regrade(inst, patch: str, *, timeout: float) -> tuple[str, str]:
    """Materialize a fresh copy, apply test_patch + the recorded patch, run the
    instance's tests. Returns (verdict, detail). Never raises."""
    import tempfile

    from maverick.self_modify_eval import _default_materialize, git_apply, resolve_eval_sandbox
    from swebench_governed import _score, _venv_python

    if not (patch or "").strip():
        return NOT_RESOLVED, "forensics sidecar captured no patch"

    # Era-correct interpreter (per-instance venv), mirroring swebench_governed.
    vpy = _venv_python(inst)
    prev_tp = os.environ.get("MAVERICK_TEST_PYTHON")
    if vpy:
        os.environ["MAVERICK_TEST_PYTHON"] = vpy
    try:
        with tempfile.TemporaryDirectory(prefix="audit-regrade-") as td:
            wd = Path(td) / "work"
            try:
                _default_materialize(inst.repo_path, wd)
            except Exception as e:  # noqa: BLE001 -- report, never crash the audit
                return NOT_RESOLVED, f"could not isolate workspace: {e}"
            # Grader's test fixture first (adds/updates the FAIL_TO_PASS tests),
            # then the recorded source fix -- the two touch disjoint files.
            if (inst.test_patch or "").strip():
                tp = git_apply(inst.test_patch, wd)
                if not getattr(tp, "ok", False):
                    return NOT_RESOLVED, (
                        f"grader test_patch did not apply: {getattr(tp, 'reason', 'unknown')}")
            applied = git_apply(patch, wd)
            if not getattr(applied, "ok", False):
                return NOT_RESOLVED, (
                    f"recorded patch did not apply: {getattr(applied, 'reason', 'unknown')}")
            try:
                sb = resolve_eval_sandbox(None, wd)
            except Exception as e:  # noqa: BLE001
                return NOT_RESOLVED, f"sandbox refused (isolation policy): {e}"
            score, all_pass, _, _, _ = _score(wd, inst, sb, timeout=timeout)
            if all_pass:
                return RESOLVED, f"re-graded clean: score {score:.3f}, all target tests pass"
            return NOT_RESOLVED, f"re-graded score {score:.3f}; some FAIL/PASS_TO_PASS failing"
    finally:
        if vpy:
            if prev_tp is None:
                os.environ.pop("MAVERICK_TEST_PYTHON", None)
            else:
                os.environ["MAVERICK_TEST_PYTHON"] = prev_tp


@dataclass
class AuditReport:
    rows: list[RecordAudit] = field(default_factory=list)

    @property
    def valid(self) -> int:
        return sum(1 for r in self.rows if r.signature == VALID)

    @property
    def invalid(self) -> int:
        return sum(1 for r in self.rows if r.signature == INVALID)

    @property
    def unsigned(self) -> int:
        return sum(1 for r in self.rows if r.signature == UNSIGNED)

    @property
    def malformed(self) -> int:
        return sum(1 for r in self.rows if r.signature == MALFORMED)

    @property
    def resolved(self) -> int:
        return sum(1 for r in self.rows if r.regrade == RESOLVED)

    @property
    def not_resolved(self) -> int:
        return sum(1 for r in self.rows if r.regrade == NOT_RESOLVED)

    @property
    def regrade_skipped(self) -> int:
        return sum(1 for r in self.rows if r.regrade in (SKIPPED, NO_FORENSICS))

    @property
    def failed(self) -> bool:
        """A real audit fails loudly on any invalid/malformed sig or failed re-grade."""
        return any(r.sig_failed or r.regrade_failed for r in self.rows)


def audit_ledger(
    ledger_path: Path,
    *,
    keys_dir: Path | None,
    forensics: Path | None = None,
    manifest: Path | None = None,
    venvs: Path | None = None,
    timeout: float = 600.0,
) -> AuditReport:
    """Audit every record in ``ledger_path``. Never raises on a bad record."""
    public_keys = load_public_keys(keys_dir)
    if venvs:
        os.environ["MAVERICK_SWEBENCH_VENVS"] = str(venvs)

    # Instances (for re-grade) keyed by id, only when a manifest is supplied.
    instances: dict = {}
    if forensics and manifest:
        try:
            from swebench_governed import _load_manifest
            instances = {i.instance_id: i for i in _load_manifest(Path(manifest))}
        except Exception as e:  # noqa: BLE001 -- a bad manifest disables re-grade, not the audit
            log.warning("could not load manifest %s: %s", manifest, e)

    try:
        raw = json.loads(Path(ledger_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read ledger {ledger_path}: {e}") from e
    if not isinstance(raw, list):
        raise SystemExit(f"ledger {ledger_path} is not a JSON list of records")

    report = AuditReport()
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("id"):
            report.rows.append(RecordAudit(
                record_id=str(entry)[:40], signature=MALFORMED,
                detail="record is not an object with an id"))
            continue
        rid = str(entry["id"])
        row = RecordAudit(record_id=rid, rung=str(entry.get("rung", "")),
                          rolled_back=bool(entry.get("rolled_back", False)))
        # A. Signature.
        try:
            row.signature, row.approver_id = _check_signature(entry, public_keys)
        except Exception as e:  # noqa: BLE001 -- a malformed record is a discrepancy, not a crash
            row.signature = MALFORMED
            row.detail = f"signature check errored: {e}"
            report.rows.append(row)
            continue
        # B. Re-grade (optional).
        if forensics and manifest and not row.rolled_back:
            inst = instances.get(rid)
            if inst is None:
                row.regrade = NO_FORENSICS
                row.detail = "no manifest row for this record id"
            else:
                patch = _forensics_patch(Path(forensics), rid)
                if patch is None:
                    row.regrade = NO_FORENSICS
                    row.detail = f"no forensics sidecar at {Path(forensics) / (rid + '.json')}"
                else:
                    try:
                        row.regrade, detail = _regrade(inst, patch, timeout=timeout)
                        row.detail = detail
                    except Exception as e:  # noqa: BLE001 -- report as not-resolved, never crash
                        row.regrade = NOT_RESOLVED
                        row.detail = f"re-grade errored: {e}"
        report.rows.append(row)
    return report


def _print_report(report: AuditReport, *, regrading: bool) -> None:
    print("=" * 78)
    print("  LEDGER AUDIT   (independent re-verification -- don't trust, verify)")
    print("=" * 78)
    for r in report.rows:
        bits = [f"sig={r.signature}"]
        if r.approver_id:
            bits.append(f"approver={r.approver_id}")
        if r.rolled_back:
            bits.append("rolled_back")
        if regrading:
            bits.append(f"regrade={r.regrade}")
        line = f"  [{r.signature:9}] {r.record_id:40} {' '.join(bits)}"
        print(line)
        if r.detail:
            print(f"             {r.detail}")
    print("-" * 78)
    n = len(report.rows)
    summary = (f"  {n} records | signatures: {report.valid} valid, "
               f"{report.invalid} invalid, {report.unsigned} unsigned")
    if report.malformed:
        summary += f", {report.malformed} malformed"
    summary += (f" | re-graded: {report.resolved} resolved, "
                f"{report.not_resolved} not-resolved, {report.regrade_skipped} skipped")
    print(summary)
    print("=" * 78)
    if report.failed:
        print("  AUDIT FAILED: invalid signature(s) and/or a fix that no longer resolves.")
    else:
        print("  AUDIT OK: every stored signature verified; every re-graded fix still resolves.")
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Independently audit a signed promotion ledger.")
    ap.add_argument("--ledger", required=True, type=Path)
    ap.add_argument("--keys", required=True, type=Path,
                    help="dir of trusted approver *.pub files (raw 32-byte Ed25519)")
    ap.add_argument("--forensics", type=Path, default=None,
                    help="dir of per-instance forensics sidecars; enables re-grade")
    ap.add_argument("--manifest", type=Path, default=None,
                    help="jsonl manifest (repo_path + test_patch) needed to re-grade")
    ap.add_argument("--venvs", type=Path, default=None,
                    help="MAVERICK_SWEBENCH_VENVS dir for era-correct re-grading")
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="per-instance re-grade test timeout (seconds)")
    args = ap.parse_args(argv)

    report = audit_ledger(
        args.ledger, keys_dir=args.keys, forensics=args.forensics,
        manifest=args.manifest, venvs=args.venvs, timeout=args.timeout)
    _print_report(report, regrading=bool(args.forensics and args.manifest))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
