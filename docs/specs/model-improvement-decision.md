# Decision: build governed specialist-model plumbing now

**Status:** Accepted · **Date:** 2026-07-23

## Context

The June 2026 learning-substrate decision parked an in-kernel, general-purpose
trajectory-learning loop until real volume and operator demand existed. That
was the right decision for speculative GPU training, but it did not distinguish
training execution from the governance, taskset, evidence, and qualification
plumbing required to make a later run safe and useful.

Maverick now has a sharper commercial target: deterministic specialist models
for narrow, repeated, human-reviewed work products, starting with privacy and
compliance decisions. Those environments reuse shipping deterministic engines
and the existing weights-rung promotion transaction.

## Decision

Build the vendor-neutral environment, data-boundary, candidate catalog,
qualification, receipt, Model Risk, and backend-export layers now. Keep actual
training operator-run, default-off, and gated on genuine customer-authorized
data volume and sealed evaluation.

Use per-tenant models only in v1. Do not treat the existing agent federation or
fleet memory systems as federated learning. Do not aggregate cross-tenant weight
deltas without a new threat model and legal/privacy design.

Prime Intellect Verifiers and prime-rl are optional pinned backends. They do not
replace Maverick's taskset locks, data-boundary admission, deterministic
scoring, signatures, Model Risk review, promotion transaction, or rollback.

## Effect on the earlier decision

This ADR partially supersedes
[`learning-substrate-decision.md`](learning-substrate-decision.md):

- it supersedes "no code changes" for governed specialist-model plumbing;
- it preserves the earlier refusal to fabricate training value before real
  data, GPU runs, and operator pull exist;
- it does not enable general in-kernel RLAIF, learned compaction/reflexion, or
  autonomous weight adoption.

The implementation and rollout plan live in
[`MODEL_IMPROVEMENT_PLATFORM.md`](../MODEL_IMPROVEMENT_PLATFORM.md).
