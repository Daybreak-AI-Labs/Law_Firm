# Lightwork — Live Demo Runbook (the 3-minute governed-agent demo)

> The demo *is* the pitch. One command, no model in the loop, fully reproducible —
> you show governance the audience can verify themselves. Nothing is cherry-picked
> because there's nothing to cherry-pick: every verdict is the real enforcement code.
>
> **The ethos:** we don't *say* it's governed — we show it, and we hand them the
> means to check. Lead with that. It's the whole differentiator.

---

## Before the call (2 minutes of setup)

- [ ] One terminal, **large font** (18pt+), clean prompt, dark theme. Nothing else on screen.
- [ ] `maverick` on PATH (installed) **or** a source checkout you can run from.
- [ ] **Dry-run it once** and clear the screen — warms caches, confirms the machine is clean.
- [ ] Have `maverick/golden_path.py` open in a second tab (for the "is this a mock?" question).
- [ ] Know your numbers cold: **$60k wire → DENY · $6k → human · $4k → allow · loop → capped · tamper → caught.**

Offline-safe: **no API key, no network, no provider needed.** Run it on a plane.

---

## The demo — one command (≈90 seconds)

**Open (say this, ~10s):**
> "Every agent vendor says *trust us, it's safe*. I'm going to **show** you governance
> you can verify yourself — one command, no model in the loop, so nothing's staged."

**Run:**
```bash
python -m maverick.golden_path -o ./gp
```

**Walk the table row by row — don't rush the two that matter (DENY, CAUGHT):**

| # | What you say |
|---|---|
| 1 | "The finance specialist **boots sealed** — it *physically* can't open a shell or move money, even though its parent could. Least privilege by construction, not a policy PDF." |
| 2 | "A vendor asks for a **$60,000 wire → DENIED.** That's the policy engine, not the model deciding to be nice." |
| 3 | "A **$6,000 release → REQUIRE_HUMAN.** The dollar-tier authority gate — it lands where a person signs off." |
| 4 | "A **$4,000 release → ALLOWED.** Routine work flows. Governance isn't a handbrake." |
| 5 | "A **runaway loop → CAPPED** at a hard budget ceiling. It can't burn your account." |
| 6 | "An auditor **alters one amount → CAUGHT.** The signed hash-chain breaks. You can't quietly edit history." |

---

## The mic-drop — they verify it themselves (≈30 seconds)

The demo **prints the exact command.** Run it in front of them:

```bash
cd gp
maverick audit verify --file audit.ndjson --pubkey <the-hex-it-printed>
# → OK: chain intact
# → OK: tip-ledger intact
```

**Say:**
> "That public key is the *only* thing a third party needs. Your auditor verifies this
> **offline** — never touches our systems, never trusts our word. That's the product:
> governance you can prove, not governance you're asked to believe."

---

## The takeaway (land it in one breath)

> "Enforced on every action — not prompted. On a signed record. Running on **your**
> infrastructure, so a prompt injection can't even move data out. That's the agent
> platform a regulated buyer can actually deploy."

---

## Handling the two questions you'll always get

- **"Is this real or a mock?"** → "No model is called — every verdict is the real
  enforcement code the production runtime calls. Here's the source." *(flip to
  `golden_path.py`; it's ~180 lines, no smoke.)*
- **"What about accuracy / benchmarks / ROI?"** → "We deliberately don't quote a
  fabricated number. The proof pack reports what it can prove and refuses to invent
  the rest — that refusal *is* the trust posture. ROI comes from your pilot, on your
  data." *(This honesty reads as strength to a technical buyer. Lean in.)*

---

## Optional second act — "and it improves itself, safely" (≈60 seconds)

Only if they're leaning in and you have time:

```bash
python proof/self_harness_proof.py    # 7 guarantees, incl. determinism: 6/6 runs identical
```

> "It gets better on your data — but every learned change is proven before it ships,
> bounded, and reversible in one step. **Deterministic**: same situation, same change,
> every time. It compounds *without* drifting. It can't quietly get worse."

---

## Do-not (footguns that break the spell)

- **Don't** run `audit verify` without `--pubkey` on an old build — it prints red
  `FAIL: no_pubkey`. (Fixed in current build; the printed command now includes the key.)
- **Don't** promise a capability benchmark or a customer ROI figure. Off-thesis, and
  it's the one thing we never fake. "We show what we can prove" is the line.
- **Don't** wander into architecture. One command, the two verdicts that matter, the
  offline verify. Stop. Let *them* ask the next question.

---

## The 20-second version (when the clock is gone)

Run `python -m maverick.golden_path`, point at **DENY** and **CAUGHT**, run the
**verify** command. "Enforced, signed, self-hosted, and you just checked it yourself."
That's the whole story.
