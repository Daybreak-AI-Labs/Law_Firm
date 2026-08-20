# Bjerken and Day

The firm's internal practice platform: governed legal assistants that draft, research,
and keep the file straight — VA disability claims and appeals, pleadings and motions,
wills and trusts, corporate redlines, discovery, research memos — with an attorney
reviewing everything before it leaves the office. Nothing it produces is
self-approving; every legal seat routes its work product to a human.

Private software for one firm. Not a product, not for distribution.

---

## What this is

Maverick, the firm's agent runtime, cut down to a single law practice. You hand it a goal in plain English; an orchestrator decomposes it, routes
to the right specialist seat, and returns a draft with its sources. Every run is capped
in dollars and wall-clock, every action is recorded, and nothing a seat produces is
self-approving.

**Practice areas:** VA (Veterans Affairs) disability, family law, complex litigation,
privacy and cyber, estate planning and probate, business and transactional, real estate.

**Jurisdictions:** Tennessee, Georgia, and Florida to start — with one important
exception. VA disability is *federal* practice before the Department of Veterans
Affairs, so an accredited attorney represents claimants nationwide; the three-state
limit governs the state-law matters, not the veterans work. A matter therefore carries
both a practice area and a jurisdiction, and the two are not the same axis.

Multi-state is a design constraint, not a detail. A state matter's jurisdiction drives
which rules of professional conduct apply, which trust-accounting regime governs its
retainer, which court's deadlines calculate, and which e-filing system it lands in.
Jurisdiction is a required field on a matter, not an optional tag.

**What it keeps from upstream**

- The kernel — recursive orchestration, persistent world model, one explicit run-wide model pin,
  hard budget caps, sandboxed execution.
- The governance layer that matters for legal work: least-privilege tool envelopes per
  seat, a signed hash-chained audit log, human sign-off gates, and compartment isolation
  so one matter's context cannot bleed into another.
- The matter-scoped local improvement loop: reflexion, rehearsal, offline candidate
  evaluation, and explicit operator promotion. Runtime agents cannot acquire tools or
  promote code.

**What was removed**

- The inherited cross-industry specialist roster. Thirty-one law-firm profiles
  remain; finance, tax, HR, insurance, foreign-regime, and generic business
  profiles are not shipped as selectable matter workflows.
- The enterprise sales surface: demo SKUs, the benchmark harness, the published
  governance benchmark, marketing pages, and the release pipeline that shipped them.
- Agent-surveillance instrumentation. The **audit record stays** — conflicts checks,
  privilege logs and billing substantiation are malpractice-defense artifacts, not
  ceremony — but per-step agent scorecards and trust telemetry are gone. It is one
  attorney's own platform; it does not need to police itself.

## The roster

31 legal profiles, each with a least-privilege tool envelope, a risk ceiling, a
workflow playbook, and a declared deliverable that names a human consumer.

| Work | Profiles | What it covers |
|---|---:|---|
| Matter and client | 5 | Firm-wide legal drafting, conflicts, intake, and matter management |
| Research and litigation | 10 | Research, citation checks, briefs, discovery, holds, investigations, case management, settlement, and subpoenas |
| Transactional and corporate | 12 | Contract intake/drafting/review, DPA/MSA/NDA/SaaS work, entity and board work, obligations, negotiation, and vendor review |
| Privacy, cyber, and employment | 3 | Breach response, privacy analysis, and employment-law analysis |
| Knowledge management | 1 | Matter-scoped precedent and playbook upkeep |

Every legal pack declares a deliverable, names a human consumer, and ends in a
review or approval gate. Intake routing and knowledge-management packages are
also held for attorney review; neither is an ungated internal exception.

**Gap worth naming:** the compact roster does not yet have dedicated VA-disability,
family-law, estate/probate, or state-specific real-estate profiles. Those matters can
use the gated firm-wide research/drafting profile during development, but the platform
must not claim a specialized workflow until the attorney-reviewed packs and live legal
evaluations exist — see the [roadmap](#roadmap).

Prove the roster's safety properties:

```bash
maverick domains-lint     # 0 errors across all 31 profiles
```

## Setup

```bash
git clone https://github.com/Daybreak-AI-Labs/Law_Firm && cd Law_Firm

python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -e ./packages/maverick-core                 # kernel, with deps
for p in maverick-shield \
         maverick-dashboard maverick-knowledge; do
  pip install --no-deps -e "./packages/$p"
done
pip install --no-deps -e ./apps/installer-cli

maverick init          # setup wizard
maverick dashboard     # web UI at http://127.0.0.1:8765
```

`.devcontainer/post-create.sh` does the same thing in one step. On a machine where the
distribution owns `cryptography`, use the venv route above — pip cannot uninstall a
debian-packaged `cryptography` and the script will stop there.

## Commands

Day to day, everything user-facing happens in the dashboard — goals are created,
watched, and answered there. The CLI is the operational surface:

| Command | What |
|---|---|
| `maverick init` | Setup wizard with API-key validation |
| `maverick doctor` | Health check with remediation hints |
| `maverick dashboard` | Local web UI and REST API — where goals are created and run |
| `maverick worker` | Background job worker that executes queued goals |
| `maverick migrate` / `config-lint` / `domains-lint` | Setup and health checks |
| `maverick audit verify` | Verify the hash-chained audit log |
| `maverick erase` / `erase-verify` / `export-user` | The privacy record |
| `maverick halt` / `unhalt` | Emergency stop |
| `maverick dream` | Nightly learning consolidation |
| `maverick knowledge` | Matter-scoped document knowledge |

The CLI is `maverick` and settings are `MAVERICK_*`. That is the internal package
name, kept because renaming 394k lines of source buys nothing; the upstream product
name and its compatibility aliases are gone.

## Layout

```
packages/
  maverick-core/       Kernel: orchestration, world model (SQLite/Postgres),
                       providers, sandboxes, 31 legal profiles, budget caps
  maverick-shield/     Prompt/tool/output screening
  maverick-dashboard/  FastAPI web UI + REST API at /api/v1
  maverick-knowledge/  Document parsing, chunking, embedding, retrieval
apps/
  installer-cli/       Setup wizard
docs/                  Architecture, configuration, deployment, safety, API
```

## Client matters and conflicts

The dashboard calls these workspaces **matters**. A named attorney opens a
matter with either a new client or a client already visible through that
attorney's active matter memberships, plus a firm matter number, jurisdiction,
and a legal workflow that ends in human review or approval. The client, matter,
responsible-attorney membership, and client/adverse-party records commit as one
transaction.

Conflict clearance is deliberately conservative and opaque. It compares exact
normalized names across encrypted client and party records; a possible match
returns only “potential conflict” and no client, matter, party, or match count.
Alias, affiliate, and fuzzy-name research remains a conflicts-counsel workflow,
not an automated clearance claim.

## Confidentiality

Every matter defaults to `local_only`. Public model or tool egress is denied unless the
responsible attorney changes that exact matter to `approved_services` and the operator
has separately placed the exact provider and HTTPS host on the firm's allowlists.
Provider and HTTP dispatch re-check the durable matter authority before use, so a
membership revocation or egress-policy change takes effect during a live run.

This is an enforcement boundary, not a representation that any cloud service is
ethically or contractually suitable. The firm must still approve vendor terms,
retention, privilege handling, and incident response before adding a service to an
allowlist. Client data is sealed at rest when the operator provisions the encryption
key; backups use a separate operator-custodied encryption key.

## Roadmap

Remaining product work, in rough priority order:

1. **VA disability packs** — the largest gap, and federal, so one build serves all three
   states. Intake and accreditation (VA Form 21-22a), the AMA lanes (supplemental claim,
   higher-level review, Board appeal), C-file review and evidence development, nexus
   letters and DBQs, rating analysis under 38 C.F.R. Part 4, TDIU, and fee-agreement
   compliance under 38 C.F.R. § 14.636 — where the fee rules are strict enough to be
   worth encoding as a hard gate rather than a checklist. Verify current form numbers
   and rule text when authoring; VA forms change.
2. **State practice packs** — family law (custody, support, property division, marital
   settlement agreements), estate planning (wills, RLTs, POAs, advance directives),
   probate, and residential closings — each parameterized by jurisdiction rather than
   written three times.
3. **Client-intake follow-ons** — alias/affiliate research, conflicts-counsel
   resolution records, and jurisdiction-aware deadline docketing. Exact normalized
   conflict checks and required client/matter metadata are already enforced at intake.
4. **Time, billing, and trust accounting** — three IOLTA regimes, not one. Florida's
   trust-accounting rules are the strictest of the three and should set the floor the
   ledger is built to; Georgia and Tennessee then fit inside it.
5. **Court e-filing and rules-based calendaring** — three systems: Florida's statewide
   portal is the most uniform, Georgia is Tyler-based, and Tennessee varies by county.
   Sequence them in that order; the uniform one proves the design.
6. **Attorney-feedback evaluations** — expand the governed offline evaluation corpus
   from reviewed edits and executed documents without creating cross-matter recall.

## People

Two of us, with local accounts and matter-level access control: owner, associate,
and paralegal roles. The invite flow works today.

**Admission status matters for how this gets used.** Until we are admitted, the
platform is for building and testing — not for producing work product a client
relies on, which would be practicing law without a licence regardless of who or
what drafted it. The VA packs and live legal evaluations are the things worth
building in the meantime, so the platform is ready the day the licences are.

## License

Proprietary. See [LICENSE](LICENSE). This is internal software for Bjerken and Day; it
is not licensed for use, redistribution, or derivative works by anyone else.
