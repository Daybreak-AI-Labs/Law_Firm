#!/usr/bin/env python3
"""Governance-tax mini-report for a governed SWE-bench slice.

The governed run reports two very different things at once: how many patches the
proposer could PRODUCE (raw capability) and how many the governance chain let
THROUGH (governed resolves). The gap -- patches that fixed the tests but were
refused by a gate, plus anti-cheat refusals -- is the "governance tax": the
capability the platform deliberately declines to bank because it wasn't earned
cleanly. This script surfaces that gap from a slice log (and, optionally, its
ledger) so the number can be reported honestly.

    python benchmarks/governance_tax.py slice4.log [slice4_ledger.json]

Parses conservatively from the log's per-instance tag lines
(``[PASS] ...`` / ``[CHEAT] ...`` / ``gate refused: ...``); malformed or empty
input yields zeroes, never a traceback.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_PASS = re.compile(r"\[PASS\]")
_CHEAT = re.compile(r"\[CHEAT\]")
_GATE_REFUSED = re.compile(r"gate refused:")


def parse_log(text: str) -> dict[str, int]:
    """Count resolves, anti-cheat (CHEAT) refusals, and gate refusals."""
    resolved = cheat = gate_refused = 0
    for line in (text or "").splitlines():
        if _PASS.search(line):
            resolved += 1
        if _CHEAT.search(line):
            cheat += 1
        if _GATE_REFUSED.search(line):
            gate_refused += 1
    return {"resolved": resolved, "cheat": cheat, "gate_refused": gate_refused}


def count_ledger(path: str | None) -> int | None:
    """Number of promotions recorded in the ledger; None if unreadable.

    The PromotionLedger serializes a JSON list of record dicts; tolerate a few
    shapes and never raise."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for key in ("records", "entries", "promotions"):
                if isinstance(data.get(key), list):
                    return len(data[key])
            return len(data)
    except Exception:
        return None
    return None


def report(counts: dict[str, int], ledger_count: int | None) -> str:
    resolved = counts["resolved"]
    cheat = counts["cheat"]
    gate_refused = counts["gate_refused"]
    # Gate-only refusals = pure-governance refusals: patches turned away by a
    # gate (the promotion gate said no, or the anti-cheat boundary caught a
    # test/config edit). These are what a naked "% resolved" number would count
    # but a governed number does not.
    gate_only = gate_refused + cheat
    raw_capability = resolved + gate_only
    lines = [
        "=" * 68,
        "  GOVERNANCE-TAX REPORT",
        "=" * 68,
        f"  governed resolves (banked):        {resolved}",
        f"  gate refusals (passed tests):      {gate_refused}",
        f"  anti-cheat (CHEAT) refusals:       {cheat}",
        f"  gate-only refusals (total):        {gate_only}",
        "-" * 68,
        f"  raw-capability resolves:           {raw_capability}"
        "   (governed + gate-only refusals)",
        f"  governed resolves:                 {resolved}",
        f"  governance tax:                    {gate_only} patches refused by "
        "gates",
    ]
    if raw_capability > 0:
        pct = 100.0 * gate_only / raw_capability
        lines.append(f"  tax rate:                          {pct:.1f}% of "
                     "raw-capable patches declined")
    if ledger_count is not None:
        lines.append("-" * 68)
        lines.append(f"  ledger promotions (cross-check):   {ledger_count}")
        if ledger_count != resolved:
            lines.append("  NOTE: ledger count != log [PASS] count; the log and "
                         "ledger may be from different runs.")
    lines.append("=" * 68)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: governance_tax.py <slice.log> [ledger.json]", file=sys.stderr)
        return 2
    log_path = Path(args[0])
    ledger_path = args[1] if len(args) > 1 else None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"could not read log {log_path}: {e}", file=sys.stderr)
        return 2
    counts = parse_log(text)
    ledger_count = count_ledger(ledger_path)
    print(report(counts, ledger_count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
