# Security

This platform runs AI agents that read files, execute commands in a sandbox, and call
external APIs — against client matter files. The threat model is not abstract: a
compromise here is a confidentiality breach with bar consequences, not just an outage.

This is a private repository for one firm. There is no external vulnerability
disclosure program and no security advisory process; if you are not staff, you should
not be reading this.

## The rules that actually matter

**1. Privileged content and model providers.** Anything an agent sends to a model
provider leaves the building. Until the confidentiality gate is built (see the README
roadmap), assume every matter file handed to an agent reaches whichever provider is
configured. Decide the posture — zero-retention terms, a local model for sensitive
matters, or both — before real client data goes in.

**2. Conflicts and compartments.** Each matter runs in its own compartment. That
isolation is what keeps one client's context out of another client's draft, which is a
conflicts problem as much as a technical one. Do not disable compartment isolation to
make something convenient.

**3. The audit log is evidence.** The log is hash-chained and signed; `maverick audit
verify` checks it. It exists so that "what did the system do on this matter" has an
answer if a client, an opposing party, or the bar ever asks. Do not disable it, and do
not prune it to save space without an explicit retention decision.

**4. Nothing self-approves.** Every legal seat's deliverable carries a review or
approval gate and names a human consumer. A drafting agent cannot reach a
state-mutating tool — `maverick domains-audit` proves this across all 125 packs. If you
add a pack, it inherits that floor; do not widen an envelope to unblock a task.

**5. Shell goes through the sandbox.** All command execution routes through
`sandbox.exec()`. CI greps for `shell=True` outside the sandbox backends and fails the
build. This is the chokepoint that applies the configured backend and the
secret-scrubbed environment.

**6. Secrets live in the vault.** API keys and credentials belong in the sealed vault
(`{{secret('NAME')}}` in flows), not in config files, not in goal text, and not in a
matter note. The repository is scanned for committed secrets on every push.

## If something goes wrong

1. Stop the workforce — the kill switch halts every running agent.
2. Preserve the audit log before anything else; it is the record of what happened.
3. Rotate any credential the agent could have touched.
4. Determine whether client confidential information was exposed. If it was, the
   analysis is a legal one — Virginia Rule 1.6 and the applicable breach-notification
   statutes — not merely a technical postmortem.

## Hardening

See `docs/security-hardening.md` for the opt-in controls: egress locks, encryption at
rest, RBAC, capability tokens, per-tool ACLs, and consent gates. `maverick safety`
prints the current posture — shield status, sandbox backend, and egress policy — and is
worth checking after any configuration change.
