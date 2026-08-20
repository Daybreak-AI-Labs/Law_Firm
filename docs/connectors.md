# Law-firm connectors

The firm runtime ships exactly five third-party SaaS connectors. Every one is
GET-only: its schema cannot express POST, PUT, PATCH, or DELETE. Selecting a
legal suite does not grant any connector automatically. A connector is visible
only when the exact legal profile declares it and an authenticated
`MatterContext` proves current membership in the goal's matter.

| Tool | System | Endpoint | Credential |
| --- | --- | --- | --- |
| `carta_read` | Carta | `CARTA_BASE_URL` | `CARTA_TOKEN` |
| `clio_read` | Clio | `CLIO_BASE_URL` (defaults to `https://app.clio.com`) | `CLIO_TOKEN` |
| `contractbook_read` | Contractbook | `CONTRACTBOOK_BASE_URL` (defaults to `https://api.contractbook.com`) | `CONTRACTBOOK_TOKEN` |
| `docusign_read` | DocuSign | `DOCUSIGN_BASE_URL` | `DOCUSIGN_TOKEN` |
| `ironclad_read` | Ironclad | `IRONCLAD_BASE_URL` (defaults to `https://ironcladapp.com`) | `IRONCLAD_TOKEN` |

Store credentials in the firm's protected runtime environment, not in a goal,
matter document, prompt, or repository file. Requests still pass through the
central egress policy and SSRF-safe pinned transport. A matter in `local_only`
mode cannot use a hosted connector even when its profile names the tool.

The inherited enterprise/public-data catalog, ambient-credential adapters,
GraphQL generator, plugin discovery, generated-tool loader, and MCP/gRPC tool
acquisition paths are not part of the firm runtime. Adding another connector is
therefore a reviewed code change: add one read-only spec, name it in an exact
legal profile, update the fixed registry contract, and supply egress and
cross-matter denial tests.

The authoritative implementation is
`packages/maverick-core/maverick/tools/_connector_specs.py`; the immutable
runtime ceiling is tested in
`packages/maverick-core/tests/test_firm_tool_registry.py`.
