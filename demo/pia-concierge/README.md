# PIA Concierge — governed privacy-assessment demo

## Two SKUs from one codebase

PIA Concierge sells two ways:

* **Standalone agent** — sold on its own. Run `bash run_standalone.sh` (sets
  `PIA_STANDALONE=1`). The agent takes the intake (chat / voice / guided),
  scores the risk on its **own self-contained engine** (`pia_engine.py`,
  imports nothing from `maverick`), files into OneTrust, and a human reviews
  and approves there. There is **no audit trail, no governance, no learning,
  no dashboard, and no control-catalog citations** — those are Lightwork
  features. The agent still works end to end. `/about` shows the live
  capability matrix.
* **Lightwork platform** — the same agent bundled with the governed engine,
  the Ed25519 signed audit chain, the world-model approval queue, the privacy
  workspace + program insights, cross-run learning, and the connectors.

The seam is `capabilities.py` (what's on) and `backend.py` (binds each
capability to the platform or its standalone stand-in). The app never branches
on the mode itself — set `PIA_STANDALONE=1` to preview the standalone SKU on a
machine that has the platform installed.

**Installing for a client?** See **[DEPLOYMENT.md](DEPLOYMENT.md)** — the
partner install guide covering Docker (`Dockerfile.standalone` +
`requirements-standalone.txt`), AWS (App Runner / ECS / EC2), GCP (Cloud Run /
GCE), Azure (Container Apps / VM), Linux systemd, Windows, and macOS, plus the
TLS/auth requirements, environment reference, backups, upgrades, and the
go-live checklist.

**What a standalone client gives up (and what unlocks it):** the signed audit
chain, the governed approval queue and program workspace, the full framework
catalog with control citations, cross-run memory / suggested answers,
connected-source document discovery, the Art. 28 clause-by-clause DPA review,
and model-assisted chat understanding — all of it comes with Lightwork.

---

**The agent does input + analysis; the human approves in OneTrust.** A
privacy-assessment request submitted in any ticketing system is picked up by
Lightwork, which interviews the requester — a full **chat experience** (typed
or **voice**) or one-click guided answers, switchable mid-interview — scores
the risk with the real assessment engine, maps required controls with
framework citations, and files the completed analysis **straight into
OneTrust as Under Review**. The privacy team reviews and approves it *inside
OneTrust*; the decision is mirrored onto the governed approval row and the
real Ed25519-signed audit chain in the background. Vendors that were already
assessed take the short path: "just adding documents" appends an analyzed
addendum to the existing OneTrust record — no second interview.

## What is real vs. simulated

| Real (actual product code) | Simulated (this harness) |
|---|---|
| Goal + live timeline on the dashboard (`/goals`, `/chat/goal/{id}`) | ServiceNow tenant (`:8890/servicenow`) — fires a real HTTP webhook |
| PIA scoring — `maverick.assessment` (GDPR Art. 35 template) | OneTrust tenant (`:8890/onetrust` + `/ot-api`) — speaks the field-verified wire protocol (see `ONETRUST-INTEGRATION.md`) |
| Control mapping — `maverick.controls` (GDPR/ISO 27001/SOC 2/NIST/HIPAA citations) | Requester mailbox (`:8890/mail`) — real SMTP+STARTTLS delivery, captured locally |
| Approval — real world-model approval row; the OneTrust Approve button (and the Lightwork queue) both decide it | Requester questionnaire (`:8890/intake/{case}`) — chat (type/voice) or guided |
| Email send — `maverick.tools.email_tool` (SMTP) | SharePoint tenant (`:8890/graph-sim`) — Graph-shaped search + download, seeded with each vendor's SOW/MSA/DPA/security PDFs |
| OneTrust filing — `maverick.tools.onetrust_tool` (confirm-gated POST) | |
| Signed audit chain — `maverick.audit` (verify with `maverick audit verify`) | |
| Evidence storage — `maverick.attachments` (real goal attachments: size caps, mime allowlist, executable deny) | |
| Doc discovery — `maverick.doc_discovery` (searches connected sources for the SOW/contract/DPA) | |
| Addendum analysis — `maverick.privacy_ops` Art. 28 clause review on appended documents | |

Point `ONETRUST_HOSTNAME` at a real OneTrust sandbox and the filing is live —
same code path. Any ticketing system that can send a webhook can replace the
mock ServiceNow. Export a real `MSGRAPH_ACCESS_TOKEN` (and unset
`MSGRAPH_BASE_URL`) and "Find my documents" searches your real SharePoint /
OneDrive — same code path there too.

Chat answers are interpreted by scripted rules (identical behavior with no
key and no network); set `ANTHROPIC_API_KEY` and ambiguous replies get a
model-assisted read, degrading back to a scripted clarification on any error.
Voice input uses the browser's Web Speech API (Chrome; allow the mic) and
"Read questions aloud" speaks the agent's side — no server dependency.

**The agent answers what it can prove.** Attached documents, the ticket's
own data-types declaration, and the vendor's last review pre-answer intake
questions with quoted provenance ("Yes — from Acme DPA.pdf: '…'"); the
interview skips them visibly and the reviewer sees every source in the
answer notes. A known vendor opens with the full memory (last record, risk,
who decided, when, documents on file) and offers "just adding documents" —
appended files get an on-the-spot Art. 28 clause review diffed against the
last review's gaps. The landing page's **speed story** strip counts it all:
median ticket→decision minutes, evidence-answered ratio, and hours saved
against an explicitly labeled baseline (`PIA_MANUAL_BASELINE_HOURS`,
default 6).

The launcher seeds **a year of backdated OneTrust history** (27 records:
a live Under Review queue, completed reviews, a sent-back, addenda) so the
tenant reads as sustained use — set `PIA_SEED_TENANT=0` for a clean slate.
Every seeded vendor record also carries its **openable documents** (the risk
analysis and notice cross-check the live run attaches), and two vendors carry
a filed paper round — analysis memo + a real tracked-changes redline produced
by the actual engine — so the record pop-out's **Documents** section has
things to click on before you've run anything. Fixtures live only in the
demo store; the world model and signed audit chain stay clean.

## Run it

```bash
bash demo/pia-concierge/run_demo.sh
```

- Lightwork dashboard → http://127.0.0.1:8765 (goals · approvals · audit)
- External world → http://127.0.0.1:8890 (ServiceNow · inbox · OneTrust)

No LLM key and no external accounts needed. State lives in
`demo/pia-concierge/.demo-home` (delete it for a clean slate). Both servers run
in ONE process on purpose: the audit chain's signer requires a single writing
process — the same shape as `maverick dashboard` itself.

**Make it look like a year of use** (optional, recommended before a pitch):

```bash
MAVERICK_HOME=demo/pia-concierge/.demo-home python3 demo/pia-concierge/seed_workspace.py
# PowerShell: cd demo\pia-concierge; $env:MAVERICK_HOME = "$PWD\.demo-home"; python seed_workspace.py
```

Seeds ~436 assessments over 14 months through the real engines (approved on
review cadences with a due-for-re-review queue, live backlog, varied risk),
plus DPA reviews, the AI registry, a populated Art. 30 register, a DSAR queue
with history, and incidents in three states. Run it BEFORE starting the
servers or restart them after. Marker-guarded; `--force` adds another batch.

## The Privacy workspace (right window → Govern → Privacy)

The program-of-record view worth showing after (or instead of) the ticket
flow — everything below runs on real records:

- **Worklist** — every assessment with the inherent→residual risk pair,
  status, follow-ups, and the re-review countdown; filters for needs-review /
  awaiting-answers / high-residual / due-for-re-review. The **Review** pop-out
  approves with a cadence (records come due), rejects, or sends questions
  back — without leaving the page.
- **DPA reviews** — paste a vendor's terms (or search connected sources, or a
  digital PDF) for clause-by-clause Art. 28(3) verdicts where every call
  quotes its evidence.
- **AI registry** — EU AI Act tier screening with signals + obligations; a
  completed AI risk assessment registers its system with an upward-only tier
  ratchet.
- **RoPA** — Art. 30 entries drafted from PIAs, imported from a OneTrust CSV
  export, exported back out as the register an authority asks for.
- **DSARs** — paste the email a data subject actually sent; the tracker
  detects the kind (Art. 15/17/20), starts the 30-day clock, runs the real
  export for access, and only ever *prepares* the erase command.
- **Incidents** — the Art. 33 72-hour clock, with the notify-or-document
  decision recorded both ways.
- **Assessment catalog** — click any framework to edit its questionnaire, or
  author a new one; custom versions override built-ins until deleted.

`Govern → Finance` is the same chassis over SOX / fraud / ITGC / credit /
close-readiness — the "one workspace per department" proof.

## The reviewer desk (`/reviewer`) — when the vendor sends their own paper

A privacy reviewer signs in and gets **their own queue, numbered**; picking a
number (click, or type it) opens one case and asks the question that decides
the next hour of work: *is this vendor on our paper, or theirs?*

On **our** paper the desk does the drafting: it generates our template
instrument as a real Word document with the vendor-specific values (party
names, effective date) filled **in red** for counsel to verify, files it onto
the vendor's OneTrust record under version control with a cover note listing
every auto-filled value, and shows it in the rounds table with a **Draft**
button. Deterministic — the clause body is the operator's playbook; no model
touches it. On **their** paper, upload the document (`.docx` or a digital
PDF) and the desk does the review by hand-equivalent steps:

1. **Reads it** — stdlib extraction, and a scanned PDF is refused honestly
   rather than reviewed as "10 clauses missing".
2. **Decides the instrument** — GDPR Art. 28 DPA vs CCPA/CPRA privacy
   addendum, scored from the text and filename; a tie resolves to the stronger
   instrument and says so.
3. **Compares it to our template** — clause by clause. A missing clause is a
   gap; a *negated* one is unclear; and language that bargains for the opposite
   of our position ("may engage sub-processors at its sole discretion") is
   reported as **conflicting** and escalated to high severity. Presence
   detection alone would score that last one as satisfied.
4. **Writes two deliverables** — an analysis memo (concerns, their wording, the
   required change, the basis in law) and **their own document marked up with
   real Word tracked changes** you can Accept/Reject clause by clause.
5. **Files both onto the vendor's OneTrust record** under the next sequential
   version for that vendor + instrument, so the Documents tab reads as one
   legible negotiation history (`v1`, `v2`, `v3`) instead of same-named files.
6. **Tracks the negotiation across rounds** — when the vendor's renegotiated
   draft comes back, upload it as the next round and the memo (and the chat)
   reports **which of our demanded changes they accepted** and which they
   still refuse, so the history reads as a converging negotiation.

Everything filed is **openable where it landed**: the rounds table has a
**Memo** button (renders in the browser) and a **Redline** button (downloads
the tracked-changes `.docx` for Word), and the same files appear under
**Documents — vendor record** in the OneTrust record pop-out, where the memo
opens inline and the redline downloads. The mock tenant retains attachment
bytes (bounded) exactly so this click works in the demo.

The governance line is the point: **which clauses fall short is always decided
deterministically** — a model can neither invent a finding nor clear one. A
model is used only to re-word *our* required clause into the vendor's defined
terms, and any draft that drops a load-bearing term is thrown away in favour of
the template language. Set `[paper_review] use_model = false` (or answer no in
the wizard) and no model sees the contract at all; the findings are identical.

## Demo script (~6 minutes) — Part 1: the agent alone, Part 2: the platform

Two browser windows: **left** = external world (:8890) — this is ALL of
Part 1: the agent plus the tools the customer already uses (ServiceNow,
email, OneTrust). **right** = Lightwork dashboard (:8765) — Part 2, the
governance layer, shown after the value lands.

**Part 1 — the agent (left window only):**

1. **[ServiceNow]** File the pre-filled privacy request. *"An employee asks
   for a privacy review the way they already do — a ticket. They never learn
   a new tool."*
2. **[Requester inbox]** Open the intake email, click the link. The interview
   opens in **Chat** mode — tap the 🎤 and *talk* an answer, type another in
   plain words, then flip the toggle to **Guided** to show the one-click
   version. Click **Find my documents**: the DPA and security overview attach
   AND auto-answer their questions with quoted evidence — the interview skips
   them visibly. *"The agent asks only what the paperwork can't prove. Every
   skipped question shows its source, and the requester's own words are kept
   for the reviewer."*
3. **[OneTrust]** The money shot: your assessment is **already at the top of
   the queue, Under Review**, in a tenant that shows a year of history — open
   it: the full interview verbatim with the provenance notes, findings,
   required controls with GDPR / ISO 27001 / SOC 2 / NIST citations, and the
   **Agent speed** row. Scroll to **Documents — vendor record** and open the
   risk analysis and notice cross-check the agent wrote — real Word
   documents; any paper-review redline or our-paper draft (red-filled
   values) downloads there too. Click **Approve**. *"The agent did the input and the
   analysis; your team reviews and approves in the tool they already live
   in. Nothing is Completed until a person clicks."*
4. The record flips to Completed; the ServiceNow ticket is Resolved with the
   risk rating in the work notes; the requester got a completion email; the
   landing page's **speed story** strip updates. *"Repoint one hostname at
   your OneTrust tenant and this is live."*
5. **[OneTrust record]** Click **＋ Add document to this vendor** and drop the
   updated DPA. *"Already assessed the vendor and just adding stuff? The
   addendum is clause-reviewed on the spot, diffed against the last review's
   gaps, and flagged for re-review only if the risk picture changed."* (Same
   from chat: a new ticket for a known vendor opens with everything we know
   and offers "just adding documents" — or a fresh assessment that carries
   the old answers forward.)

**Part 2 — the platform (right window):**

6. **[Govern → Privacy]** The program of record behind the agent: worklist
   with re-review cadences, DPA reviews, AI registry, RoPA, DSARs,
   incidents, the board-pack report.
7. **[Govern → Audit]** The silent governance: GOAL_START → TOOL_CALL(email)
   → INTAKE_PREFILL → ASSESSMENT_REVIEW → AUTONOMY_GATED →
   TOOL_CALL(onetrust) → APPROVAL_DECISION (via onetrust). *"Your reviewer
   never left OneTrust, yet every step — including their click and every
   auto-answered question — is on a tamper-evident, Ed25519-signed chain.
   That's how assessments get faster without losing the audit."*

Optional closer (terminal):

```bash
MAVERICK_HOME=demo/pia-concierge/.demo-home maverick audit verify
MAVERICK_HOME=demo/pia-concierge/.demo-home maverick assess list
```

## Preflight (demo day)

```bash
pkill -f serve.py; rm -rf demo/pia-concierge/.demo-home   # clean slate
bash demo/pia-concierge/run_demo.sh                        # then dry-run once
```

- Dry-run the whole flow once, then reset `.demo-home` again.
- Voice input: use Chrome and grant the mic on the intake page during the
  dry-run so the permission prompt doesn't appear on stage. No key needed.
- Don't set `MAVERICK_DASHBOARD_TOKEN` locally (it gates every page).
- The launcher already sets `MAVERICK_SHIELD_PROFILE=strict` and
  `MAVERICK_DEFAULT_MAX_DOLLARS=10`.
- Keep both windows pre-opened; the OneTrust page refreshes on reload only —
  F5 after the requester submits.

## Architecture

```
ServiceNow (mock, :8890) ──webhook──▶ harness ──create_goal/append_event──▶ world.db ◀── dashboard (:8765)
                                        │                                        ▲
        requester inbox (SMTP sink) ◀───┤ email_tool                             │
        chat/guided /intake/{case} ─────┤ interpret (scripted, LLM-assist opt.)  │
                                        │ assessment engine + controls           │
                                        ├─create_approval (mirror) ────────────▶─┘
                                        └─onetrust_tool POST (Under Review) ──▶ OneTrust (mock)
        OneTrust "Approve" ──▶ decide approval + assessment record ──▶ Completed · ticket resolved · email · goal done
        "Add document" / chat append ──▶ Art. 28 clause review ──▶ addendum (+ re-review flag)
        every step ──▶ maverick.audit (Ed25519 signed chain)
```
