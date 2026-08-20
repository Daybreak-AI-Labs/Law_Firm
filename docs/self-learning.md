# Governed local improvement

The law-firm runtime improves from the firm's own reviewed outcomes. It does
not install remote skills, load external plugins, synthesize executable tools,
discover arbitrary API specifications, or start subprocess capability servers.

The retained loop is deliberately local and evidence-gated:

1. completed matter work produces bounded reflexion and outcome evidence;
2. local distillation can turn repeated, successful procedures into draft
   guidance under the active tenant and matter boundaries;
3. rehearsal, calibration, held-out evaluation, and approval gates decide
   whether a candidate is safe to use;
4. promotion and rollback remain auditable and revocable.

## Configuration

```toml
[self_learning]
enable = true
distill_local = true
allow_provider_egress = false
```

`enable` is the master policy for governed local learning. `distill_local`
controls local procedure distillation. `allow_provider_egress` is a separate,
off-by-default authority for learning helpers that would send redacted task or
result text through an already configured model provider.

Set `MAVERICK_SELF_LEARNING=0` to stop local learning. The global learning HALT
and the promotion-controller gates remain authoritative even when the stored
configuration enables learning.

## Removed acquisition surfaces

This firm profile has no catalog search/install, plugin entry points, generated
tool store, OpenAPI discovery, MCP acquisition, or automatic pack provisioning.
Local skills are shipped with the build and are read-only at runtime. Adding or
changing executable capability requires a reviewed software release.
