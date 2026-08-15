# Bjerken and Day

The firm's internal practice platform: a governed AI workforce that drafts, researches,
and keeps the file straight — VA disability claims and appeals, pleadings and motions,
wills and trusts, corporate redlines, discovery, research memos — with an attorney
reviewing everything before it leaves the office. Nothing it produces is
self-approving; every legal seat routes its work product to a human.

Private software for one firm. Not a product, not for distribution.

---

## What this is

A hard fork of the Lightwork agent platform (forked at `f47c70c`), cut down to a single
law practice. You hand it a goal in plain English; an orchestrator decomposes it, routes
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

- The kernel — recursive orchestration, persistent world model, per-role model routing,
  hard budget caps, sandboxed execution.
- The governance layer that matters for legal work: least-privilege tool envelopes per
  seat, a signed hash-chained audit log, human sign-off gates, and compartment isolation
  so one matter's context cannot bleed into another.
- The self-improvement machinery, which is being retargeted at the work that actually
  matters here — see [Roadmap](#roadmap).

**What was removed**

- 1,895 specialist packs for industries the firm does not practice in (aerospace,
  mining, semiconductors, healthcare, banking, …). 125 remain.
- The enterprise sales surface: demo SKUs, the benchmark harness, the published
  governance benchmark, marketing pages, and the release pipeline that shipped them.
- Agent-surveillance instrumentation. The **audit record stays** — conflicts checks,
  privilege logs and billing substantiation are malpractice-defense artifacts, not
  ceremony — but per-step agent scorecards and trust telemetry are gone. It is one
  attorney's own platform; it does not need to police itself.

## The roster

125 specialist packs, each with a least-privilege tool envelope, a risk ceiling, a
workflow playbook, and a declared deliverable that names a human consumer.

| Suite | Packs | What it covers |
|---|---:|---|
| Legal | 77 | Briefs, citation, conflicts, matter intake and management, contract drafting and review, NDA desk, e-discovery, litigation holds, subpoenas, settlement, IP docket, patent, trademark, privacy (GDPR/CCPA/state), entity management, board minutes, investigations |
| Tax | 12 | Advisory and controversy work supporting estate, entity and client matters |
| Finance | 8 | The firm's own books — AR, AP, payroll, close, unclaimed property |
| Security | 5 | Breach-response support for the privacy and cyber practice |
| Knowledge | 4 | The firm's clause bank, precedent library, and SOPs |
| Employment | 4 | Employment-law advice for business clients, plus hiring staff |
| Corporate | 5 | Minutes, meeting prep, data rooms, succession documents |
| Real estate | 3 | Lease abstraction, transaction coordination, fair housing |
| Insurance | 3 | Coverage disputes, subrogation, cyber cover |

Every legal pack must declare a deliverable, name a human consumer, and carry a
review or approval gate. Two internal-workflow seats (`legal_intake`, `legal_km`) are
the only exceptions, and a test guards that exception list so it cannot quietly grow.

**Gap worth naming:** none of the 77 legal packs cover veterans' benefits. VA disability
is the firm's practice area with the least support from the inherited roster and the
most to gain from purpose-built seats — see the [roadmap](#roadmap).

Prove the roster's safety properties:

```bash
maverick domains-lint     # 0 errors across all 125 packs
maverick domains-audit    # no drafting agent can reach a state-mutating tool
maverick domains-eval     # behavioral golden cases, including "never invent authority"
```

## Setup

```bash
git clone https://github.com/Daybreak-AI-Labs/Law_Firm && cd Law_Firm

python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -e ./packages/maverick-core                 # kernel, with deps
for p in maverick-shield maverick-channels maverick-evolve \
         maverick-dashboard maverick-mcp maverick-knowledge; do
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

| Command | What |
|---|---|
| `maverick init` | Setup wizard with preflight and API-key validation |
| `maverick doctor` | Health check with remediation hints |
| `maverick start TITLE` | Run one goal |
| `maverick chat` | Interactive REPL |
| `maverick dashboard` | Local web UI and REST API |
| `maverick mcp` | MCP server, for driving the platform from Claude Code or Cursor |
| `maverick logs / status / resume` | Inspect and control running work |
| `maverick schedule` | Recurring autonomous goals via cron |
| `maverick budget` / `spend` | Cost history, per goal and per tag |
| `maverick audit verify` | Verify the hash-chained audit log |
| `maverick template list` | Starter goals |

The CLI also answers to `lightwork`, and every `MAVERICK_*` environment variable can be
spelled `LIGHTWORK_*`. Internals keep the upstream `maverick` package names so fixes
from upstream stay portable; only the product surface carries the firm's name.

## Layout

```
packages/
  maverick-core/       Kernel: orchestration, world model (SQLite/Postgres),
                       providers, sandboxes, the 125 domain packs, budget caps
  maverick-shield/     Prompt/tool/output screening
  maverick-channels/   Channel adapters (email, Slack, Signal, …)
  maverick-dashboard/  FastAPI web UI + REST API at /api/v1
  maverick-mcp/        MCP server
  maverick-evolve/     Self-improvement loop
  maverick-knowledge/  Document parsing, chunking, embedding, retrieval
apps/
  desktop/             Tauri shell
  installer-cli/       Setup wizard
docs/                  Architecture, configuration, deployment, safety, API
```

## Internal integration surface

The application is Python-first. Its MCP server remains available for trusted local
automation, but the former public language SDKs, sample clients, editor plugins, mobile
companions, and embeddable widgets are intentionally not part of the firm's supported
product. New integration surfaces should be added only for a concrete firm workflow.

## Confidentiality

**Not yet fit for real client data.** The platform is provider-agnostic by design, but
no confidentiality gate is in place: nothing currently stops matter content reaching
whichever model provider is configured. Before a real client file goes in, the firm
needs a decided posture — zero-retention terms with the provider, a local model for
sensitive matters, or both — and a gate that enforces it. This is the first item on the
roadmap for a reason; Rule 1.6 does not have a "we were still setting it up" exception.

The platform self-hosts and needs no outbound network access beyond the model provider
you choose. All matter data stays on the machine or server you run it on.

## Roadmap

Not built yet, in rough priority order:

1. **Confidentiality gate** — matter-sensitivity flags and a hard block on privileged
   content reaching an unapproved provider.
2. **VA disability packs** — the largest gap, and federal, so one build serves all three
   states. Intake and accreditation (VA Form 21-22a), the AMA lanes (supplemental claim,
   higher-level review, Board appeal), C-file review and evidence development, nexus
   letters and DBQs, rating analysis under 38 C.F.R. Part 4, TDIU, and fee-agreement
   compliance under 38 C.F.R. § 14.636 — where the fee rules are strict enough to be
   worth encoding as a hard gate rather than a checklist. Verify current form numbers
   and rule text when authoring; VA forms change.
3. **State practice packs** — family law (custody, support, property division, marital
   settlement agreements), estate planning (wills, RLTs, POAs, advance directives),
   probate, and residential closings — each parameterized by jurisdiction rather than
   written three times.
4. **Matters and clients** — a matter model carrying practice area *and* jurisdiction,
   with conflicts checking across all three states and deadline docketing.
5. **Time, billing, and trust accounting** — three IOLTA regimes, not one. Florida's
   trust-accounting rules are the strictest of the three and should set the floor the
   ledger is built to; Georgia and Tennessee then fit inside it.
6. **Court e-filing and rules-based calendaring** — three systems: Florida's statewide
   portal is the most uniform, Georgia is Tyler-based, and Tennessee varies by county.
   Sequence them in that order; the uniform one proves the design.
6. **Retargeted self-improvement** — learning from the attorney's edits to drafts,
   building a clause bank out of executed documents, and grounding it all in outcomes.

## People

Two of us, with local accounts and matter-level access control: owner, associate,
and paralegal roles. The invite flow works today.

**Admission status matters for how this gets used.** Until we are admitted, the
platform is for building and testing — not for producing work product a client
relies on, which would be practicing law without a licence regardless of who or
what drafted it. The confidentiality gate and the VA packs are the things worth
building in the meantime, so the platform is ready the day the licences are.

## License

Proprietary. See [LICENSE](LICENSE). This is internal software for Bjerken and Day; it
is not licensed for use, redistribution, or derivative works by anyone else.
