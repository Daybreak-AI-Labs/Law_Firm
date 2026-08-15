# The pitch flow

The exact click path for demoing the PIA Concierge end-to-end. Anyone on the
team can drive from this page. Total runtime ~10 minutes plus questions.

## Before the audience arrives

```powershell
cd C:\Users\Cstep\Lightwork\demo\pia-concierge
powershell -ExecutionPolicy Bypass -File .\reset-demo.ps1
```

One-time setup for full speed: set your model key at user level —
`setx ANTHROPIC_API_KEY "<your key>"` in any terminal, once, then open a new
one. The reset script picks it up automatically and the checklist confirms
it. Never put the key in a file in this repository.

Wait for the green **"Demo is ready"** line (~2 minutes — it reseeds 14 months
of history so every screen is lived-in). Then open these tabs in order, left
to right, so alt-tabbing tells the story:

1. `http://127.0.0.1:8890/servicenow` — ServiceNow (the requester's world)
2. `http://127.0.0.1:8890/mail` — the privacy office inbox
3. `http://127.0.0.1:8890/onetrust` — OneTrust (the system of record)
4. `http://127.0.0.1:8765/overview` — Lightwork (the governed platform)

Each tab carries its own icon and title, so the taskbar reads as four
separate applications.

## The story, beat by beat

**Beat 1 — work arrives like it really does.** In ServiceNow, open the
privacy-request queue and pick the open ticket. This is the moment every
privacy team knows: a business owner wants a new vendor, the request lands in
a queue, and a human used to spend a week on it.

**Beat 2 — the agent runs the interview.** From the ticket, start the
concierge intake. Use the presenter auto-fill (it answers like a real,
slightly over-sharing business owner) or answer by voice. Point out: the
agent skips questions it can already answer from the vendor's documents and
prior reviews — it gets faster with every assessment.

**Beat 3 — the deliverables file themselves.** Switch to OneTrust → the
vendor's record → **Documents**. The risk analysis and privacy-notice
cross-check are already filed — versioned, dated, attributed to the
Concierge, and they open as designed Word documents (navy headings, severity
colors, page footers). Nothing here is a mock-up; open one.

**Beat 4 — paper gets negotiated.** Still in OneTrust, show the DPA
tracked-changes redline (opens in Word with real revisions) and the
our-paper draft with auto-filled values in red. This is the part audiences
assume is impossible; it's just Tuesday for the agent.

**Beat 5 — the human stays in charge.** Switch to Lightwork → Privacy. The
same assessment is in the reviewer's worklist with risk ratings and follow-up
questions. Approve it live. Then open **Oversight**: every guardrail action
is on the record, and the kill switch is one click.

**Beat 6 — govern the agents you already have.** Stay in Lightwork and open
**External agents** (`/external-agents`). The roster shows `sf-quotebot` — a
Salesforce Agentforce quoting agent the sales team already runs. Lightwork
doesn't orchestrate it; it governs it: an owner, a department, tool ceilings,
and a $250/month budget with spend metered against it. Click through to the
scorecard: two weeks of quoting runs on the same Operating Record as the
native workforce, one honest failure included. Then open **Approvals**
(`/approvals`) — the contract it asked to send is parked there, labeled
**external agent · BYOA gateway**, waiting on a human. **Workforce**
(`/workforce`) tells the same story: an External agents segment right beside
the native departments. The point: keep Agentforce, keep Copilot — Lightwork
is the governance layer over all of them.

**Beat 7 — the receipts.** Finish on the Privacy **command center** board
(`/privacy/board`) and **Savings**: a year of opened-vs-decided flow,
residual-risk mix, first-pass acceptance trending up, and dollars saved
computed from the client's own cost assumptions — never invented numbers.

## The lines that land

- "Every number you saw is computed from this deployment's own signed
  record. There is no demo mode — you just watched the product."
- "The agent drafted; your reviewer decided. That separation is enforced by
  the runtime, not by policy documents."
- "This filed into a OneTrust-shaped system today. It files into *your*
  OneTrust the same way."
- "You don't have to fire the agents you already hired. Agentforce keeps
  running — Lightwork is what makes it governable."

## If something looks wrong mid-pitch

Ctrl+C the server window, rerun `reset-demo.ps1`, and you are back to a
pristine, fully seeded demo in about two minutes.
