# Ekko work discovery

Ekko turns repeated, observable work into evidence-backed Lightwork drafts. A
typical candidate is “download a scheduled report, open PowerPoint, and assemble
the same management deck.” Ekko can identify that repeated sequence and propose
a first-pass flow and agent profile. It does not execute, save, schedule, or
activate either draft.

That detailed example requires a reviewed `guided` integration to supply the
semantic labels `download/report` and `create/presentation`. The built-in
Windows metadata observer deliberately knows only that the user moved between
allowlisted applications; it never guesses a document action from a process
name.

Ekko is a client-controlled sensor, so it is independent of Lightwork's
default-on governed learning. A client must complete all three gates:

1. set `[ekko] enable = true` (or `MAVERICK_EKKO=1`);
2. enroll a named owner and device with a positive application allowlist; and
3. start a reviewed collector explicitly.

An empty allowlist means observe nothing. The sensitive communication and
system-of-record blocklist is an immutable floor in this release and wins over
the allowlist. Pausing, stopping, revoking, or erasing is available without
provider access.

## What Ekko records

The default `application_metadata` level permits only normalized application
transitions, timestamps, and timing needed to reconstruct a sequence; its only
action is `switch` and its object type is always `none`. `guided` additionally
accepts semantic action and object labels supplied by a user or a reviewed
integration. Both modes reject raw screen pixels, window titles,
clipboard data, keystrokes, URLs, query strings, document contents, and form
values. Guided mode also rejects `email`, `message`, and system-of-record
`record` object labels even when a general-purpose browser is allowlisted; the
sensitive-data floor cannot be weakened by enrollment policy.

Raw events stay in the active tenant's local data boundary and expire after 14
days by default (hard maximum: 30 days). Opportunities are computed on demand,
not retained in a second candidate store. Provider egress is reserved and
unsupported in this release; it must remain false, and setting it true fails
closed rather than opening a network path.

## Configure and enroll

Run `maverick init` in advanced mode, or add:

```toml
[ekko]
enable = true
retention_days = 14
enrollment_days = 30
min_occurrences = 3
min_distinct_days = 2
poll_interval_seconds = 5
capture_level = "application_metadata"
allowed_apps = ["excel", "powerpoint", "chrome"]
blocked_apps = ["email", "outlook", "gmail", "chat", "teams", "slack",
                "crm", "salesforce", "erp", "sap", "database"]
provider_egress = false
```

Then use the lifecycle commands:

```text
maverick ekko enroll --device DEVICE --apps excel,powerpoint --days 30
maverick ekko status --device DEVICE
# Explicit Windows application-transition observer (no titles or content):
maverick ekko run --device DEVICE --observer windows
# Or a reviewed semantic integration in guided capture mode:
maverick ekko run --device DEVICE --events reviewed-events.jsonl
maverick ekko pause --device DEVICE
maverick ekko resume --device DEVICE
maverick ekko stop --device DEVICE
maverick ekko discover --device DEVICE
maverick ekko erase --device DEVICE --yes
maverick ekko forget --device DEVICE --yes
```

`run` is foreground by design and never chooses an observer on its own. On
Windows, `--observer windows` records only transitions among a fixed mapping of
allowlisted executable identities; it does not read window titles, UI trees,
screens, clipboard, keyboard, URLs, or document content. Windows supplies the
foreground process image path transiently; Ekko immediately reduces it to the
basename for fixed-map lookup and never emits or persists the path. The
`--events` form consumes an explicitly supplied guided semantic stream. Neither
form starts at login or requests operating-system permissions on its own.

Each runner holds a single opaque collector capability in process memory. The
store retains only its one-way digest and accepts an observation only while the
matching exclusive lease and heartbeat are live. A second collector cannot
attach to the same owner/device, a crashed runner becomes `stale`, and
pause/stop/halt changes the lease authority in the same transaction as the
session state. Dashboard `running` therefore means “window authorized”; only a
`live` collector means the endpoint is actually observing approved metadata.

The ordinary CLI binds its owner scope to the current operating-system security
principal; it does not accept an owner-impersonation argument. Authenticated
dashboard deployments use the verified dashboard principal instead.

Application names in policy and guided events are canonical IDs such as
`excel`, `powerpoint`, and `chrome`, not executable names. A reviewed platform
adapter maps `EXCEL.EXE`, a macOS bundle ID, or an equivalent platform identity
to that fixed vocabulary. The bundled Windows adapter has a fixed executable
map. Unknown identities are dropped, never persisted verbatim.

## Production collector boundary

An enterprise may run the non-detaching runner under a client-managed per-user
supervisor so it operates in the background. Review and code-sign the pinned
artifact, deploy the command with the client's endpoint-management system,
grant the narrow OS permission there, and run it as the logged-in user—not
SYSTEM or an administrator—with outbound network denied. A session-0 Windows
Service is the wrong host for foreground-window discovery because it cannot
observe the signed-in user's desktop. The current release has no
provider-analysis egress path.

Lightwork does not install a Windows service, launch agent, systemd unit, login
item, or accessibility/screen-recording permission from `maverick init`. This
keeps policy consent distinct from endpoint privilege. A production rollout
should also surface an always-visible recording indicator and bind pause/stop to
the local user session. Lightwork intentionally does not self-detach or install
autostart: the client owns that deployment decision, visible indicator, restart
policy, and content-free log collection.

Authenticated encryption detects modified policy, session, lease, and event
records. Separate consent and per-session control-authority markers prevent a
database-only replay from reviving revoked enrollment or restoring a collector
snapshot from before pause, stop, release, revoke, or erase. Like any purely
local application, Ekko cannot prove freshness against an administrator who
rolls back the complete data tree, including both the database and authority
files. Deployments whose threat model includes full-host rollback must anchor
the consent and session-control generations in an external control plane or
TPM/OS-backed monotonic authority before enabling production capture.

## Draft review boundary

Discovery requires repeated evidence across the configured number of days. A
candidate contains frequency, confidence, estimated manual time, and risk
signals. `discover` produces reviewable data only. Saving an agent profile,
saving a flow, activating a schedule, or running a side effect remains a
separate authenticated human action through the normal Agent Factory and Flow
governance paths.
