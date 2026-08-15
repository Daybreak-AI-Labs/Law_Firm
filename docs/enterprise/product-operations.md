# Product operations — updates, support, and entitlements

> How Lightwork is **run as a product** across customers of every size and
> regulatory posture — including **air-gapped** — without ever phoning home or
> shipping source. Three capabilities × three connectivity tiers, all riding
> the same primitive you already own: **Ed25519 signing + offline verification**.

The organizing constraint: a self-hosted product **cannot assume internet**. A
bank's box may sit in a locked-down VPC or a fully air-gapped enclave. So every
capability below has a *connected* path **and** an *offline* path. The elegant
part — your update, support, and licensing machinery is itself **signed,
offline-verifiable, and auditable**, which is exactly what a regulated buyer
wants from a vendor touching their environment. "We auto-update and phone home"
is what bank security *hates*; "signed, offline-verifiable, you approve and
audit every touch" is a selling point.

| Capability | Connected (VPC w/ egress) | Change-controlled VPC | Air-gapped |
|---|---|---|---|
| **Entitlements** | signed license + poll an API | signed license file | **signed license file, hand-delivered** |
| **Updates** | signed feed → auto-pull | signed artifacts, ops pulls on schedule | **signed offline bundle, out-of-band** |
| **Support** | opt-in OTel/metrics + bundle | redacted bundle | **redacted bundle, customer-carried** |

---

## 1. Entitlements — control features per customer, flip tiers remotely

**Built:** [`maverick/entitlements.py`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/packages/maverick-core/maverick/entitlements.py)
(+ `tests/test_entitlements.py`, 10 tests).

A **license** is an Ed25519-signed JSON doc declaring `customer`, `tier`
(basic / gold / platinum), add-on `suites` (e.g. `fleet`), explicit `features`,
`seats`, and `expires_at`. It verifies **offline** against the publisher key set
(`MAVERICK_LICENSE_PUBKEYS` / embedded-in-build), and a single gate
(`entitlements.current().allows(feature)` / `.suite_enabled()` / `.tier_at_least()`)
is consulted at every paid-feature chokepoint.

- **Upsell flow** — "buy platform now, add Gold or Fleet in 6 months": issue a
  new signed license with the added entitlements → the customer drops it in →
  it lights up. **No redeploy, no network.** Air-gapped included.
- **Fail-open core, fail-closed add-ons** (kernel rule 1): a missing/expired/
  invalid license never stops the core kernel — it warns and runs the base
  tier. Only paid add-ons gate off. A **grace window** keeps a just-expired
  license fully live so renewal is never a fire drill.
- **Trust anchor** — paid features activate *only* under a license signed by a
  trusted publisher key; a self-signed license grants nothing.

Issue/verify with the self-contained CLI:
```bash
python -m maverick.entitlements keygen                     # publisher keypair (once; keep priv secret)
python -m maverick.entitlements issue --customer "Cedar Valley Bank" \
    --tier gold --suites fleet --expires 2027-01-01 --key @publisher.key -o license.json
python -m maverick.entitlements show --file license.json --pubkey <pub>
```

**Built (this PR):** the `[license]` config knob (`enforce` / `publisher_pubkeys`
/ `license_file` / `api_url` / `api_token`), the **opt-in enforcement gate**
(`enforcing()` / `require()` / `require_suite()`, **default off** so no dev/test
box changes behavior), a connected **entitlement API** client
(`refresh_from_server()` — fail-open, only persists a *verified, non-rollback*
license so a spoofed/replayed API reply can't forge or downgrade entitlements),
the **installer-wizard step** (`pick_license()` writes `[license]`; the API token
is a `${MAVERICK_LICENSE_API_TOKEN}` env reference, never inline), and **all six
gated capabilities wired to fail-open chokepoints**:

| Feature | Tier | Chokepoint |
|---|---|---|
| `fleet_memory` | Gold | `fleet_memory.enabled()` |
| `fleet_governance` | Gold | `fleet.governance_enabled()` (CLI `fleet run` + dashboard run) |
| `siem_export` | Gold | `audit.forwarder.forward()` |
| `advanced_evolve` | Platinum | `self_improvement.enabled()` (promotion ladder) |
| `custom_pack_factory` | Platinum | `intake.save_profile()` (persist a new custom pack) |
| `multi_tenant` | Platinum | `paths.tenant_by_user_enabled()` + `tenant.registry.create_tenant()` |

Every gate is a **no-op unless enforcement is on** (a dev/community box is
byte-identical) and matches the canonical tier table in `docs/product-portfolio.md`.

**Built (feature-access drop):** per-feature **checkbox control** in the vendor
console (à-la-carte `features` grants below tier + explicit `features_denied`
within tier — denial wins, and neither outlives a paid-active license), and the
**background auto-refresher** (`start_refresher` / `[license]
refresh_interval_seconds`, default 60 s once `api_url` is set; `python -m
maverick.entitlements refresh` is the cron one-shot) so a console checkbox is
live in a connected deployment ~a minute later. Operator's guide:
[client-access-rollout.md](client-access-rollout.md).

**To build next:** usage metering + seat enforcement on the connected API; embed
Daybreak's publisher pubkey in the build (`_EMBEDDED_PUBKEYS`).

## 2. Updates — every size, every regulatory posture

**Built:** [`maverick/release_update.py`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/packages/maverick-core/maverick/release_update.py)
(+ `tests/test_release_update.py`, 10 tests).

A **release manifest** is an Ed25519-signed doc (same trust anchor) listing the
`version`, `artifacts` (name + sha256 + size), the `min_from` version, and the
ordered **governed migrations** a release introduces. The decision layer is
built:
- `verify_manifest()` — authenticate a release.
- `verify_bundle()` — verify an **offline update bundle**: the manifest
  signature **and** every artifact's sha256 (a swapped binary is caught even if
  the manifest is authentic).
- `plan_upgrade()` — block downgrades, no-ops, and **version gaps** (below
  `min_from` → step through an intermediate so migrations apply in order),
  returning the migrations to run.

The governed migrations a manifest names are meant to resolve to the machinery
you already have — **immutable, checksum-locked migrations** (`migration_governance`
+ `schema_migrations --ci` gate online/offline hot-deploy safety) — plus
**Sigstore** signing on release binaries. Today `apply_bundle` runs each
migration through the injected `migrate(id)` callback (a deployment wires it to
`schema_migrations.plan()` / the online-vs-offline hot-deploy gate); it does not
call that machinery itself yet — the linkage is the caller's, and making it
automatic is in "to build next."

- **Connected:** a signed release feed + channels (stable/edge); desktop =
  Tauri auto-update; cloud = container tag bump + rolling restart.
- **Change-controlled VPC:** publish signed artifacts + release/migration notes;
  ops pulls on their schedule; `plan_upgrade()` + governed migrations make it
  auditable.
- **Air-gapped:** hand a signed bundle out-of-band → `verify_bundle()` offline
  → apply under change control.

**Built (this PR):** the offline verify + plan + **apply** + **rollback** CLI —
`python -m maverick.release_update {verify-bundle,plan,apply,rollback}`.
`apply_bundle()` owns the governed order (**verify → plan → [snapshot] → migrate
→ install**) and is deployment-agnostic by injection: the caller passes
`migrate(id)`, `install(dir, manifest)`, and optional `snapshot()`/`rollback(dir,
manifest, handle)` callbacks (systemd, container tag, Tauri updater, workspace
snapshot), so the risky swap only runs after signature + hash + version-gap
checks pass. When a `snapshot`/`rollback` pair is supplied, a failing migration
or install **auto-rolls-back** to the checkpoint (`ApplyResult.rolled_back`); a
standalone `rollback_bundle()` + the `rollback` CLI (wired to
`workspace_snapshot.restore_snapshot`) covers the "caught a bad release after it
applied" case. Both hooks default to `None`, so an existing no-hook apply is
byte-identical. **To build next:** the concrete per-target `migrate`/`install`
hooks (the actual binary/container swap, blue-green); the release-feed publisher;
channel config.

## 3. Support — see what broke, fix it, push the fix

**Built:** [`maverick/support_bundle.py`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/packages/maverick-core/maverick/support_bundle.py)
extended with a `correlation_id` (keys a ticket) and the deployment's
`entitlement` (so a ticket is tied to the exact SKU) — on top of the existing
**redacted** bundle (versions, runtime, readiness, providers, recent failures,
secret-scrubbed config).

- **Air-gapped / regulated (the floor):** the customer runs `maverick support`,
  gets a redacted, **customer-controlled** bundle (they see and approve what's
  sent), and delivers it out-of-band. No silent telemetry — which is what would
  break the "your data never leaves" promise.
- **Connected + opt-in:** OpenTelemetry / Prometheus / Sentry (already optional
  extras) for governed metrics/errors — no customer data, opt-in per posture.
- **Governance angle (a selling point):** every support action — a bundle
  export, a remote diagnostic — should land on the **signed audit trail**,
  redacted and customer-approved. Tell a CISO: *"our support literally cannot
  siphon your data — it's on your audit log and you approve it."*

**Built (this PR):** `support_bundle.export()` writes the redacted, customer-
controlled bundle to a file (keyed by `correlation_id`); `ticket_summary()`
derives a compact, secret-free intake view (SKU, build, failing readiness checks,
recent failure modes) from an already-redacted bundle — the fields an intake
queue routes on, safe to POST to a portal/email→ticket bridge; and **`maverick
support -o` now emits a redacted audit event** (`support_bundle_exported` —
`correlation_id` / tier / customer / basename, never bundle contents) on the
signed audit trail, so a CISO sees every support touch on their own log
(`collect()` stays pure; the emit lives in the CLI wrapper and is fail-soft).
**To build next:** the portal/email→ticket bridge that consumes
`ticket_summary()`; the round-trip to a hotfix release (feeds §2).

---

## The through-line

Entitlements, updates, and support all reduce to **signed, offline-verifiable
documents against one publisher trust anchor** — the same Ed25519 substrate as
the proof pack and the audit chain. That means your *operations* are as
governed and provable as the product, which is a differentiator with exactly
your buyer. Ship the publisher keypair management carefully (it's the root of
trust for licenses **and** updates): generate offline, store in an HSM/KMS,
rotate with an overlap window, and ship the public key(s) embedded in the build.

### Known limitations / accepted risks

- **No hardware/deployment binding on a license.** A validly-signed license
  grants its entitlements on *any* host, and `seats` is declared but not
  enforced in code — inherent to the air-gapped, offline-verifiable design (a
  license must verify with no network or callback). A leaked license file yields
  its entitlements until it expires. Mitigations if this becomes material: bind a
  license to a deployment/install id, or enforce `seats` via a signed count +
  local registration. The connected refresh path additionally **refuses a
  rollback** (an older or different-customer license replayed over the installed
  one is rejected on freshness/subject), closing the on-path downgrade vector.
- **Migration governance linkage is caller-wired** (see §2): `apply_bundle`
  gates on signature + hash + version, then delegates each migration to the
  injected `migrate` hook; wiring that hook to `schema_migrations` is the
  deployment's responsibility until the automatic linkage ships.
