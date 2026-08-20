# maverick-installer

The private law-firm setup wizard. Install it only as part of the five-package
release cohort from the same reviewed repository commit:

```bash
python scripts/install_release_cohort.py --source-root . \
  --target-python python --core-extra release-runtime
maverick init
```

The standalone `maverick-init` entry point invokes the same wizard.

The recorded deployment choices are exactly `local`, `docker`, and `vps`.
Docker is an operator-built private image: no public image is published or
trusted. Secure/container-required operation also requires the sandbox image to
be an immutable `repository@sha256:<digest>` or local `sha256:<image-id>`.

## Modes

- **consumer** collects the operator name, one provider, one exact run-wide
  `provider:model` pin, provider credential or local endpoint, workspace, and
  hard budget using firm-safe defaults.
- **advanced** exposes retained provider, model, sandbox, budget, authentication,
  audit, encryption, knowledge, and local-learning controls.
- `--fast` writes reviewed defaults without prompts only when
  `MAVERICK_MODEL_OVERRIDE=provider:model` explicitly selects the run model; it
  never guesses a vendor and does not approve public provider or host egress.
- `--resume` resumes an interrupted advanced run.
- `--from-file PATH` installs one bounded regular configuration file from the
  exact bytes read from an identity-bound handle; symlinks and changed sources
  are rejected.

The wizard writes `~/.maverick/config.toml` and, when secrets were entered,
`~/.maverick/.env`, with private permissions. A changed file is backed up before
atomic replacement. Re-run `maverick config-lint` and `maverick doctor` after
every configuration change.
