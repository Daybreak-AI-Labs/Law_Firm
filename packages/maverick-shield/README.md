# maverick-shield

Agent Shield integration for Maverick. Provides three safety chokepoints
the agent loop wraps around:

- `Shield.scan_input(text)` — before user input enters the orchestrator
- `Shield.scan_tool_call(name, args)` — before any tool executes
- `Shield.scan_output(text)` — before the final answer reaches the user

See [`../../docs/safety.md`](../../docs/safety.md) for profiles and

## Roster-wide governance invariants

Beyond the three runtime chokepoints, the firm profile enforces governance
invariants across the fixed 31-profile legal roster, with non-vacuous
fault-injection controls:

1. **Tool-reachability** — no drafting/non-builder agent can reach a
   state-mutating tool.
2. **Capability attenuation** — a spawned child can never exceed its parent's
   grant (no privilege escalation).
3. **Matter isolation** — client material never crosses matter authorization
   boundaries.
4. **Hard refusals** — the universal refusal floor is unstrippable.
5. **Budget caps** — no cap is ever silently exceeded.

The suite also hostile-argument fuzzes every connector and tool, and each
invariant ships a fault-injection control proving it is non-vacuous.
Robustness hardening from the stress sweep: connectors return an ERROR string
(rather than raising) on a non-string op/path/query, `Skill.parse` raises
`ValueError` (not `AttributeError`) on malformed/untrusted frontmatter, and
`format_money` degrades gracefully on a None/empty currency.

threat coverage.
