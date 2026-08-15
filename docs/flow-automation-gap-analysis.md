# Workflow automation — gap analysis & improvement plan

> Target: easier to use than Power Automate, more capable than UiPath, with two
> first-class authoring modes — a visual designer and a chat experience that
> builds (and edits, and fixes) the flow for you.
>
> Scope: the Flow engine (`packages/maverick-core/maverick/flow/`), its
> triggers/import/connector plumbing, and the dashboard authoring surfaces
> (`packages/maverick-dashboard`). Audited 2026-07.

---

## 0. Progress since this audit (2026-07)

Much of the plan below has shipped; `docs/FEATURES.md` is the current source of
truth. Closed since the audit:

- **G3 Designer ergonomics** — undo/redo, duplicate, click-to-cut edges,
  zoom-to-fit, multi-select marquee, minimap, insert-into-edge, and nested
  foreach/parallel/scope body drill-in.
- **G4 Data experience** — one-click **data pills** (`{{key}}` from the outputs a
  node can actually see upstream); ordering comparisons now numeric-or-lexical so
  date/string conditions work, plus `now()`/`today()`/`add_days()`.
- **G6 Debugging** — a dedicated **run viewer** (node timeline, redacted run data,
  measured per-node seconds, origin + $ cost) and live canvas overlays.
- **G7 Node vocabulary** — `switch` (n-way), `while`, `scope` (try/catch),
  `wait_event`, `setvar`, approval **choice verdicts** + **expiry**, and
  **parallel `foreach`** (`concurrent`). (`try` shipped then consolidated into
  `scope`.)
- **G5 Triggers** — cron (with per-flow **timezone**/DST), webhook, email (imap),
  form, file, RSS, http_json polling, OAuth'd sources, GitHub issues.
- **Chat copilot (G2)** — a conversational designer assistant that edits the
  canvas via validated patches and diagnoses "why did this fail?".
- **Engine/production** — per-node **retry backoff** (+ hard-exception retries),
  per-flow **concurrency cap** (singleton), `{{secret('NAME')}}` from the vault,
  **typed manual-run inputs** (validated form + run idempotency key), and
  **sub-flow argument passing / isolation** (`subflow_inputs`).
- **Self-learning moat** — the self-rewrite loop now **infers the tool** a
  reliable agent node calls to auto-harden it (`node_tools.py`), the autonomy
  knobs (`auto_evolve`/`auto_apply`) are toggleable from the Learning page, and an
  honest "workflows improved N times" moat number.

Still open (largely UX/glue, not engine): **G1** product coherence/nav, **G8**
import branch-fidelity for PA/UiPath, **G9** connection-management UI, **G10**
template gallery + sharing.

---

## 1. Where we stand — honest scorecard

| Area | Status vs Power Automate / UiPath |
|---|---|
| **Execution engine** | **At or above par.** 8 node kinds, retries, on-error routing, per-node timeouts, whole-flow deadline + spend ceiling, pause/resume approvals, versioning + rollback, idempotency keys, secret redaction. |
| **AI-native steps** | **Ahead — neither competitor has this.** `agent` nodes run a plain-English brief as a governed goal; the self-rewrite loop hardens reliable agent nodes into deterministic actions and softens flaky actions back to agents, with measured before/after impact and auto-revert. |
| **Connectors** | **Breadth par, UX behind.** ~2,878 registry connectors + ~68 bespoke tools + governed simulate-before-commit writes; but auth is env-var convention, with no connection-management UI. |
| **Migration** | **Ahead in ambition, behind in fidelity.** Importers for Power Automate, UiPath, n8n, Make, Workato (definition import) + Zapier, Notion (connect-and-trigger). Only n8n preserves branches; the rest flatten to a linear chain. |
| **Visual designer** | **Well behind.** A legitimate hand-rolled SVG canvas (drag, connect, zoom/pan, nested sub-flow drill-in, live run overlay) — but ~700 lines of vanilla JS with no undo/redo, copy/paste, multi-select, edge deletion, minimap, snapping, or search. |
| **Chat experience** | **Well behind the stated goal.** Two disconnected one-shot drafters; no conversation, no incremental graph editing, no "why did this fail → fix it." |
| **Data binding UX** | **Behind.** `{{key}}` templating + 16 safe functions is a solid *engine*; there is no dynamic-content picker, no schema inference, and conditions are hand-typed strings. |
| **Debugging** | **Behind.** Canvas status badges + a 25-row run list; no dedicated run viewer, no per-step input/output inspection, polling not streaming. |
| **Product coherence** | **Behind — self-inflicted.** Two systems both called "workflows," three SVG/form editors, and the flow designer is not even in the main nav. |

The strategic read: **the engine is a differentiated asset; the authoring and
debugging experience is what loses the comparison today.** Nearly all of the
work below is UX and glue, not engine work.

## 2. What exists today (inventory)

Two separate systems share the "workflow" name:

1. **Flows** — the real graph engine. `maverick/flow/ir.py` defines 8 node
   kinds (`agent`, `action`, `branch`, `foreach`, `parallel`, `approval`,
   `delay`, `subflow`) with per-node `retries`/`on_error`/`timeout`/`output`
   and flow-level `max_seconds`/`max_dollars`/`schedule`. `runner.py` is a
   pure interpreter (injected executors, testable without an LLM);
   `execution.py` binds agent nodes to governed goals and action nodes to the
   tool registry, with budget reservation per node; `store.py` persists
   versioned definitions + resumable runs; `evolve.py`/`node_outcomes.py`
   implement the self-rewrite loop; `approvals.py` mints signed HMAC
   approve/reject links. Edited in the SVG designer at `/flows/designer`
   (`flow_designer.html/.js`), API under `/api/v1/flows/*` (`api.py:915-1273`).
2. **"Workflows"** — AI-drafted *text* artifacts (parameterized prompt
   templates and agent playbooks) built in the form-based `/workflow-builder`
   (`workflow_builder.html`, `workflow_ai.py`). No graph. Separate draft/refine
   endpoints (`/api/v1/workflows/draft|refine`).

Trigger plumbing: cron on the flow itself (`Flow.schedule`), 7 polled event
sources (`automation_events.py`: `http_json`, `oauth_http_json`, `rss`,
`github_issues`, `imap_email`, `file_dir`, `form`), inbound HMAC webhooks
(`/webhook/run`), hosted forms (`/form/{token}`), and importer-armed
schedules/webhooks. Event triggers can target a template **or a flow**
(`api_schemas.py:157-164`); webhook triggers can target **templates only**
(`api.py:2520-2530`).

## 3. The competitive frame — what we must exploit

What actually makes Power Automate hard (from years of user complaints, and
what our design should refuse to replicate):

- **The expression cliff.** The UI is friendly until you need
  `@{coalesce(triggerBody()?['x'],'')}` — then every user hits Workflow
  Definition Language. There is no gradual ramp.
- **Connection/licensing archaeology.** Premium connector tiers, per-flow
  plans, DLP policies, broken connection references on import/export.
- **Debugging by archaeology.** Run history is a click-into-each-step
  affair; no live view; fixing a failed run means re-running the whole flow.
- **NL is bolted on.** Copilot drafts a first flow but is weak at *editing*
  an existing one, and can't explain or repair a failed run.

UiPath's weakness is different: Studio is a heavyweight desktop IDE aimed at
RPA developers; citizen users get a reduced StudioX. The learning curve *is*
the product barrier.

Our structural advantages neither can copy cheaply:

- **The `agent` node.** When a user can't express a step deterministically,
  they write a sentence. Power Automate has no fallback below "give up";
  we have "let the agent do it, governed and budgeted."
- **Self-evolution.** Flows that measurably harden over time (agent→action)
  is a story neither competitor has: "your flow gets cheaper and more
  deterministic the more it runs."
- **Governance built in.** Simulate-before-commit writes, risk tiers, spend
  ceilings, signed approvals — enterprise controls PA sells as add-ons.
- **Import from everyone.** Seven-platform import is a migration wedge.

## 4. Gaps (ranked by impact on "easier than PA, better than both")

### G1 — Product fragmentation (highest leverage, lowest cost)
Two systems named "workflow," three editors (`flow_designer`,
`workflow_builder`, `graph_editor`), and the flagship designer is reachable
only via a button on the Automations page — it is absent from the main nav
(`base.html:630`). A template is already formally the degenerate 1-node flow
(`ir.py:243-256`, `single_agent_flow()`), so the split is presentational, not
architectural. Users cannot find, let alone prefer, the good tool.

### G2 — The chat experience is one-shot, not conversational
The stated goal is "chat that builds the flow for them." Today:
- The designer's ✨ button is a single text input → whole-graph replacement
  (`flow_designer.js:525-536` → `POST /flows/draft` → `draft.py`). No
  follow-up turns, no editing an existing graph, no explanation.
- `workflow_ai.py` has a draft→refine loop, but for the *text* system, and
  refine is full-document regeneration, not a patch.
- Neither drafter can see run history, so neither can answer the two
  questions users actually ask: *"why did this fail?"* and *"change it so
  that…"*.

### G3 — Designer ergonomics below the commercial bar
Present: drag nodes, drag-to-connect, pan/zoom, 8-node palette, per-kind
property panel, nested foreach/parallel drill-in with breadcrumbs, BFS
auto-layout, live run overlay. Missing vs any mature canvas: **undo/redo,
copy/paste/duplicate, multi-select + marquee, edge deletion/reconnection,
minimap, snapping/alignment, node search, insert-on-edge, zoom-to-fit,
touch support**. Also no client-side structural validation before save (only
JSON-param checks + server round-trip).

### G4 — No dynamic content experience ("data pills")
The engine's `{{key}}` templating with 16 safe functions (`runner.py:79-97`)
and string conditions (`eval_condition`, `runner.py:213-227`) is sound and
safely non-`eval`. But the *authoring* experience is "type the right dotted
key into a textarea." Power Automate's single best UX idea — the dynamic
content picker that shows you what data is available from upstream steps —
has no equivalent, and nothing infers step output schemas from test runs.

### G5 — Trigger gaps
- Webhooks cannot target a flow — `TriggerIn` binds templates only
  (`api.py:2520-2530`); event triggers already support both. An inbound
  "when Stripe/GitHub/Jira calls, run this flow" is table stakes.
- Trigger configuration is scattered: cron lives in the designer, webhooks
  and event triggers live on the Automations page. A flow has no single
  "when does this run" panel showing all its triggers.
- No **wait-for-event** node (pause mid-flow until a webhook/form/reply
  arrives — the engine's pause/resume machinery already supports the shape;
  approvals are the special case that exists).

### G6 — Debugging and observability
- No dedicated run page: monitoring is canvas badges + a 25-row list
  (`automations.html:676-714`). No per-step input/output inspection, no
  timeline, no durations, no cost breakdown per node (the data exists in
  `FlowRun.nodes`).
- Live status is 20 polls × 900 ms (`flow_designer.js:505-524`) — a run
  longer than ~18 s silently stops updating. No SSE/WebSocket stream.
- Retry restarts from scratch (`store.py` retains `input_data` only);
  there is no resume-from-failed-node, even though per-node trace + threaded
  `data` are persisted.
- No step-level test ("run just this node with this sample input").

### G7 — Node vocabulary gaps
Missing vs PA/UiPath: **switch/case** (n-way branch), **while/until** loop,
**try/catch scope** (on_error exists per node but there is no grouped error
scope), **wait-for-event** (G5), richer **human task** (approval is binary
approve/reject with one prompt; no choices, no form input on approve, no
assignee/escalation/expiry). `foreach` is sequential-only (no parallel
iteration option).

### G8 — Import fidelity
`to_flow.py:14-20` documents it: only n8n preserves IF/Filter as `branch`
nodes; Power Automate, UiPath, Workato, Make imports **flatten to a linear
chain**, silently discarding conditions/loops/scopes. For the two platforms we
most want to migrate from, that means imports are demos, not migrations. There
is also no fidelity report telling the user what was preserved vs approximated
vs dropped.

### G9 — Connector authoring UX
2,878 connectors resolve credentials by env-var convention
(`<NAME>_BASE_URL`/`<NAME>_TOKEN`, `enterprise_connectors.py:15-26`). Fine for
operators; a non-starter for the citizen-developer audience PA serves. There is
no connection-management UI (create/test/share a connection), and the
designer's tool picker is a flat catalog (`GET /flows/tools`) over ~3k names
with no categories, search ranking, param schemas, or per-tool docs.

### G10 — No gallery, sharing, or collaboration
No flow template gallery ("start from: triage inbound email"), no
export/import of flow JSON in the UI, no sharing/roles per flow, no
co-editing. PA's template gallery is a major activation lever.

## 5. What must not be lost

These are already better than both competitors — the plan below builds the UX
*around* them rather than replacing anything:

1. `agent` node as the universal escape hatch (`execution.py:143-162`).
2. Self-rewrite with measured impact + rollback (`evolve.py`,
   `/flows/insights`, `/flows/{id}/nodes/{n}/impact`).
3. Budget governance: per-node reservation against `max_dollars`
   (`execution.py:37-55`), wall-clock deadline, spend caps on drafting.
4. Safe expression engine — no `eval` anywhere (`runner.py`).
5. Versioning + rollback as first-class store behavior (`store.py:54-115`).
6. Seven-platform import surface (`automation_import/`).
7. Dry-run sandbox executors (`sandbox_runners`) — test without side effects.

## 6. Improvement plan

Phases ordered by (impact on the goal) / (engineering cost). Phases 1–2 are
the "easier than Power Automate" story; 3–4 reach parity on polish; 5 is the
"better than both" story.

### Phase 1 — One product, findable and triggerable (mostly glue)
- **Merge the two workflow systems in the UX.** One "Automations" home: every
  automation is a Flow; templates render as single-agent flows (the IR already
  says so). `workflow_builder`'s draft-from-document and playbook editing
  become panels/routes within the same surface. Kill the duplicate naming.
- **Put the designer in the main nav** (`base.html`).
- **Webhook → flow binding**: extend `TriggerIn`/`triggers_store` with a
  `flow` target mirroring `EventTriggerIn` (`api_schemas.py:157-164`), payload
  delivered as run `data` with the existing `idem_key` dedup.
- **A "Triggers" panel in the designer**: one place listing this flow's cron,
  webhooks, event sources, and form URL, with add/remove — backed by the
  existing three APIs, no new engine work.
- **Dedicated run viewer** (`/flows/{id}/runs/{run_id}`): timeline of nodes
  with status, duration, retries, per-node redacted input/output, cost per
  agent node, and approve/reject/retry inline. All data already persisted in
  `FlowRun.nodes`; this is a page, not a feature.

### Phase 2 — The chat copilot (the headline feature)
One conversational assistant docked in the designer, replacing both one-shot
drafters. Design constraints learned from the current code:

- **Edits are patches, not regenerations.** Define a small flow-patch
  vocabulary (`add_node`, `remove_node`, `set_field`, `rewire`, `wrap_in_
  foreach`, `add_trigger`, …) validated by `Flow.validate()` before apply.
  The LLM emits patches; the canvas animates them; every applied patch is a
  designer undo entry *and* rides the existing version history — so "undo
  what the AI did" is free via `rollback_flow`. Full-graph drafting
  (`draft.py`) remains the special case for an empty canvas.
- **Grounded context**: current graph, selected node, connector catalog
  slice (searched, not the whole 3k), inferred step schemas (Phase 4), and —
  crucially — recent run traces. That unlocks the three verbs PA's Copilot
  can't do: **explain** ("walk me through this flow"), **diagnose** ("why did
  run #41 fail?" → the failed node's trace is in the context), and **repair**
  ("fix it" → patch proposal + optional dry-run before apply).
- **Conversation state is per-flow and cheap**: persist turns alongside the
  flow; each turn is one budget-capped completion under the existing
  `DRAFT_MAX_DOLLARS` discipline.
- **Guardrails carry over**: patches that touch governed connectors or raise
  `max_dollars` surface an explicit confirm, reusing the approval affordance.
- Reuse `workflow_ai.py`'s plumbing conventions (role-resolved model,
  injectable `complete`, forgiving JSON parse, threadpool + rate limit).

### Phase 3 — Designer to the commercial bar
- Undo/redo (command stack — the patch vocabulary from Phase 2 doubles as
  the undo unit), copy/paste/duplicate, multi-select + marquee, edge
  delete/reconnect, insert-node-on-edge, minimap, zoom-to-fit, snapping,
  node search / quick-insert palette (`/`-style).
- Client-side structural validation with inline red badges (port of
  `Flow.validate()` rules to `flow_designer_core.js`, which is already the
  pure/unit-tested layer).
- Decision point: the current vanilla-SVG editor is ~700 lines and already
  correct on the hard math (zoom-aware hit testing, nested canvases). Budget
  one spike to compare "grow it" vs adopting a canvas library; do not rewrite
  by default — the dashboard is deliberately framework-free (FastAPI + htmx,
  no bundler), and a React Flow adoption drags in a build toolchain.

### Phase 4 — The data experience (kills the expression cliff)
- **Schema inference from runs**: after any test/dry run, record per-node
  output shapes into the flow's metadata (redacted sample + inferred keys).
- **Dynamic content picker**: in any brief/params/condition field, a picker
  listing upstream nodes' available keys (from inferred schemas + trigger
  payload fields, which importers already extract —
  `automation_import/ir.py:115-161`) that inserts `{{node_output.key}}`.
- **Structured condition builder**: dropdown field/op/value UI emitting the
  existing safe grammar (`_COND`), with and/or groups; raw-string mode stays
  as the advanced tab.
- **NL→expression in place**: a small "describe it" affordance on any
  expression field, routed through the Phase-2 copilot ("last name,
  uppercased" → `{{upper(last(name))}}`); the 16-function set grows only as
  this surfaces real demand (likely: date/time formatting, split/join, regex
  extract — all currently absent).

### Phase 5 — The "better than both" differentiators
- **Fix-it-from-failure**: on a failed run, one button — copilot reads the
  trace, proposes a patch, dry-runs it, then offers **resume from the failed
  node** (new engine capability; `FlowRun` already persists cursor + threaded
  data, so this is an execution-layer feature, not a storage one).
- **Self-evolution surfaced in the designer**: proposal badges on nodes
  ("this agent step succeeded 47/47 — harden to a deterministic action?"),
  with measured impact and one-click apply/revert (`/flows/{id}/proposals`,
  `/apply`, `/rollback` all exist; this is pure UI).
- **Migration with a fidelity report**: extend `to_flow.py` branch/loop
  capture beyond n8n — Power Automate first (WDL `If`/`Switch`/`Foreach`/
  `Until`/`Scope` map cleanly onto `branch`/`foreach`/`subflow`+`on_error`;
  the importer already toposorts `runAfter`), UiPath second. Emit a per-step
  report: preserved / approximated (→ `agent` node with the original step
  described in the brief — a trick PA cannot mirror) / dropped. "Import your
  Power Automate flow; anything we can't translate deterministically becomes
  an agent step that still works" is the migration headline.
- **Node vocabulary**: `switch`, `while`, error scopes, `wait_for_event`,
  richer human tasks (choices, form fields, expiry/escalation) — in that
  order, each demand-driven.
- **Flow gallery**: ship the importer + drafter output as curated starting
  templates; a gallery is also the cheapest onboarding for the copilot
  ("start from this, then tell the chat what to change").

## 7. Positioning summary

| | Power Automate | UiPath | Lightwork (after phases 1–5) |
|---|---|---|---|
| First flow | Template gallery + Copilot draft | Studio(X) learning curve | Chat builds it on the canvas, editable turn by turn |
| When the happy path ends | Expression cliff (WDL) | Code/Studio | Data pills → NL→expression → `agent` node escape hatch |
| A step fails | Re-run whole flow, read logs | Debugger in Studio | Copilot explains from the trace, patches, resumes from the failed node |
| Over time | Static flow | Static flow | Self-evolving: agent steps harden into deterministic actions, measured, reversible |
| Governance | DLP add-ons, licensing tiers | Orchestrator add-ons | Budgets, risk tiers, simulate-before-commit, signed approvals — built in |
| Migration | Lock-in | Lock-in | Imports from 7 platforms; untranslatable steps become working agent steps |

The one-sentence strategy: **keep the engine, unify the product, make chat a
patch-emitting copilot grounded in run traces, and spend the saved effort on
the data-picker and debugging UX — because those two are where Power Automate
actually loses its users.**
