# Third-Party Notices

Maverick is distributed with, or depends on, third-party open-source software.
This file inventories the **declared** runtime and optional dependencies of the
`maverick-*` packages and their best-known SPDX license identifiers, so that
attribution and license obligations can be tracked.

> **Provenance & scope.** This list is generated from the `dependencies` and
> `optional-dependencies` declared in each package's `pyproject.toml`; it does
> not include transitive dependencies. SPDX identifiers below are the commonly
> published licenses for each project and are provided for convenience — they
> are **not** a substitute for the canonical `LICENSE`/`NOTICE` shipped inside
> each distribution. Before redistributing a built artifact, regenerate an
> authoritative, transitive inventory from the CycloneDX SBOM produced in CI
> (`.github/workflows/ci.yml`) and reproduce each dependency's required license
> text and `NOTICE` (Apache-2.0) per its terms.

## Permissive dependencies (attribution required)

The following are distributed under permissive licenses (MIT / BSD / Apache-2.0
/ ISC / PSF) that require reproducing their copyright and license notice on
redistribution. Identifiers are best-known and should be verified against the
installed version:

| Package | Likely license (SPDX) |
|---|---|
| anthropic | MIT |
| openai | Apache-2.0 |
| fastapi | MIT |
| starlette | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| pydantic (via fastapi) | MIT |
| httpx | BSD-3-Clause |
| requests | Apache-2.0 |
| urllib3 | MIT |
| idna | BSD-3-Clause |
| click | BSD-3-Clause |
| questionary | MIT |
| rich | MIT |
| jinja2 | BSD-3-Clause |
| orjson | Apache-2.0 / MIT |
| python-multipart | Apache-2.0 |
| websockets | BSD-3-Clause |
| pyjwt | MIT |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| boto3 | Apache-2.0 |
| redis | MIT |
| arq | MIT |
| psutil | BSD-3-Clause |
| prometheus-client | Apache-2.0 |
| opentelemetry-api | Apache-2.0 |
| opentelemetry-sdk | Apache-2.0 |
| opentelemetry-exporter-otlp-proto-http | Apache-2.0 |
| sentry-sdk | MIT |
| torch | BSD-3-Clause |
| transformers | Apache-2.0 |
| sentence-transformers | Apache-2.0 |
| fastembed | Apache-2.0 |
| pdfplumber | MIT |
| pypdf | BSD-3-Clause |
| python-docx | MIT |
| openpyxl | MIT |
| lxml | BSD-3-Clause |
| pillow | MIT-CMU (HPND) |
| pytesseract | Apache-2.0 |
| tomli | MIT |

## Build / dev / test tooling (not redistributed in runtime artifacts)

`ruff`, `pytest`, `pytest-asyncio`, `pre-commit` — used for development and CI
only; not bundled into distributed wheels.

## Optional/non-distributed

`agent-shield` — declared as an optional extra; the kernel runs without it
(fail-open) and it is not bundled. If you ship it, add its license here.

---

_To regenerate this inventory authoritatively (including transitive deps and
exact installed-version licenses), use the CycloneDX SBOM from CI or run a
license tool such as `pip-licenses` against the installed environment._
