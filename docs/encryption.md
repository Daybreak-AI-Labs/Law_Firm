# Encryption at rest

Maverick keeps its state under `~/.maverick`. By default that state is plaintext on
disk — fine for a personal agent, but a GDPR Art. 32 / HIPAA exposure once the agent
handles sensitive data. **At-rest encryption** seals the sensitive stores with
AES-256-GCM.

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

Existing installs are safe to leave on: reads are plaintext-tolerant, so rows
written before it was enabled are returned unchanged until rewritten — and are
sealed as they are rewritten.

## What gets sealed

| Store | Location | Field(s) |
|---|---|---|
| Cross-session memory | `~/.maverick/memory/**` | whole files |
| Channel conversation turns | world DB | `turns.content` |
| Persisted facts | world DB | `facts.value` |
| Per-goal agent message log | world DB | `messages.content` |
| Clarifying questions | world DB | `questions.question`, `questions.answer` |
| Goal content | world DB | `goals.title`, `goals.description`, `goals.result` |
| Per-agent goal events | world DB | `goal_events.content` |
| Episode summaries | world DB | `episodes.summary`, `episodes.outcome` |
| Parked approvals | world DB | `approvals.action`, `approvals.scope`, `approvals.detail` |

Sealing is transparent — values are encrypted on write and decrypted on read, so
application behaviour is unchanged. A value written **before** encryption was enabled
carries no seal marker and is read back as-is, so enabling encryption is a gradual
migration, not a flag-day re-encrypt.

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
  The **qdrant**/**weaviate** backends embed server-side, so the sealed path isn't
  wired for them yet: under at-rest the semantic path is **disabled** for those two
  (it falls back to lexical recall over the sealed world DB rather than ship them
  plaintext). With at-rest off, behaviour is unchanged. Metadata never carries the
  sensitive `title`/`result` on any backend (hydrated from the sealed DB by
  `goal_id`).
- **Attachments** (`~/.maverick/attachments/**`) — on-disk uploaded files; only the
  metadata row lives in the DB.
- **`config.toml` / `.env`** — configuration and API keys; `.env` is already
  `chmod 600`. Protect these with filesystem permissions / full-disk encryption.

## Verify

The platform's compliance control map reports the at-rest control under
**GDPR Art. 32**.
