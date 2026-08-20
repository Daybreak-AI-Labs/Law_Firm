# Firm runtime tool inventory

The inherited tree contained 275 Python paths under `maverick/tools/`, including
`__init__.py`. The firm tree retains 27: 248 tool modules were removed together
with their dedicated tests and obvious optional dependencies.

Physical modules are not the runtime catalog. Some retained files are imported
by offline evaluation, parsing, or operator-only code. Under secure
defaults, `ToolRegistry` enforces a fixed ceiling of 30 names and intersects it
with the exact bound legal profile. A registry built without a valid matter
snapshot exposes only `budget_status` and `citation_verifier`.

## Maximum secure catalog

The 19 legal-profile names are:

`carta_read`, `clause_library_read`, `clio_read`,
`contract_clause_read`, `contract_read`, `contract_repository_read`,
`contractbook_read`, `dependency_manifest_read`, `docusign_read`,
`incident_record_read`, `invoice_read`, `ironclad_read`, `knowledge_search`,
`list_attachments`, `read_attachment`, `read_file`, `spreadsheet`, `sql_query`,
and `web_search`.

The 11 kernel names are:

`ask_user`, `budget_status`, `citation_verifier`, `delegate_to_agent`,
`kv_memory`, `list_specialists`, `recv_from_agent`, `send_to_agent`,
`spawn_specialist`, `spawn_subagent`, and `spawn_swarm`.

This is a ceiling, not a promise that every name is instantiated. The selected
profile, feature switches, risk ACL, and bound `MatterContext` can only narrow
it. `ask_user` writes durable matter state and therefore also requires the
bound context.

The interactive Playwright `browser` tool is not reserved or registered: its
document and subresource requests could not be constrained by the central firm
HTTP egress guard, and mutating page actions could transmit client data.

## Retained connector surface

Only `carta_read`, `clio_read`, `contractbook_read`, `docusign_read`, and
`ironclad_read` are generated from connector specs. They are GET-only and must
be declared by the selected profile; there is no suite-wide grant.

## Explicitly outside the runtime catalog

The firm catalog cannot register `shell`, `write_file`, `apply_patch`,
`str_edit`, `code_exec`, `find_tools`, `learn_capability`, generated tools,
plugins, MCP tools, gRPC plugins, or the inherited SaaS/public-data long tail.
Secure registration also refuses a later attempt to shadow a retained
first-party name.

`tools/apply_patch.py` remains as an offline/operator parser used by Agent patch
validation, but `apply_patch` is not in the catalog. Media, parser, assessment,
intake, and diagnostic helpers that still have production importers likewise
do not expand the catalog.

The executable contract is
`packages/maverick-core/tests/test_firm_tool_registry.py`; do not replace that
fixed-set assertion with a minimum count.
