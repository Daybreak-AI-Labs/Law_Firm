# Changelog

All notable changes to the firm's platform. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Refocused the distribution on a private, single-firm deployment with a local
  operator CLI, authenticated dashboard, 31 built-in legal practice domains,
  and 45 reviewed firm skills.
- Made executable work exact-matter-bound: a durable client and matter,
  jurisdiction, named principal, active matter membership, enabled legal
  domain, and terminal attorney review or approval gate are required before
  model or sandbox work begins.
- Restricted knowledge retrieval to the current matter plus explicitly
  firm-approved non-client playbooks. Local embeddings and isolated document
  parsers are the supported defaults.
- Made client and matter content encrypted at rest and added operator-custodied
  AES-256-GCM backup, verification, and restore commands with fail-closed key
  handling.
- Reduced sandbox execution to the retained local and digest-pinned Docker
  backends and reduced third-party connectors to the reviewed, read-only legal
  connector set.
- Kept self-improvement local, matter-scoped, encrypted, offline-evaluated, and
  subject to durable promotion receipts and attorney governance.

### Removed

- External MCP and gRPC execution surfaces, external plugin loading and remote
  skill acquisition, fleet/global learning, and matterless goal producers.
- Desktop, phone, Tauri, browser automation, voice/TTS, notification forwarding,
  remote audit forwarding, and hosted attachment-mirroring surfaces.
- General-purpose coding, finance, tax-preparation, workforce, marketplace,
  starter-template, and non-legal domain packs and their dedicated tests and
  documentation.
- Connected entitlement refresh, generic governed-write connectors, global
  tool memory, and sandbox backends outside local and Docker.

### Security

- Added fail-closed matter-context checks at runner, queue, and worker boundaries,
  including signed queue identity, membership revocation rechecks, and rejection
  of shared static-bearer execution identities.
- Hardened untrusted PDF and DOCX parsing, archive inspection, attachment paths,
  audit payloads, outbound-host policy, approval/signoff binding, and release
  evidence integrity.
- Added explicit client offboarding, matter erasure, immutable audit evidence,
  encrypted disaster recovery, and migration-governance coverage for the
  retained firm data model.

## Earlier history

The platform began as a hard fork of an internal general-purpose agent
platform; the pre-fork release history (0.1.0-alpha through 0.1.6) described
that product, not this one, and has been removed. The fork point is recorded
in the repository history.
