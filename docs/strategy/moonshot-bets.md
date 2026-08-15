# Moonshot Bets — the "$50M, buy-don't-build" theses

Working doc. Each bet is a capability a model vendor (Anthropic / Google /
Microsoft) is *structurally disqualified* from building themselves — because it
requires vendor neutrality — and that becomes a standard or a data-gravity moat.
Grounded in modules that already exist in this repo, so each is a build-path, not
vaporware.

---

## Bet 1 — The Trust Layer: third-party-verifiable behavioral attestation

**Thesis.** We are not an agent company; we are the neutral, cryptographically
provable *control plane* above the agents. The three buyers are walled gardens
and cannot credibly be the Switzerland that governs an enterprise running Claude
*and* GPT *and* Gemini agents at once. That neutrality is the one moat they can't
clone — they must buy it.

**The one feature.** Promote `proof/run_proof.py` + the Operating Record capsule
into a **portable, regulator/insurer/acquirer-grade proof bundle** that a party
trusting *neither the operator nor the model vendor* can verify with one command.
It proves three things no one else can prove today:

1. Every action stayed inside a declared policy envelope (capability + governance,
   enforced at `agent._run_tool`).
2. **The self-improvement loop never escaped that envelope** — the agent got
   smarter without ever granting itself new authority. The unique claim. Already
   *enforced* (`maverick-evolve/adopt.py` whitelist, `calibration.py` freeze);
   now *proved*.
3. The decision history is authentic and reproducible from the signed trajectory
   (`audit/signing.py` chain + cross-file anchors).

**Bleeding edge.** Make the attestation work *without revealing the underlying
data*: TEE-attested (`confidential_compute.py` already detects SEV-SNP/TDX) plus
zero-knowledge-style proofs of policy compliance. A bank proves "every agent
action satisfied policy P" without exposing the trades. Does not exist anywhere.

**Why $50M pre-ARR.** Standard capture (be the Sigstore/SBOM of agents) →
buy-the-standard-and-team. Makes agentic AI *insurable* (the underwriting
substrate). Regulatory tailwind = free distribution (EU AI Act Art. 12/15,
SR 11-7, AI-liability wave; artifacts already scaffolded in `ai_act_package.py`,
`dpia.py`, `ropa.py`).

**Per-buyer wow.** Anthropic: a verifiable claim that self-improvement stays
bounded is their north star. Microsoft: Copilot's enterprise blocker is
governance/system-of-record; we are it, cross-vendor. Google: Vertex/Agentforce
can't offer neutral cross-vendor attestation; acquiring is the only path.

**Build path (~1 quarter to demo).**
1. `run_proof.py` → continuously-emitted attestation bundle signed into the
   Operating Record capsule (~60% there).
2. Add the "self-improvement stayed in-envelope" proof by chaining
   `adopt.py`/`calibration.py`/`hindsight.py` evidence into the signed bundle.
3. Ship a standalone verifier (`maverick attest verify capsule.mvk`) needing zero
   access to our infra or the model vendor — the "hand a regulator a USB stick"
   demo.
4. Moonshot: TEE-attested + ZK policy proof for the confidential case.

**Centerpiece demo.** Regulated firm runs a swarm, hands an outside auditor a
sealed capsule, auditor runs one command, it cryptographically confirms *what the
AI did, what it learned, and that it never gave itself more power than it was
granted* — without ever seeing the data.

### Build log — Bet 1 steps two and three (shipped)

Steps 2 and 3 are built. `maverick attest` produces a bundle that binds the
three claims to evidence, and `maverick attest export-verifier` writes out the
checker as a standalone program.

**What the capsule could not do.** `operating_record.verify_capsule` checks the
signature using the public key stored *inside the capsule*. That proves nobody
edited the file since it was signed, and nothing whatever about who signed it —
anyone can mint a keypair and sign anything. It is integrity, not provenance,
and provenance is the entire ask of a party who trusts neither the operator nor
the model vendor. So the new verifier **requires the publisher's key out of
band** (`maverick attest key`) and fails closed without it; a re-signed bundle
embeds the attacker's own key, which will not match.

**What made claim 2 real.** The self-improvement controller's capability gate
already refused any change that widened authority — but the durable
`PromotionRecord` never recorded *what the gate concluded*. From the ledger file
alone, "proven bounded" and "never checked" were indistinguishable, so the
bundle could only have asserted the claim. Receipts now carry
`capability_evidence`: `probed_bounded` (the capability algebra was walked over
N tools, N recorded), `declared_bounded` (the caller asserted it), or `unproven`.
That distinction is the claim; collapsing it would let a bare assertion read as
a walked probe. The field is additive and conditionally emitted, so pre-existing
receipts re-serialize byte-identically and their hash-chained journal entries
still verify — and a verifier reads an absent grading as **unknown**, never as
bounded, because a receipt written before anyone recorded the verdict cannot
retroactively establish it.

**The discipline, inherited from Bet 5's adapters.** A claim is made only when
the bundle holds what substantiates it. Each claim reports `HOLDS` / `FAILS` /
`INDETERMINATE` / `NOT_APPLICABLE`, and the last two are never softened into the
first. Three places that matters most:

- **An empty governance policy forbids nothing**, so "no action violated the
  envelope" is vacuously true. Reported `NOT_APPLICABLE` — a green badge there
  would be the most misleading thing the tool could print, and Lightwork's
  community default is an open policy.
- **A sealed-at-rest day-file** is `INDETERMINATE`. The content digest still
  pins the bytes exactly, but nobody outside the tenant can walk the chain
  inside it, and handing an auditor the at-rest key to prove a point would
  defeat sealing it. "The publisher verified it in-house" is precisely the
  assurance this tool exists to replace.
- **A promotion that cannot show authority stayed bounded** makes the whole
  claim `INDETERMINATE`, naming the receipts.

**Two verification depths**, so the report never overstates what was checked.
*Signed*: authentic, with claims as the issuer recorded them. *Corroborated*
(`--evidence <audit-dir>`): the action list is re-walked from the chain and the
receipts re-read from the ledger. Corroboration **unions** the issuer's
disclosures rather than replacing them — a sealed or absent day cannot be
re-walked, so a re-derived list can be shorter, and replacing would let the
deeper check quietly forgive a violation the issuer had actually admitted to.
The one limit stated plainly in the report: risk levels are the issuer's signed,
inspectable classification, not independently derived, because the standalone
verifier deliberately cannot call our code.

**Flaws the build surfaced, mostly by pointing the tool at itself.** The export
writes its own audit row, so a bundle committing to *today's* day-file was
committing to a moving target and self-invalidated one row later. Open days are
now committed as a **prefix** — the first N rows and the tip at that point — so
later activity is expected while an edit to the attested rows still fails. That
prefix also catches the same-uid re-sign the audit-signing warning calls out: an
actor holding the key can rewrite history into a chain that verifies cleanly on
its own terms, but cannot reproduce a tip already committed to. A decryption
failure was forfeiting the content digest, which is the one commitment an
outside auditor can always compute; digest and chain tip are now gathered
independently. A self-review pass then caught three more, all the same shape —
a report saying more or less than the evidence warrants:

- The history check walked chains with the key **embedded** in the bundle rather
  than the trusted anchor, so a bundle carrying a signature but no `pubkey`
  field would have reported every row as unverifiable. A false accusation
  against an honest customer is as bad a failure as a false pass.
- An open day committed with **zero rows** attests to nothing beyond the file's
  existence, but counted toward the re-verified tally.
- The builder **skipped sealed days when listing actions**. The issuer holds the
  at-rest key, so that made its own disclosure incomplete — and since the
  verifier cannot re-walk a sealed day either, an action hidden in one would
  have been invisible at both depths. Full disclosure by the issuer, partial
  corroboration by the auditor, is the honest split.

71 deterministic tests for the bundle plus 14 for the receipt grading, all
network-free, and every guard mutation-checked — 23 mutants, 23 caught,
including "no anchor required", "a missing grading counts as bounded", "an empty
envelope passes", "corroboration replaces the issuer's list", and "history walks
with the embedded key". The standalone verifier is pinned two ways: a test
asserts it imports nothing from maverick, and its chain walk is cross-checked
against the shipped `signing.verify_chain` over clean and every tampered
fixture, so a drift in either shows up as a test failure rather than as a false
accusation against a customer.

Remaining for the full bet: step 4 (TEE-attested + ZK policy proofs) and
off-host key custody, which is what turns tamper-evidence against an outside
editor into tamper-evidence against the operator.

---

## Bet 2 — The Institutional Memory: a vendor-neutral plane that compounds and ports

**Thesis.** Bet 1 proves the *past* was safe. Bet 2 compounds the accumulating
*future*. The deepest enterprise moat isn't the model — models commoditize — it's
the firm's accumulated operational judgment. The firm that accumulates that
judgment in a governed, portable memory can't be cheaply displaced — and the
judgment is *theirs*: isolated to their boundary, owned, and exportable. Build the
**neutral, governed, compounding system of record for everything the customer's
own agents learn**, regardless of model vendor. The longer it runs, the more
valuable and bespoke *their* instance becomes — never pooled with another
customer's, never training ours, never leaving their walls. This is the literal
answer to "hit the milestone and the ARR will come" — stickiness earned through
customer-owned value, not data we harvest.

**The one feature.** Make institutional memory **cross-vendor, compounding, and
provably improving — inside a single customer's isolated boundary.** Within one
customer's deployment, every agent interaction — a Claude swarm, a Copilot agent,
an Agentforce flow — deposits and retrieves reusable, department-scoped,
capability-bounded knowledge through ONE governed plane; one customer's memory
never reaches another's. We already have the
substrate: `fleet_memory.py` (external Agentforce/Copilot/custom agents ingest +
recall through a fail-closed governed surface), `operating_record.py` (signed
portable capsule of the firm's decisions + learned state), `dreaming.py`
(consolidation), `hindsight.py` (proof it's improving), `semantic_recall.py`,
learned skills, reflexions. Today they're internal; productize them as **the
memory API beneath all of an enterprise's own agents.**

Three properties make it a moat, not a feature:
1. **Cross-vendor.** A Copilot agent benefits from a lesson a Claude agent learned
   last week — through `fleet_memory` over MCP. We are the substrate; the model
   vendors become interchangeable front-ends.
2. **Compounding & provable.** Cost-per-task drops with use (the cold-vs-warm
   `benchmarks/moat.py` curve), and we can *prove* it's getting better
   (`hindsight.py`) without it reward-hacking (`calibration.py` freeze). The
   Operating Record becomes a portable, appreciating asset — judgment on the
   balance sheet.
3. **Governed & private.** Department bulkheads (`compartment`), per-user notes
   never cross channel/user boundaries, scope tagging, shield-scan-on-ingest
   (RAG-poisoning defense), per-tenant KMS. Memory that's safe to **share across a
   customer's own agents and vendors — without one tenant's data ever reaching
   another's** is the hard part, and we already enforce it (learned stores resolve
   under each tenant's data dir; one tenant's memory never feeds another's runs).

**Why $50M pre-ARR.** A firm with two years of accumulated, department-scoped,
provably-improving judgment has something it can't quickly rebuild elsewhere — and
it *owns* that judgment as a signed, vendor-neutral, portable capsule. That earned
value — not lock-in over data we hold — is what makes the platform sticky.
Vendor-neutral = the same Switzerland logic as Bet 1 (buyers can't build it
neutrally). Microsoft/Anthropic/Google would pay to make their agent the front-end
to the customer's *own* governed memory rather than cede the neutral substrate to a
rival.

**Per-buyer wow.** Microsoft: Copilot becomes the UI on top of an enterprise's
*own* institutional memory — the thing Copilot conspicuously lacks. Anthropic:
Claude agents that visibly compound inside each customer's own isolated instance
(their agentic deployment story gets a flywheel). Google: the cross-vendor memory
plane Vertex can't offer because it's a walled garden.

**Build path (~1 quarter to demo).**
1. Harden `fleet_memory.py` into a stable public **Memory API** (ingest/recall/
   attest) over MCP + REST, with governance + provenance on every read/write
   (mostly there).
2. Ship the **cross-vendor proof demo**: connect a non-Claude agent (Copilot/
   LangChain) and a Claude swarm to one memory plane; show the second agent using
   the first's lesson.
3. Wire the **compounding dashboard**: live cold-vs-warm cost curve per department
   (`benchmarks/moat.py` + `role_stats.py` + `hindsight.py`), exportable as a
   signed Operating Record capsule (ties back to Bet 1).
4. Moonshot: a "memory portability" standard — the firm's judgment exports as a
   signed, vendor-neutral capsule it owns and can carry between platforms. Owning
   that format is owning the category.

**Centerpiece demo.** Within one customer's deployment, two agents from two
different model vendors share one governed, isolated memory plane. Agent B solves
in 30 seconds and $0.02 a task that cost Agent A ten minutes and $2 last week —
because the customer's own fleet already taught it that lesson. Then export the
whole thing as a signed capsule the customer owns. "Your AI workforce gets smarter
every day on your data, across every vendor you run — and it never leaves your
walls. You can prove it, and take it with you."

---

### Build log — Bet 2 step three: the attest half (shipped)

`fleet_memory` already did ingest/recall. `maverick memory-plane` adds **attest**,
and it rides the *same signed bundle spine as Bet 1* — one implementation of the
crypto, the out-of-band trust anchor, and the grading vocabulary. That is this
doc's own thesis about the capsule format made literal rather than restated.

Three claims, graded the way Bet 5 taught us to grade:

- **cross_vendor** is the moat, and the easiest thing here to oversell. A plane
  with one vendor connected has demonstrated nothing, so it grades
  `NOT_APPLICABLE`, not a pass — selling the one property the model vendors
  structurally cannot copy on the strength of an empty room would be the worst
  thing this bundle could do. Registered-but-never-contributed grades the same
  way. An ingest that cannot be attributed to a `vendor:agent_id` **fails** the
  claim outright: a lesson of unknown origin is precisely what a governed plane
  exists to prevent.
- **compounding** is the un-fakeable proof, now measured *per department*
  ("your finance swarm got 40% cheaper" is the sentence a buyer acts on). Below
  the run floor it is `INDETERMINATE`, because a short curve is noise. A plane
  that is running and has not paid off grades an honest `FAILS` — the customer
  is told rather than shown a green badge.
- **tenant_isolation** is deliberately ungraded. Every record in a bundle sits
  under that tenant's root because that is the only place we looked; calling
  that a proof would be a tautology in a signed wrapper. The tenant binding is
  published as evidence (as a digest, not a path) and the claim says plainly
  that isolation is established by comparing two tenants' bundles.

**A real bug the work surfaced, and it was in the substrate, not the new code.**
`fleet_memory._audit` forwards its payload into
`audit.record(kind, *, agent, goal_id, **payload)`. Ingest passed the record's
success/failure/lesson class as `kind=`, which collided with the event kind,
raised `TypeError` inside the audit call, and was swallowed by a bare
`except Exception: pass`. **Every fleet-memory ingest has been going unaudited**
— the governed plane's *write* path, the higher-trust operation, left no signed
trace while reads logged fine. Reserved keys are now re-prefixed rather than
dropped, and an audit failure logs instead of vanishing: a hole in the trail
that nobody can see is worse than no trail.

That fix also changed where the attestation reads its evidence. Activity counts
come from the signed chain rather than the inbox directory `status()` globs,
because `ingest` routes a *lesson* into the reflexion store and only
success/failure into the inbox — an inbox count would miss exactly the
cross-vendor lessons the plane exists for, and could report "no records
deposited" while a Copilot agent had deposited five hundred.

Remaining for the full bet: the live cross-vendor demo needs a non-Claude agent
actually wired in, and the memory-portability standard (step 4).

---

## Bet 3 — Safe Recursive Self-Improvement: the alignment-frontier "wow"

**Thesis.** The most important — and most feared — capability in AI is an agent
that improves *itself*. Every frontier lab wants it and is terrified to ship it
ungoverned, because ungoverned self-improvement reward-hacks, escapes its
envelope, and can't be rolled back. Lightwork is the only platform that already
ships a *bounded, auditable, reversible* self-improvement loop. Productize that
into the credentialed path to recursive self-improvement: **agents that
provably get better without ever escaping their capability envelope.** This is
the bet that makes *Anthropic specifically* go wow — it's their deepest research
interest, de-risked and made shippable.

**The one feature.** Turn the existing loop into a demonstrable, certified
**"safe self-improvement" engine** and then climb one rung past everyone else.
Pieces already in the repo: `maverick-evolve` (config-only evolution; code
mutation deliberately deferred), `adopt.py` (adoption can never widen a
capability scope — `ADOPTABLE_KEYS` excludes `allow_*`/`max_risk`),
`calibration.py` (freezes learning the moment the verifier drifts —
anti-reward-hacking interlock), `hindsight.py` (proves it improved, or regressed),
the signed learning audit (`dreaming._audit_cycle`, snapshot + rollback). Three
properties that make it singular:

1. **Bounded.** Self-improvement runs inside a capability envelope it can never
   widen — proven, not asserted (`adopt.py` + capability attenuation).
2. **Non-reward-hacking.** The calibration interlock freezes evolution when the
   judge stops discriminating, so the system can't learn to game its own grader.
3. **Reversible + audited.** Every learning cycle is snapshotted, rollback-able,
   and written to the signed audit chain — you can undo what the AI taught
   itself and prove what changed.

**Bleeding edge.** Ship the rung the labs deferred: **bounded code
self-modification** — the agent proposes changes to its *own* tools/policies, but
only inside an out-of-process sandbox, under capability bounds, with
human-gated promotion (the Darwin-Gödel step `maverick-evolve` explicitly
parked). Done *with* the governance rails, not without them. That is the frontier
capability every lab wants and no one dares ship raw.

**Why $50M pre-ARR.** This is the safety-credentialed path to RSI. Whoever owns a
*demonstrably bounded* self-improvement loop owns the most valuable and most
dangerous capability in AI, done in the one way regulators and boards will
tolerate. It is also un-buildable in a hurry: the moat is the *interlocks*
(calibration freeze, capability-never-widens, signed rollback), which took this
codebase years of safety-first design to assemble correctly.

**Per-buyer wow.** Anthropic: bounded RSI is their north-star research problem,
arriving pre-governed and demoable. Google/Microsoft: a self-improving workforce
they can put in front of a board without the liability — the thing their own legal
teams won't let them ship ungoverned.

**Build path (~1 quarter to demo).**
1. Wire `maverick-evolve` continuous loop → live "improvement certificate":
   each round emits a signed proof that capabilities never widened and
   calibration never unfroze (chains into the Bet 1 capsule).
2. Dashboard the cold-vs-warm improvement curve per department with the rollback
   button visible — "undo what it learned" is the trust unlock.
3. Moonshot: the sandboxed, human-gated code-self-mod rung, with every proposal
   diffed, capability-checked, and promotion-gated.

**Centerpiece demo.** An agent system measurably rewrites its own playbooks over a
week and gets cheaper and more reliable — then you show, cryptographically, that
it never granted itself a single new permission and never gamed its grader, and
you roll one bad lesson back with one click. "Self-improving AI you can actually
sleep next to."

---

## Bet 4 — The Agent Security Plane: "CrowdStrike for AI agents"

**Thesis.** Bet 1 *proves* compliance after the fact; Bet 4 *actively defends* in
real time. The number-one reason enterprises won't deploy autonomous agents is
fear of compromise: prompt injection, tool abuse, data exfiltration, poisoned
RAG, malicious MCP servers and plugins, runaway swarms. Lightwork already contains
the most complete agent-runtime defense stack in existence — but it's buried as
internal plumbing. Surface it as a standalone, vendor-neutral **runtime security
product** for agent fleets: detection, containment, and continuous adversarial
evaluation. This bet also *diversifies the acquirer pool* beyond the three —
Microsoft Security/Defender, Google Mandiant/Chronicle, CrowdStrike, Palo Alto.

**The one feature.** A live **agent detection-and-response (agent-EDR) plane**
that watches every agent — yours or any vendor's, via MCP — and detects, contains,
and proves compromise in flight. Pieces already in the repo: the shield (3
chokepoints, ~35 de-obfuscating rules, cross-family lockstep-jailbreak defense),
capability attenuation + revocation, `honeytokens.py` + `canaries.py` (exfil/
escape tripwires), `quarantine.py`/compartments (seal a compromised agent
mid-run, withhold its output from the swarm), `leak_quarantine.py`, the SSRF
guards, Shield-scanning of untrusted MCP/plugin schemas, `threat_hunt.py` over the
audit trail, `ebpf_monitor.py`, and the offensive side — the red-team corpus +
calibration runner + `capability_leak_fuzzer`. Productize as:

1. **Detect.** Continuous adversarial evaluation (red-team corpus as a live
   regression gate) + runtime injection/exfil detection across the fleet.
2. **Respond.** Mid-run containment — quarantine-seal a compromised agent, revoke
   its capability subtree, black-hole its egress — without killing the swarm.
3. **Prove.** Every detection + response written to the signed audit chain (the
   forensic record insurers and IR teams need; ties to Bet 1).

**Bleeding edge.** Make it the neutral "agent-EDR telemetry standard" — any
vendor's agent emits Lightwork-format security telemetry over MCP, and the plane
scores/contains across all of them. Own the format, own the category.

**Why $50M pre-ARR.** Security is the highest-willingness-to-pay budget in the
enterprise, with its own buyer and its own acquirer set — so this bet is both a
moat and a hedge: if the model vendors don't move, the security platforms will.
And the assets (a coherent, tested, fail-closed agent-defense stack with offense +
defense + forensics) cannot be assembled quickly; it took this codebase's
safety-first posture to build correctly.

**Per-buyer wow.** Microsoft: Defender for AI agents, cross-vendor, day one.
Google: Mandiant/Chronicle gain an agent-runtime sensor + IR capability. Anthropic:
a runtime that can *prove* it defeated an attack class (cross-family verifier vs
lockstep jailbreak) — a verifiable security claim.

**Build path (~1 quarter to demo).**
1. Expose the defense stack as a **security telemetry + control API** over MCP
   (detections, seals, revocations) — fleet-wide, vendor-neutral.
2. Ship the **containment demo**: inject a compromised tool/MCP server, watch the
   honeytoken trip, the agent get sealed mid-run, its capability subtree revoked,
   and the whole incident land in the signed audit trail.
3. Wrap the red-team corpus + capability fuzzer as a **continuous adversarial
   eval** product (CI gate + scheduled fleet scans).

**Centerpiece demo.** A malicious MCP server tries to exfiltrate secrets through a
compromised agent. The honeytoken trips, the agent is quarantine-sealed mid-run,
its output is withheld from the swarm, its capability subtree is revoked, egress is
black-holed — and the analyst gets a signed forensic timeline. "Your agents get
attacked. Ours fight back and prove it — no matter whose model they run."

---

### Build log — the moat spine (shipped)

The council's verdict: none of the four bets is a moat on its own; the moat is
**safe, governed, deployable self-improvement** — the interlocks that make
self-modification shippable into a regulated buyer. Step one of that is built:

- **`maverick.self_improvement`** — the Self-Improvement Controller: a governed
  promotion ladder (`config → prompt → tool → policy → evaluator → code →
  weights`; the `evaluator` rung swaps the learned judge — see
  `maverick.evaluator_evolution`). A
  proposed self-change is promoted only if it (a) beats its own baseline by a
  margin with enough evidence, (b) **never widens the capability envelope**
  (declared, or proven via a before/after grant probe), (c) is human-approved at
  `code`/`weights` and above the `max_auto_rung` ceiling, (d) is reversible, and
  (e) is refused while `calibration.learning_frozen()` (the verifier-drift
  interlock). Every promotion is signed into the audit chain
  (`EventKind.LEARNING_UPDATE`) and recorded in a reversible ledger. OFF by
  default, fail-open while off, fail-**closed** when deciding. 20 deterministic
  tests, ruff/vulture clean. Config: `[self_improvement]` (`config.get_self_improvement`).

This is the spine every rung hangs on. Remaining work to reach *real* (not
config-only) self-improvement, in order — each rung plugs into the controller as
an opaque candidate payload and inherits the gates above:

1. **Phase 0 capture** — governed raw-trajectory store; wire `prm.py` into the
   agent loop (today it's observability-only); auto-collect calibration samples;
   live cold→warm compounding metric.
2. **Phase 1 judgment** — train the verifier/PRM (`training/prm_train.py`) on
   labeled outcomes.
3. **Phase 2 policy** — stand up real `training/rlaif.py` (per-tenant LoRA/DPO).
4. **Phase 3 action space** — close the loop on `self_learning.write_generated_tool`
   (measure/promote/retire by outcome).
5. **Phase 4 strategy** — expand `maverick-evolve` beyond 5 config knobs to
   prompts/playbooks/policies.
6. **Phase 5 code** — sandboxed, human-gated code self-modification (Darwin-Gödel).
7. **Phase 6 weights** — periodic per-tenant fine-tune on accumulated trajectories.

Phases 2/5/6 require GPUs / real model training / safety review and cannot be
validated in a keyless CI sandbox — they land behind the controller's gates as
the deployment matures.

### Build log — update 2 (all phases wired to the controller)

Every phase now flows through the merged controller's gates. Status:

- **Phase 0 capture — BUILT & TESTED.** `maverick.trajectory_store` (governed,
  per-tenant, secret-redacted, consent-gated raw-trajectory store, off by
  default); `maverick.prm_guidance` + a default-off `agent.py` hook that lets the
  process-reward model *steer* the loop (it was observe-only); and
  `maverick.compounding_metric` — the live cold→warm cost/quality signal (the
  un-fakeable moat proof).
- **Phase 3 action space — BUILT & TESTED.** `si_producers.ToolOutcomeTracker`
  measures whether a synthesized tool actually helps; `propose_tool` promotes it
  only when its success rate beats baseline and it doesn't widen capability.
- **Phase 4 strategy — BUILT & TESTED.** `propose_prompt`/`propose_policy` route
  prompt/playbook/policy changes through the gate.
- **Phases 1, 2, 6 — pipeline + seam BUILT & TESTED; training is the seam.**
  `propose_verifier` (adopt a retrained head only if it discriminates better),
  `propose_policy` (an RL/DPO adapter), `propose_weights` (a fine-tuned
  checkpoint, human-gated). The governance/adoption path is real and tested; the
  GPU training that *produces* the artifact is an injected callable — never
  faked — and lands when a GPU/model is available.
- **Phase 5 code self-mod — safe pipeline BUILT & TESTED; generation gated.**
  `propose_code` runs an out-of-process `validate` seam *before* the gate, which
  then forces human approval + non-escalation + reversibility. The diff
  *generation* stays behind a hard flag + human gate.

~67 new deterministic tests across the tranche; ruff + vulture clean; full core
suite collects (8,289 tests, no errors). Everything off by default.

### Build log — update 3 (model-agnostic completion + the OS-model decision)

**Decision (consistent with prior guidance and the council): no open-weights
base model.** The default reasoning brain stays a frontier closed model and is
swappable per role (kernel rule 2: `ROLE_MODELS` defaults are last-resort,
overridable across 13 providers within the admin allow-list). The moat is
governance + per-customer compounding *on top of* the best model — not owning
one. So real self-improvement is the **model-agnostic** rungs; weight-level
fine-tuning (Phases 2/6) is demoted to an *optional, sovereign-/air-gap-only*
seam, never the default and never the strategy.

Model-agnostic completion glue shipped (`self_improvement_runner.py`,
`trajectory_store` wired into `agent._score_step`, `maverick compounding` CLI):

- **Capture is live** — the agent now writes governed, redacted trajectory steps
  (off by default).
- **Judgment** — `build_prm_examples` turns trajectories into training rows for
  the small reward *head* (an MLP, not an LLM — no open-weights model implied).
- **Tools** — `review_generated_tools` promotes a synthesized tool that earns it
  and retires one that doesn't (the FORGET half).
- **Strategy** — `emit_strategy_candidate` routes prompt/skill/policy changes
  through the gate.
- **Calibration** — `collect_calibration` arms the verifier-drift interlock from
  any ground-truth source.
- **Proof** — `maverick compounding` reports the live cold→warm cost/reliability
  delta per task class.

What remains for full *training* completion is infra/business, not code: a real
workload (design partner) for the eval signal + data, GPU/compute, and a raw-text
capture consent decision — the same four moves the council said convert the
platform into a $50M asset. The deterministic half of every rung is now built,
tested (~130 self-improvement tests total), and off by default.

### Current audited DGM posture (2026-07-15)

The older update-4/update-5 notes overstated the production posture and are
superseded by this section. The stock `[self_modify]` path is a **research-only
code-evolution harness**. It can ground a proposal in a bounded source snapshot,
review a unified diff through a fail-closed reference monitor, evaluate baseline
and candidate in separately bound no-egress/non-root sandboxes, and persist
bounded lineage telemetry. It does not apply or promote code.

The production runner deliberately refuses `apply=True`, inline approval,
persistent archive-parent branching, host-visible or fixed-root evaluation, an
empty/non-discriminating challenge corpus, and any non-code rung. The challenge
tests and their argv are visible to candidate code, so they are a development
corpus—not a sealed holdout and never deployment authorization. Archive records
are research evidence, not promotion receipts.

The reference monitor protects the control plane and repository trust roots,
normalizes paths according to the exact `git apply -p1` semantics, and allows
only a positive mutable zone. DLP checks bound proposal context, provider prompts,
diffs, feedback, and persisted lineage. HALT is checked at learning start and
again around expensive evaluation and durable writes. These controls reduce
risk; none is described as a proof that arbitrary self-modification is safe.

Production code adoption stays disabled until all of the following exist and
are independently exercised: an external one-shot evaluator whose hidden cases
cannot be observed or influenced by the candidate; a manifest binding exact
source/evaluator/container bytes, tenant, surface, expiry, nonce, and approval;
a cross-process PREPARE/CAS/COMMIT apply transaction with recovery; and adversarial
containment tests on the deployment backend. A human signature alone is not
sufficient. Until then, the defensible claim is “governed self-improvement
research,” not “recursive self-improvement running in production.”

### Build log — Bet 4 steps one and two: the agent-EDR plane (shipped)

The defense stack was all there and none of it was a *plane*: six modules with
six vocabularies, fine for the kernel and useless to an analyst. `maverick
security` is the surface — and it composes the existing enforcement rather than
re-implementing any of it, so a detection is only ever a claim about a row
somebody already signed.

- **Detect.** Every security row on the signed chain normalized into one
  `Detection` with an attack class and a severity **derived from the event**. A
  compromised agent gets no say in how loudly its own detection rings, and an
  audit kind the table does not model produces *no detection at all* — a plane
  that invents threats from rows it does not understand trains its operator to
  ignore it.
- **Respond.** `contain()` seals the agent mid-run and revokes its capability
  subtree in one act. The two are reported separately, and that asymmetry is the
  point: a compartment seal is **run-scoped and in-memory**, a revocation is
  **durable on disk**. Blurring them would tell an operator an agent is locked
  out when the seal evaporates at process exit. `contained` is True only when
  every requested action landed, and the revocation is confirmed by re-reading
  the registry rather than assumed — a partial containment reported as success
  is worse than a failed one, because everyone stops watching a live agent.
- **Prove.** Containment lands on the signed chain; `incident_report` renders
  the timeline.

**The rule that shapes the whole module:** every command reports the deployment's
*posture* alongside its findings. An empty detection list from a deployment with
the shield off means "not watched", not "not attacked", and an unsigned chain is
flagged as not tamper-evident rather than presented as evidence. This is the same
discipline as Bet 1's empty-policy envelope: silence from a control that was
never on is not a compliance finding.

Remaining for the full bet: the telemetry *standard* — any vendor's agent
emitting Lightwork-format security events over MCP — and continuous adversarial
evaluation wired as a scheduled fleet scan rather than a CI gate.

---

## Bet 5 — Consequence-Proven Autonomy → "Earned Autonomy" (the breakthrough)

**Thesis.** The biggest blocker to enterprise agents isn't quality, it's **trust
to take irreversible action** (move money, change prod, file, send). Everyone
ships agents that draft/suggest; almost no one ships agents that *act*, because
the downside is catastrophic and unprovable. Bet 5 is the layer that makes
autonomous high-stakes action safe — and it's the synthesis of everything
Lightwork already has (sandbox, connectors' single egress chokepoint, verifier,
governance, audit chain, autonomy slider, the self-improvement controller), not
a fifth silo.

**The capability — the Consequence Engine.** Every high-stakes plan is run
through a preview + reversible-execution layer: (a) **dry-run** the irreversible
action types (the ~10 high-risk tools in `tool_risk`: wire_transfer,
post_journal_entry, run_payroll, deploy, send, file_*, ...) via the connector
chokepoint; (b) where dry-run is impossible, a **compensating-action** layer —
every action ships its inverse and executes inside a saga that rolls back on any
failure or human rejection; (c) emit a **signed consequence card** ("this would
move $240k, close these 3 tickets, change this config"); (d) gate real execution
on policy/human approval of the *simulated* outcome; (e) sign the whole
sim → approve → execute chain into the audit record.

**One engine, three moats:**
1. **Trust to act** — clients let agents *do* the work, not just suggest it
   (where the real dollars are).
2. **The safe RL environment** — you can't RL on real money movement; you *can*
   against dry-run/compensating execution. The shadow layer is the practice
   ground that makes high-stakes self-improvement possible. The
   **predicted-vs-actual gap is the training signal** that improves the predictor.
3. **Compounding trust** — every real outcome makes the predictor more faithful,
   which unlocks more autonomy, which generates more data.

**The 10x reframe — Earned Autonomy.** Trust is the product and it compounds. As
the consequence-predictor proves accurate (measured: predicted vs actual, per
action type, per customer), the system **progressively earns autonomy**: an
action type predicted correctly N times graduates from "human approves" to
"policy auto-approves." Not a scary binary switch — an **autonomy dial driven by
evidence**, wired to the existing `autonomy.py` slider + `calibration`. One line:
*"your agents earn the right to act, action type by action type, by proving they
predict consequences correctly, with a guaranteed undo until they have."*

**3-round council evolution (how it got here):**
- **R1 (attack):** a faithful full-system digital twin is research-grade; a wrong
  sim trusted is worse than no agent; per-tool what-if already exists; Musk: just
  build a perfect undo. → Drop "simulate everything"; do dry-run of the
  irreversible action types + a compensating-rollback saga (undo fused with
  preview).
- **R2 (moat):** novelty = a *uniform, cross-tool, governed, signed*
  consequence+rollback layer over every connector (per-tool what-if isn't that).
  Karpathy: it's also the safe RL environment; predicted-vs-actual is the
  learning signal. Underwriter: signed preview + guaranteed rollback = insurable.
  → MVP: "Shadow Mode for the ~10 irreversible action types."
- **R3 (10x):** trust compounds → Earned Autonomy: agents measurably graduate
  from human-approved to policy-auto-approved per action type. The autonomy dial
  is driven by proven prediction accuracy.

**Why the named buyers beg.** ServiceNow: their platform is execution; this is the
only safe way to turn their workflows autonomous — they can't ship it without
this layer. Clients: "show me what it'll do, let me approve, guarantee the undo,
and let it earn more trust over time" is a painkiller. Anthropic: a verifiable
safety story for autonomous action.

**Defensibility.** Requires governance + connectors + sandbox + verifier + audit +
the self-improvement loop + the autonomy slider — Lightwork has all of them; a
competitor must build the entire stack. The earned-autonomy ledger + the
per-customer consequence-predictor are non-portable. Multi-year moat.

**Honest critique (kept in view).** A faithful twin of arbitrary systems is hard —
so don't build one; dry-run only the irreversible action types through the
connector chokepoint and lean on the compensating-rollback saga for the
sim-to-real gap (the gap is itself a learnable signal). Start narrow (the
high-risk tool list), expand. And it still needs a design partner to be real.

**Karpathy on the self-improvement architecture (recorded):** approves the
*shape* — thin governed spine, model-agnostic verifier head, freeze-on-drift
anti-reward-hacking interlock, cheap rungs first, reversibility. Two caveats:
(1) it's "an empty gym" until real trajectories + a calibrated reward model run
through it — prove the verifier-head rung end-to-end on one real workload before
declaring victory; (2) make the calibrated verifier central (not optional) and
**decouple capture from PRM-enabled** (capture should be unconditional/cheap).
Bet 5's shadow layer is also his answer to "you can't RL high-stakes actions on
production" — it's the safe environment that makes the architecture trainable.

### Build log — Bet 5 step one (shipped)

The synthesis layer exists: **`maverick.earned_autonomy`** — consequence
cards, the predicted-vs-actual join, the evidence-driven autonomy dial, and
the compensating-action saga. What shipped:

- **Consequence cards.** Every rehearsed high-stakes action that PROCEEDS now
  pins its predicted outcome into a hash-chained, append-only, multi-writer
  card store *before* it runs (the agent loop captures the rehearsal verdict;
  PROCEED predictions used to vanish, and a held action is never graded by an
  outcome it didn't produce). A card is a commitment reality can grade.
- **The join.** `reconcile` scores unscored cards against real outcomes landing
  through the existing Consequence Engine (`consequence.resolve`, keyed
  `(goal_id, episode_id)`) — hit/miss per action type with per-agent
  provenance, same-episode duplicates collapsed (one observed outcome grades
  one prediction), idempotent and serialized across processes, event-sourced
  into a second hash-chained trust ledger.
- **Earned Autonomy dial.** A proven streak (default 10 consecutive accurate
  predictions, ≥90% overall) graduates an action type from "human approves" to
  "policy auto-approves" — implemented as a revocable standing consent-ledger
  grant, which the existing agent approval path already honours (zero new
  hot-path authority code). The trust unit is deliberately the ACTION TYPE,
  deployment-wide — the same unit the grant applies at — so minted authority
  is never wider than the evidence, and a miss by ANY agent demotes instantly;
  the revoke runs BEFORE the evidence write, is retried on every later miss,
  and a stale-grant sweep each reconcile re-runs any withdrawal that failed.
  Interlocks: refused while `calibration.learning_frozen()`, during a learning
  HALT, above the `max_auto_risk` ceiling (default `medium`; the action's risk
  is recomputed from `tool_risk`, never trusted from the card, and an unknown
  level ranks above every ceiling), and for action types whose cards don't
  declare an inverse. Arming is separate from enabling (`auto_graduate`,
  strict-parsed); disabling stops evidence/demotion but not grants already
  minted — `maverick earned-autonomy --revoke` is the incident path and works
  while disabled. Graduation never widens the capability envelope — it changes
  who approves an already-permitted action, never what is permitted.
- **Compensating-action saga.** `run_saga`: every step ships its undo, a step
  without one refuses to start (fail-closed before any effect), and a mid-saga
  failure rolls the completed prefix back in reverse, recording failed
  compensations rather than swallowing them.
- **Shadow Mode — the preview→approve→execute centerpiece.** `shadow_execute`
  composes the card + dial + saga into the actual product flow: a
  `ConsequencePreview` from a connector's `preview_write` (effect, exposure in
  dollars, touched entities, predicted outcome) is gated — **auto-approved
  when the action type has earned it, otherwise routed to the human callback**
  — then executed inside the compensating saga, signing the sim → approve →
  execute chain (`shadow_execution`). A consequence card is pinned ONLY when
  the effect commits, so a denied / refused / rolled-back action never becomes
  a prediction reality would mis-grade. The preview/gate/execute/sign flow is
  valuable standalone (not gated by the learning switch); the earned dial only
  changes *who approves*. End-to-end this is the compounding loop: a
  human-approved shadow execution is graded by reality, and a proven streak
  flips the same action type to auto next time.
- **Provable.** Cards, hits/misses, graduations, demotions, operator
  revocations, and shadow executions are signed audit events
  (`consequence_card`, `autonomy_graduation`, `shadow_execution`); both stores
  verify VALID/BROKEN from the file alone. `maverick earned-autonomy
  [--reconcile]` is the operator surface.

OFF by default (config knob + wizard step). 49 deterministic tests (the
tranche was adversarially reviewed — 60 agents, findings fixed: action-level
trust unit, same-episode collapse, cross-process chain safety, forced demote
rows, live-risk recomputation, stale-grant sweep); the engine's
grant/revoke/audit/clock are injected callables so the decision logic is
fully offline-testable. Remaining to reach the full bet: real per-connector
`preview_write` adapters that populate the exposure figure (the seam and the
`ConsequencePreview` shape are now in place), and a design partner for real
predicted-vs-actual volume.

### Build log — Bet 5 step two: the connector adapters (shipped)

Step one left Shadow Mode holding a *sentence*. `preview_write` returns "would
PATCH salesforce/… with fields ['Amount']" — no dollars, no entities, and no
`undo`, so every governed write was structurally irreversible and the autonomy
dial had nothing it could ever grade. **`maverick.connector_previews`** closes
that gap. `plan_write(conn, params)` turns one connector write into a
`WritePlan`: a populated `ConsequencePreview` plus the saga steps that can
execute *and compensate* it.

The whole module exists to enforce one rule: **an action is reversible only
when we are holding the thing that inverts it.** Never asserted, never
inferred from the verb alone.

- **What earns an undo.** A PATCH earns one by capturing the record's prior
  values for exactly the fields being changed — not the whole record, which
  would replay fields nobody touched. A POST earns one from the id the create
  response returns; because that id doesn't exist at plan time, the undo
  closes over the response and **raises** if the id never arrives (a silent
  no-op rollback is worse than a loud failure — the saga would report a clean
  rollback while the record still exists). But a POST is not automatically a
  create: the same verb drives RPC endpoints — Salesforce's
  `/actions/standard/emailSimple` sends an email that no DELETE recalls — so
  the id only earns an inverse when the path addresses a collection the
  dialect explicitly models. Appending an id to an unmodelled path is a guess
  at another vendor's URL grammar, and a guess that resolves to some *other*
  endpoint turns a compensation into an unrelated destructive write. A
  full-replace PUT and a DELETE earn nothing: replaying a prior record means
  writing system-managed fields the API refuses, and a recreated record has a
  new id and leaves every reference to the old one dangling. That is a
  different record, not a restoration. A write path carrying a **query
  string** earns nothing either, for any verb: an in-place undo re-sends that
  same path, and a query string is not inert — ServiceNow's
  `sysparm_input_display_value` makes the API read submitted values as display
  labels, so replaying a raw prior through it writes a *different* value than
  the one captured. Creates are not the exception they first look like: a
  create's inverse rests entirely on the id coming back in the response, and
  the parameters that reshape a write reshape what it answers with —
  `sysparm_fields` prunes the echo, `sysparm_display_value=all` re-types it. An
  inverse whose evidence may never arrive is not an inverse, and promising one
  costs more than refusing, because the undo would fail only *after* the record
  already exists. Which parameters are safe is per-vendor knowledge we do not
  have.
- **Evidence gets validated before it is trusted as an address.** The
  compensating DELETE is built by concatenating an id the *service* chose, so
  a response carrying traversal, a query string, or simply something the wrong
  shape would aim that DELETE at an endpoint nobody approved. The id must match
  the dialect's record-id grammar first; a dialect with no grammar earns no
  create inverse at all. The same reasoning anchors the path patterns
  themselves — an unanchored suffix match claims any prefix as this vendor's,
  and the collection match is what licenses the concatenation in the first
  place. (The grammar earned its keep immediately: it caught a 19-character
  "Salesforce id" that had been sitting in our own test fixtures, where real
  ids are 15 or 18.)
- **Three ways a capture is a lie, all handled.** *System-managed fields* —
  audit stamps, auto-numbers, Salesforce formulas (`ExpectedRevenue`) and
  roll-ups, ServiceNow `sys_*` — read back perfectly and then silently fail to
  apply, so a write touching one forfeits its inverse entirely rather than
  shipping a restore that reports success and changes nothing. *Concurrent
  edits* — the dialect's optimistic-concurrency token (`LastModifiedDate`,
  `sys_mod_count`) is captured alongside the priors, and it has to guard **two
  distinct windows**, not one. The window from capture to write contains an
  arbitrarily long human approval gate, so the do-step re-reads the token
  immediately *before* writing and aborts with **no effect at all** if the
  record moved while the card waited — quietly reverting a colleague later,
  with values that went stale in the meantime, is not an acceptable substitute
  for never writing. The window from write to undo is guarded by the same
  token re-read before restoring; if the record moved, the undo refuses.
  Restoring over somebody else's edit is not an undo, it is a second incident.
  A matching token is necessary but not sufficient, because the token can be
  too coarse to resolve the edit that matters: Salesforce stamps
  `LastModifiedDate` to the second — the API always answers `.000` — so a rep
  saving inside the same second as our own write carries a token identical to
  ours, and carries it forever however long the undo is deferred. The values
  are the finer signal and we already hold the ones our write left behind, so
  the undo compares them directly, over exactly the fields the restore would
  overwrite.
  The reference value is the token as of *our own* write, not the plan-time
  one — our write bumps the token itself, so comparing against the capture
  would flag every undo as a stranger's edit and quietly make the whole
  feature dead. The do-step therefore records what the write left behind,
  preferring the response echo (ServiceNow returns the updated record) and
  paying for one GET only where the write answers with a bare 204 (Salesforce
  PATCH). That fallback GET is a second round trip, so it can return a record
  somebody else has *already* moved on from — adopting their state as the
  undo's reference would license the undo to overwrite them. Only a readback
  still carrying the values we just wrote is adopted, compared field by field
  and tolerantly enough that a service answering `"7"` for an integer is not
  mistaken for an edit. Field level is the right granularity: a stranger
  touching some *other* field is no reason to refuse, because the undo only
  ever replays ours. That comparison spans two shapes of the same value, and
  gets the tolerance asymmetric on purpose. The capture GET is *narrowed* —
  raw, dereferenced, named fields — but a write's echo is whatever the service
  volunteers, so a ServiceNow reference field is bare from the read and
  `{"link": …, "value": …}` from the echo of the write that set it. Compared
  raw, the record would not appear to hold what we just wrote, the reference
  snapshot would be forfeited, and the undo the card advertised would refuse on
  every reference field — so the dialect normalizes the shapes, and an echo
  that *still* does not corroborate is treated as no echo at all and pays for
  the GET rather than silently giving the undo up. In the other direction the
  comparison refuses to be lossy: reading a value as a number discards currency
  codes and leading zeros, so it is licensed only when one side really is a
  number. `"USD;100"` and `"EUR;100"` are different values, and calling them
  equal would let the undo destroy an edit while reporting the field untouched.
  *Display values* — ServiceNow returns `"New"` for a
  choice field and a nested object for a reference unless the read demands raw
  values, and neither is writable back, so the capture GET pins
  `sysparm_display_value=false`.
- **The approved write is the executed write.** Everything the card shows —
  the priced exposure, the captured prior, the pinned digest — describes the
  params *as of planning time*. A caller that keeps its dict and mutates it
  while the card sits in front of a human would otherwise have the do-step
  send fields nobody approved, at an exposure nobody saw. `plan_write` takes a
  private snapshot and the do-step sends that. Planning also never raises on a
  hostile path: `urlsplit` throws on an unbalanced bracket in the authority,
  and the path comes from an agent, so a string that malformed resolves to the
  empty path — it addresses no record, which routes the write to a human
  instead of to a traceback.
- **Honest dollars.** Exposure is the *delta* where a prior was captured
  (moving an opportunity from $100k to $120k risks $20k, not $120k) and the
  whole written figure where it was not. Overstating exposure keeps a human in
  the loop; understating it would not. A DELETE is priced at everything on the
  record — which is why the capture read still happens for a verb that can
  never be undone.
- **Vendor knowledge lives in a dialect, not in the verb.** `RestDialect` +
  `SalesforceDialect` + `ServiceNowDialect` carry the URL grammar, the
  money-bearing fields, the unrestorable set, the token field, and the capture
  query. An unknown connector falls back to a deliberately *incapable* generic
  dialect: it matches no record path, captures nothing, and earns no
  reversibility — degrading to "route this to a human" rather than guessing at
  another vendor's grammar. The dialect is also where a false negative gets
  fixed: ServiceNow's PUT *merges* like PATCH rather than replacing, so
  treating every PUT as irreversible would stall the dial on writes that are
  genuinely restorable.
- **Two contract seams bridged.** `preview_write` keeps its no-network
  guarantee (a REST write cannot be dry-run server-side, and a test asserts
  the silence), so the single read-only GET that earns an undo lives in the
  separate `plan_write` seam, gated by `[governed_connectors] restore_points`
  — and `plan_write` still calls `preview_write` first, keeping validation and
  the human sentence in exactly one place. Second: `RestConnector` reports
  failure by *returning* an `ERROR:`-prefixed string, while `run_saga`
  compensates only when a step *raises*. Unbridged, a rejected write is booked
  as committed and never rolled back; `_checked()` converts one convention
  into the other.
- **A live safety hole closed on the way.** `tool_risk("salesforce")` was
  `high`, but the governed action that actually posts to the system of record
  is named `salesforce.write` — absent from the table, so it fell through to
  the `medium` default and slipped under the default `max_auto_risk="medium"`
  ceiling. A namespaced action now inherits its namespace's classification,
  **upward only**, so a suffix can raise the floor but never launder a
  high-risk connector down.

Every gap — a failed restore read, a field the read didn't return, a missing
token, a system-managed field, an unrecognized connector, a POST to a path
that isn't a modelled collection, a create response whose id isn't one, a query
string on the write path, an unparseable path, restore reads switched off —
fails closed to `undo=None`, which makes `run_saga` refuse **before any
effect**. Over-refusal costs a human review; under-refusal costs somebody's
edit, so every judgment call above is settled in that direction. `maverick
connectors plan` is the operator surface: it prints the consequence card, the
restore point, and every warning, and commits nothing. 120 deterministic tests,
all network-free (connector I/O is scripted and every read the adapter *would*
have issued is asserted), and each guard above is mutation-checked — flipped
off one at a time to confirm a test actually dies for it.

Remaining to reach the full bet: a design partner for real
predicted-vs-actual volume.

### How the four bets relate

| Bet | One-liner | Tense | Primary buyer pull |
|---|---|---|---|
| 1 — Trust Layer | prove the past was safe | past | regulated entry wedge; insurability |
| 2 — Institutional Memory | compound the customer's own accumulating judgment | future | customer-owned, portable judgment; earned stickiness |
| 3 — Safe Self-Improvement | the AI improves itself, safely | forward | alignment-frontier / Anthropic wow |
| 4 — Agent Security Plane | defend agents in real time | present | security budget; widens acquirer pool |

All four ride the **same signed Operating Record / capsule format** as connective
tissue — build that once and every bet attaches to it. They also share the one
moat the model vendors structurally can't clone: **neutrality.** Sequencing
instinct: Bet 1 is the wedge (gets you in the door), Bet 2 is the lock (makes them
stay), Bet 3 is the halo (makes the labs covet you), Bet 4 is the hedge (a second
buyer universe if the labs stall). Pick the wedge first; the capsule format is the
shared spine that keeps all four optionalities open.

