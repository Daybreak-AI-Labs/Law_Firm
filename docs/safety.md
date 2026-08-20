# Safety boundary

The firm runtime ships `maverick-shield` in the same reviewed wheel cohort as
the core. It scans model-bound user input, each tool call, and tool output before
that content can return to the model or user.

Under the default secure posture, a missing scanner, scanner exception, or deny
verdict prevents model/tool work or withholds the affected content. The older
fail-open behavior is reachable only after explicitly disabling secure defaults
and is not an approved client-matter configuration.

## What Shield does

- Scans input, tool arguments, and output at central orchestration and dispatch
  chokepoints.
- Normalizes hostile Unicode and re-scans bounded decoded variants to catch
  encoded prompt injection.
- Frames tool output as untrusted data, redacts detected secrets, and caps its
  size before it re-enters model context.
- Records blocks on the audit chain and can seal the affected run compartment.

The built-in rules are a local heuristic defense, not proof that content is safe.
They supplement—never replace—exact-matter authorization, capability, egress,
approval, audit, and qualified-attorney release gates.

## Firm configuration

```toml
[security]
secure_defaults = true

[safety]
profile = "strict"
scan_input = true
scan_tool_calls = true
scan_output = true
```

Set `MAVERICK_REQUIRE_SHIELD=1` and run the regulated-deployment preflight before
serving client matters. Do not use `profile = "off"`, disable a scan sink, or set
`MAVERICK_SECURE_DEFAULT=0` in the firm deployment.

The separately named `agent-shield` SDK remains optional. If absent, the reviewed
`maverick-shield` built-in backend still runs. Evidence in
[the Shield benchmark](security/shield-benchmark.md) is repository regression
evidence only; it is not a generalized efficacy or compliance claim.

## Operational verification

```powershell
maverick config-lint
maverick doctor
python -m maverick_shield.redteam
```

Verify the installed wheel cohort and treat every Shield warning or withheld
result as a security event. The dashboard has no separate `/safety` control
plane; operators use the local CLI, configuration review, logs, and signed audit.
