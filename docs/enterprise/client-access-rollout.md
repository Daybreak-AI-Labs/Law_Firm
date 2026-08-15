# Client feature access & onboarding — how it works, end to end

> The operating loop this enables: **check a box for a client at HQ → the
> feature is live in their environment about a minute later → and the people
> at the client who need to log in got there via their company SSO or a
> single-use email invite link.** Every hop rides the primitives the platform
> already trusts: Ed25519-signed documents, offline verification, fail-open
> core.

This doc is the operator's map of the three pieces (feature control,
propagation, people onboarding) plus the distribution decision for
less-technical customers (desktop app). For the underlying signing/update
substrate see [product-operations.md](product-operations.md).

---

## 1. Controlling what each client has — checkboxes in the vendor console

The **vendor console** (`apps/vendor-console`, Daybreak-internal) is where a
customer's offering lives. The customer page renders a **feature-access
matrix** driven by the ONE canonical registry
(`maverick.entitlements.GATED_FEATURES` / `GATED_SUITES` — add a gated
capability there and the checkbox appears, nothing else to wire):

- **Tier** (Basic / Gold / Platinum) sets the baseline.
- **Checked below tier** → an à-la-carte grant in the license's `features`
  list (e.g. Gold + `advanced_evolve`).
- **Unchecked within tier** → an explicit entry in `features_denied`
  (e.g. Gold minus `siem_export`). Denial wins over tier and grant.
- Grants/denials only bind while the license is **paid-active** — an expired
  or invalid license still fails open to the base tier, so a denial can never
  outlive the license that made it (kernel rule 1 intact).

Saving the form **issues a new signed license** (the existing upsell path: the
newest non-revoked license is what the serve API returns). Upgrades AND
downgrades are therefore the same gesture: flip boxes, save.

Hosting the console on your own domain (sign in from anywhere, keep the
signing key off the internet): **Cloudflare Tunnel + Access**, documented in
[`deploy/cloudflare/`](https://github.com/Daybreak-AI-Labs/Lightwork/blob/main/deploy/cloudflare/README.md).

## 2. Near-instant propagation to client environments

Connected deployments now run a **background license refresher**
(`maverick.entitlements.start_refresher`, started by the dashboard lifespan):

- Polls `[license] api_url` every `refresh_interval_seconds` (default **60 s**
  once an API URL is configured; floor 30 s; `0` disables). Config knob +
  wizard step, per kernel rules 5–6.
- Reuses `refresh_from_server()` wholesale, so every safety property holds:
  verified-signature-only, **no rollback/subject-swap**, never raises, never
  overwrites a good local file with a bad server reply, and the on-disk
  license stays the offline floor for the next boot.
- Every poll records a **fleet check-in**, so the console's fleet board is
  near-live for free.
- No dashboard? `python -m maverick.entitlements refresh` is the one-shot
  (cron / systemd-timer) equivalent.

Postures: **connected** = the loop above (a checkbox lands in ~60 s).
**Change-controlled VPC** = same, or the cron one-shot on their schedule.
**Air-gapped** = unchanged: download the signed license file from the console
and hand-deliver; nothing here weakened the offline floor.

(Release/binary updates are a different lane — signed release manifests +
channels, `release_update.plan_upgrade` — see product-operations §2. This doc
is about *entitlement* flips, which need no new binaries.)

## 3. Getting client people logged in

Two customer shapes, one goal: the client admin adds a colleague in seconds.

**SSO shops (Azure AD/Entra, Okta, Google, AWS-fronted):** already built —
built-in OIDC browser login (`[auth.oidc]`), SAML, reverse-proxy SSO, and SCIM
provisioning. What was missing was ergonomics: an admin had to hand-type
`user:<sub>` principals into the RBAC roster. Now: **invite links**.

**Invite links** (`[dashboard] invites = true`, default off, fail-closed):

- An admin on `/users` enters an email + role → gets a **single-use, 7-day,
  revocable link** (only its hash is stored; the link is shown once — copy it
  into an email or chat).
- **With SSO configured:** the invitee opens the link → signs in with their
  IdP → accepting binds their verified principal to the invited role. No
  principal strings, no IdP admin round-trip.
- **Without any IdP (the "not that advanced" customer):** accepting the link
  **is** the sign-in — a signed `mvk_session` cookie for `user:<email>`
  (30 days, `[dashboard] invite_session_days`), minted with a locally-held
  secret (`~/.maverick/dashboard-session.key`, or `[dashboard]
  session_secret`). The link is the credential — same trust shape as a
  password-reset email. Re-invite to renew or use "log out everywhere" to
  revoke (revocation epochs apply to these sessions exactly as to SSO ones).
- Scanner-proof (GET shows a confirm page; only the same-origin-gated POST
  consumes), single-use under a cross-process lock, and `[dashboard]
  require_auth = true` turns the deployment fail-closed: only invite sessions
  (or SSO/token/proxy) get in.

**Lightwork sends the email itself** when a sending account is configured:
the platform mailer (`maverick.mailer`, stdlib SMTP) reuses the same
`[email]` / `EMAIL_USER` + `EMAIL_APP_PASSWORD` account the email tool and
wizard already know, so there is exactly one sending identity to set up.
Minting an invite then emails the link automatically (fail-soft: a broken
SMTP account just falls back to the copyable link; `[dashboard]
invite_email = false` opts out; `MAVERICK_EMAIL_DISABLE=1` is the global
kill switch). Not built yet, deliberately: a self-serve "email me a new
link" page (re-invite covers it).

## 4. The desktop question — "maybe it downloads as an app and that's how we do everything"

Recommendation: **the desktop app is a shell, not a fork — and it can't be the
only door.** The pieces already exist: `apps/desktop` (Tauri window that
spawns/attaches to the local dashboard) and `apps/installer-desktop`
(one-button installer, no Python required first).

- **Solo / tiny team:** installer-desktop → everything local, no accounts at
  all. This is the honest single-user mode.
- **Small team, no IT:** ONE machine (or small VPS) runs the dashboard;
  teammates get **invite links** (§3). They can use the browser, or the same
  desktop app pointed at the team server — the app is then just a nicer
  window onto the shared dashboard, and the session cookie works identically.
  "Send them the app" alone can't work as the sharing model: each laptop
  running its own instance means no shared world model, no shared audit
  trail, and per-laptop licenses — the server (wherever it runs) stays the
  unit, the app is how you look at it.
- **Enterprise:** their platform team deploys (helm/VPS/container lanes in
  `deploy/`), SSO + SCIM own identity, invites still useful for the first
  admins. Desktop app optional cosmetics.
- Desktop **updates** ride the existing signed-release machinery: vendor
  console publishes a signed manifest per channel; the Tauri updater is the
  `install` hook `release_update.apply_bundle` was designed for (still the
  gap to close — see product-operations §2 "to build next").

## Config quick-reference (client side)

```toml
[license]                                   # feature access (this doc §1–2)
enforce = true
api_url = "https://console.daybreak.example/api/v1/license"
api_token = "${MAVERICK_LICENSE_API_TOKEN}"
refresh_interval_seconds = 60

[dashboard]                                 # people onboarding (§3)
invites = true
require_auth = true                         # fail-closed team server
# invite_session_days = 30
# session_secret = "..."                    # else auto-generated, 0600

[auth.oidc]                                 # SSO shops: see docs/configuration
# enabled/client_id/issuer/session_secret…
```
