# Working on this repository

Internal notes for whoever is changing the platform — the attorney, an associate, a
paralegal with technical chops, or an AI agent working on the codebase. This is not an
open-source project and takes no outside contributions.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ./packages/maverick-core
for p in maverick-shield maverick-channels maverick-evolve \
         maverick-dashboard maverick-mcp maverick-knowledge; do
  pip install --no-deps -e "./packages/$p"
done
pip install --no-deps -e ./apps/installer-cli
pip install pytest pytest-asyncio pytest-xdist ruff vulture 'pyjwt[crypto]' cffi build
```

Core installs *with* dependencies; the rest with `--no-deps` so pip does not try to
resolve first-party names from a public index.

## Before you push

```bash
python -m pytest -q -n 4 packages/ apps/    # ~4 min; serial takes ~30
python -m ruff check .
python -m vulture
```

`-n 4` matters. The suite is ~20,000 tests and running it serially is the difference
between iterating and waiting.

Beyond ruff, CI enforces a set of gates that each fail the build. Run them locally
before pushing anything that touches their subject:

```bash
python -m maverick.plugin_matrix --ci
python -m maverick.deprecations --ci
python -m maverick.a11y_audit --ci             # dashboard templates
python -m maverick.migration_governance --ci   # world-model migrations are immutable
python -m maverick.evaluator_evolution --ci    # evaluator anchors are immutable
python -m maverick.schema_migrations --ci
python -m maverick.grpc_api.contract --check   # proto changes must be additive
MAVERICK_ENCRYPT_AT_REST=0 python -m maverick.control_data_plane_e2e --ci
```

## House rules

These are load-bearing; they are the reason the platform is safe to point at client
files.

1. **The kernel runs without the shield.** Never make `agent-shield` a hard
   requirement — fail open with a warning.
2. **Never hard-code a model.** Use `maverick.config.get_role_model(role)`.
3. **Budget caps are not optional.** Never bypass `budget.check()`.
4. **All shell through `sandbox.exec()`.** CI greps for violations.
5. **No new top-level dependency without a config knob.**
6. **The wizard is the UX source of truth** — a capability nobody can enable through
   `apps/installer-cli/` does not exist for a non-technical user.
7. **Surgical diffs.** Match the surrounding style; no speculative abstractions; write
   the test first for a fix.

## Pack authoring

A new specialist pack is a TOML file in `packages/maverick-core/maverick/domains/`. It
must declare a persona, a compartment, a least-privilege `allow_tools` envelope, an
explicit `deny_tools` floor (`shell` and `write_file` at minimum), a `[[workflow]]`
playbook, and an `[output]` block naming its deliverable and the human who consumes it.
Legal packs additionally require a `review` or `approval` gate — the test suite enforces
this, and the only exceptions are two internal-workflow seats that are themselves
guarded by a test.

Run `maverick domains-lint`, `domains-audit`, and `domains-eval` after authoring.

## Commit and PR conventions

- Conventional Commits for PR titles: a `type:` prefix (`feat:`, `fix:`, `docs:`,
  `chore:`, `refactor:`, `test:`, `perf:`, `ci:`) and a subject starting with a
  **letter** — `feat: 2027 …` fails the title lint.
- No AI attribution footers anywhere that lands in the repository: not in commit
  trailers, PR bodies, issue comments, or code comments.
- `import tomllib` bare at column 0 breaks Python 3.10. Use the documented
  `try` / `except ModuleNotFoundError: import tomli as tomllib` fallback.

`CLAUDE.md` carries the longer-form version of all of this, including the environment
traps worth knowing before you lose an hour to one.
