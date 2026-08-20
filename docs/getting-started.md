# Getting started

Install and run only a reviewed commit of the private firm repository. The five
Python distributions are one lockstep cohort; do not assemble a partial runtime
or install similarly named public packages.

## Install the reviewed cohort

```bash
git clone https://github.com/Daybreak-AI-Labs/Law_Firm
cd Law_Firm
git checkout --detach <reviewed-full-40-character-commit-sha>

python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python scripts/install_release_cohort.py --source-root . \
  --target-python python --core-extra release-runtime
```

The cohort helper validates the five distribution names and versions, installs
them under the reviewed constraints, checks dependencies, and imports every
runtime package.

## Configure the firm boundary

```bash
maverick init
maverick config-lint
maverick doctor
```

The wizard writes private `~/.maverick/config.toml` and `.env` files. Before
client work, the operator must provision:

- named-user authentication (OIDC or the retained named invite/session flow);
- the at-rest, audit-signing, queue-signing, backup-signing, and independently
  custodied backup-encryption keys required by the deployment;
- a local model endpoint, or exact firm provider and HTTPS-host allowlists for
  contracted services;
- a container sandbox if the selected workflow exposes executable tools.

A shared dashboard bearer is not a human execution identity and cannot execute
matter work even if someone inserts it into a membership table.

## Start the dashboard and worker

```bash
maverick dashboard
maverick worker
```

Open `http://127.0.0.1:8765`. A named responsible attorney creates or selects a
client, opens a matter with a firm matter number, jurisdiction, and reviewed
legal profile, and then creates a goal inside that matter. The matter defaults
to `local_only`; public provider or tool egress remains blocked unless the
responsible attorney and operator independently approve it.

Use a legal starter such as “Draft a source-cited research memo for attorney
review.” The worker re-resolves the signed principal, matter membership,
client, jurisdiction, legal domain, and current egress policy immediately
before dispatch. The resulting draft cannot be released until a qualified
attorney signs off on the exact current content and artifact digest.

## Operational checks

```bash
maverick domains-lint
maverick audit verify
maverick backup create /operator-custody/firm.mvkb
maverick backup verify /operator-custody/firm.mvkb
```

`domains-lint` must report 31 legal profiles with no errors. Backup creation and
verification require separate operator-custodied signing and encryption keys;
the key material must never live below the Maverick data root.

See [configuration](configuration.md), [deployment](deployment.md), and the
[threat model](security/threat-model.md) before enabling client data.
