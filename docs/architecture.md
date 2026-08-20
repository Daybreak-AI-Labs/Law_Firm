# Architecture

The firm runtime is organized around one non-negotiable boundary: every model
or tool execution belongs to an exact client matter and named active member.

The dashboard persists a client, matter number, jurisdiction, reviewed legal
domain, responsible-attorney membership, and matter goal. The queue signs the
matter and principal; the worker re-resolves them; and the runner binds an
immutable `MatterContext` before any provider or tool dispatch. Live revocation,
matter reassignment, incomplete metadata, or an egress-policy change fails
closed.

Matters default to `local_only`. Public services require both responsible-
attorney approval on the matter and an exact operator provider/HTTPS-host
allowlist. Drafts remain unreleased until qualified-attorney sign-off is bound
to the current goal and artifact digest.

The retained deployment consists of the core runtime, dashboard, knowledge,
shield, and local installer packages. MCP, gRPC, external plugin, and remote
execution-server surfaces are deliberately absent.
