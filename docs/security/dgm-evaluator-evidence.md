# DGM evaluator evidence boundary

## Current firm posture

The firm distribution retains two execution backends: local subprocesses and
Docker. Neither backend can produce controller-authenticated terminal test
evidence. The governed code rung therefore fails closed before a model proposal
or candidate test runs. It does not infer fitness from candidate-controlled
stdout, JUnit files, coverage files, plugins, or exit status.

This limitation is deliberate. A candidate can forge files and summaries in
its workspace, and a process exit code does not prove which tests were
collected or executed. Docker provides resource and network containment, not
an independent evidence authority.

## Evidence contract

Enabling code-rung fitness in a future reviewed release would require an
independent evaluator boundary that:

- authenticates a controller-issued nonce and the exact command, immutable
  subject digest, execution-context digest, baseline/candidate arm, and pinned
  policy;
- observes terminal passed, failed, skipped, and error counts outside the
  candidate process;
- authenticates its response under a pinned, non-secret authority identity;
- rejects duplicate nonces and retains an append-only audit record; and
- enforces non-host execution, deny-by-default egress, non-root execution,
  bounded output, process and memory limits, and an exact workspace binding.

No external sandbox-provider or plugin loading surface is shipped. Until a
reviewed implementation is part of the fixed firm distribution and passes its
adversarial conformance tests, code-rung fitness remains unavailable rather
than degrading to a simulated score.
