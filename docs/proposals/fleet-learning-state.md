# Proposal: fleet-shared learning state

Status: **COMPLETE (phases 1 + 2)** — the three learning stores (addenda,
line-meta, transfer tried-memory) AND the corpus family (live/pending/
rejected) ship behind ``[self_harness] store = "world"``
(`maverick.learning_store`; world-DB tables at v25/v26 on both backends;
`maverick self-harness migrate-store` imports a host's files; `corpus
export`/`corpus import` keep hand-editing first-class; battery guarantees
*fleet store parity* + *fleet corpus store* at 11/11).
Owner: self-harness subsystem
Prerequisite reading: `docs/proposals/self-harness.md`,
`docs/research/2026-06-28-self-harness-frontier-roadmap.md`

## Problem

Every store the self-learning harness compounds on is a host-local file under
`~/.maverick`, serialized by `flock` + an in-process lock:

| Store | File | Content | Sealed at rest? |
|---|---|---|---|
| Learned guidance | `addenda.json` | per-model prompt addenda | no (plaintext lines, sanitized) |
| Provenance/outcomes | `addenda.meta.json` | per-line evidence + counters | no |
| Transfer tried-memory | `addenda.transfer.json` | line-id hashes + timestamps | content-free |
| Eval corpus (live) | `<corpus>.json` | operator ground truth | no (operator-editable by design) |
| Harvest pending | `<corpus>.json.pending.json` | mined goal text | yes (machine-owned) |
| Reject memory | `<corpus>.json.rejected.json` | rejected goal text | yes (machine-owned) |

A single-host deployment is fully served by this. A fleet spanning hosts — the
control/data-plane worker story, or several machines running role models — has
**no shared learning state**: each host learns alone, transfer only sweeps its
own store, outcome evidence fragments, and two hosts can promote conflicting
lines. `flock` is also unreliable on NFS, so "shared filesystem" is not the
answer.

## Non-goals

- Changing single-host behavior, formats, or the governed gate in any way.
- A network service. The world model already solves "shared, migrated,
  postgres-backed state" — learning state should ride the same rails, not
  invent new ones.

## Phase split

- **Phase 1 (shipped)**: the LEARNING stores — `harness_addenda`,
  `harness_line_meta`, `harness_transfer_tried` — as world tables. The seam:
  an EXPLICIT store path always means the file store at that path (tests and
  tenant redirection unchanged); the default location resolves to the
  configured store. Postgres RMW is serialized by a session advisory lock
  (`learning_store.rmw_lock`); SQLite rides the existing same-host flock.
- **Phase 2 (shipped)**: the corpus family as `harness_corpus` rows
  (kinds `live`/`pending`/`rejected`, plus `extra` preserving a live file's
  non-list top-level entries). Routing keys off the CONFIGURED `eval_corpus`
  path at the eval module's one read funnel and one write funnel — any other
  path stays a plain file. The hand-editing story is `maverick self-harness
  corpus export --out f.json` → edit → `corpus import f.json [--replace]`,
  preserving every operator-authored field both ways.

## Design (recommended): learning state as world-model tables

Add a `learning_store` backend seam to `self_harness` mirroring the world
model's SQLite/Postgres split:

1. **Tables** (world-DB migrations, subject to `migration_governance --regen`):
   - `harness_addenda(key TEXT PRIMARY KEY, block TEXT, updated_at REAL)`
   - `harness_line_meta(line_id TEXT PRIMARY KEY, record TEXT/JSONB, updated_at REAL)`
   - `harness_transfer_tried(line_id TEXT PRIMARY KEY, ts REAL)`
   - `harness_corpus(key TEXT, seq INT, row TEXT/JSONB, kind TEXT
     CHECK(kind IN ('live','pending','rejected')))`
   Content columns join `encryption_migrate._SEALED_COLUMNS` so at-rest
   sealing parity is automatic (the live corpus stays exportable via a CLI
   that round-trips to the operator's JSON).
2. **Concurrency**: replace `flock` with the backend's native serialization —
   `BEGIN IMMEDIATE` on SQLite, `SELECT ... FOR UPDATE` on Postgres — behind
   the same `_lock`-shaped context manager the file store uses today, so the
   mutators' bodies don't change.
3. **Selection**: one knob, `[self_harness] store = "files" | "world"`
   (default `files`, byte-identical behavior). `world` requires a configured
   world DB; the wizard's advanced follow-up gains the question (kernel rules
   5–6).
4. **Migration path**: `maverick self-harness migrate-store` copies files →
   tables under both locks, verifies row counts + a recall byte-comparison,
   then leaves the files as a frozen backup. Reads are dual-tolerant during
   the transition (table first, file fallback) for one release.
5. **Invariants preserved** (each already proven by the 9-guarantee battery,
   which would run against BOTH stores): determinism, bounded addenda,
   gate-only promotion, reversible audited forget, merits-only tried-memory,
   reject durability, concurrency safety (the battery's concurrent-promotion
   drill is the acceptance test for the new lock path).

## Why not the alternatives

- **Shared filesystem (NFS)**: `flock` semantics are the exact thing NFS
  breaks; silently corrupting the learning store is worse than not sharing it.
- **Object store (S3)**: no read-modify-write serialization without a
  side-channel lock service; adds a dependency class the kernel doesn't have.
- **A learning microservice**: violates the kernel's no-new-services posture
  and re-invents the world model's existing multi-backend machinery.

## Deploy-time validation (the other honest gap)

Everything above — and the whole transfer/harvest lifecycle — is exercised
with the LLM boundary stubbed. The first production night with
`transfer_auto`/`corpus_harvest` on IS the live integration test. Checklist
for that night (goes in the runbook when this lands):

1. `eval_budget_dollars` set low (≤ $5) for the first sweep.
2. `maverick self-harness transfer --from <model>` run manually once,
   inspecting the per-target report before enabling `transfer_auto`.
3. `corpus_harvest = "propose"` (never `auto`) until the first
   `corpus review` has been exercised end-to-end.
4. Day-after checks: `corpus quality` report, `/learned` transfer-memory
   line advancing, calibration freeze NOT armed.

## Effort

Roughly one wave: migrations + governance regen, the store seam, the two lock
adapters, the migrate command, battery parametrization over both stores, and
wizard/docs parity.
