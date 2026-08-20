# Encryption at rest

Maverick keeps its state under `~/.maverick`. Secure defaults seal sensitive
application state with AES-256-GCM. Filesystem permissions and full-disk
encryption remain defense-in-depth; they are not substitutes for application
encryption when the system handles client data.

## Enable it

**On by default** (secure-by-default). The key auto-generates on first use, so
new writes are sealed with no configuration — see
[Back up the key](#key-management) immediately, because losing it loses the data.

Resolution (first match wins): a compliance floor (e.g. HIPAA) forces it on >
`MAVERICK_ENCRYPT_AT_REST` env > `[encryption] at_rest` in config > enterprise
mode > the secure-by-default switch (on). To **disable** it on a personal box:

```toml
[encryption]
at_rest = false
```

- env: `MAVERICK_ENCRYPT_AT_REST=0`
- or turn off the whole secure-by-default posture with
  `MAVERICK_SECURE_DEFAULT=0` / `[security] secure_defaults = false`
  (see the security and compliance overview (`docs/security-hardening.md`)).

Existing installs must run `maverick encryption migrate` before firm mode is
enabled. Strict reads withhold legacy plaintext instead of returning it. The
offline migration seals existing database fields, renames legacy attachment
ciphertext files to opaque content-addressed pathnames, checkpoints the WAL, and rebuilds the
database to remove recoverable plaintext residue.

## What gets sealed

| Store | Location | Field(s) |
|---|---|---|
| Conversation turns | world DB | `turns.content`, `matter_turns.content` |
| Persisted facts | world DB | `facts.value` |
| Per-goal agent message log | world DB | `messages.content` |
| Clarifying questions | world DB | `questions.question`, `questions.answer` |
| Goal content | world DB | `goals.title`, `goals.description`, `goals.result` |
| Deliverable artifacts | world DB | `artifacts.title`, `artifacts.content`; a goal-scoped HMAC title digest leaks equality only within one goal so versions remain groupable |
| Attorney feedback | world DB | `goal_feedback.note` |
| Per-agent goal events | world DB | `goal_events.content` |
| Episode summaries | world DB | `episodes.summary`, `episodes.outcome` |
| Parked approvals | world DB | `approvals.action`, `approvals.scope`, `approvals.detail` |
| Attachments | `~/.maverick/attachments/**`, world DB | whole files; `attachments.filename`, `attachments.path` |
| File-backed matter knowledge | knowledge SQLite DB | chunk text, vectors, metadata |
| Dashboard-managed credentials | settings TOML | provider API keys, webhook signing secret |

Sealing is transparent for migrated values: application code receives plaintext
only after authenticated decryption. In strict firm mode, an unsealed legacy
value is treated as an integrity failure and withheld until the offline migration
has completed.

## Key management

Key resolution (first match wins):

1. `MAVERICK_ENCRYPTION_KEY` — a 32-byte key as hex or base64, e.g. injected from a
   KMS / secrets manager so it never touches disk.
2. `~/.maverick/keys/at_rest.key` — generated on first use, `chmod 600` inside a
   `chmod 700` directory.

**Fail-closed:** if encryption is enabled but the `cryptography` backend or the key is
unavailable, a write *errors* rather than silently storing plaintext.

**Back up the key — losing it loses the data.** The key file is the only way to
read data sealed under it; if it is lost, that data is unrecoverable. At-rest is
on by default, so the key auto-generates on first use (with a one-time warning).
Escrow it immediately into a secrets manager / offline vault: copy
`~/.maverick/keys/at_rest.key` (and the other keyring keys under
`~/.maverick/keys/`) to the escrow location, preserving the `0600` mode.

Store the copies at least as well-protected as the originals, and not next to the
data they unlock. Operators who inject `MAVERICK_ENCRYPTION_KEY` already hold the
key in their secrets manager and need no on-disk backup.

## Search trade-off

`messages.content` is full-text indexed (SQLite FTS5). Under encryption the index
holds ciphertext, so a plaintext query can't match it — **full-text search over
encrypted messages is disabled** (messages written before encryption stay searchable).
The `facts` substring search (`search_facts`) likewise can't match encrypted *values*;
key matches still work.

## What is *not* sealed (and why)

- **The audit log** (`~/.maverick/audit/*.ndjson`) — integrity comes from the Ed25519
  hash-chain plus the erase/retention tooling. **Closed** day-files can be sealed at
  rest with `maverick audit seal`; the **current** day-file stays plaintext for the
  live append + signing path, so there is a confidentiality window on today's file
  until it rolls and is sealed. Secrets in audit payloads are redacted before write
  regardless.
  Semantic knowledge uses the retained local SQLite store and local embedding
  path; the firm build has no remote vector-store backend. Metadata never carries
  sensitive `title`/`result` outside the sealed world database.
- **Most of `config.toml` / `.env`** — only provider API keys and the dashboard
  webhook signing secret written through the settings store are application-sealed.
  Environment variables and other configuration values are not. Protect both
  files with filesystem permissions, a credential manager, and full-disk encryption.

Attachments are stricter than legacy database rows: durable plaintext attachment
files are rejected rather than read. Existing installations must migrate those
files before they can be downloaded or parsed. Decryption occurs only for a bounded
authenticated read or a short-lived private parser input, which is removed after
the call.

## Verify

The platform's compliance control map reports the at-rest control under
**GDPR Art. 32**.
