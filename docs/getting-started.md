# Getting started

## Install

The Maverick distribution names are not yet reserved on public PyPI, so do
not install those names from the public index. Install a reviewed source
commit instead:

```bash
git clone https://github.com/Daybreak-AI-Labs/Law_Firm
cd Maverick
git checkout --detach <reviewed-full-40-character-commit-sha>
pip install -e ./packages/maverick-core
pip install -e ./apps/installer-cli
maverick init
```

If you need the no-prerequisite desktop bootstrap, download
`deploy/desktop/install.sh` or `deploy/desktop/install.ps1` from the same
commit, verify it, and set `MAVERICK_REF` to the lowercase, full SHA. The
scripts fail on missing or mutable refs and have no public-index fallback.
Every `MAVERICK_*` variable can also be spelled `MAVERICK_*`.

From source while iterating:

```bash
git clone https://github.com/Daybreak-AI-Labs/Law_Firm
cd Maverick
pip install -e ./packages/maverick-core
pip install -e ./apps/installer-cli
maverick init
```

## First run

```bash
maverick init
maverick doctor
```

The wizard takes ~2 minutes. It writes `~/.maverick/config.toml` and `~/.maverick/.env`.

Then start the dashboard and queue your first goal from the web UI:

```bash
maverick dashboard
```

Open `http://127.0.0.1:8765` and create a goal, e.g. *"Build a CLI that emails me a digest of today's top Hacker News stories — research the API, write it, and verify it runs"*. Background execution is `maverick worker`.

## Watch the swarm decompose

Open the goal's page in the dashboard — it streams live. The orchestrator plans the goal, then spawns specialist sub-agents that work in parallel — here a researcher pins down the API, a coder writes the tool, and a verifier runs it:

```
Goal #1 active  2m elapsed
Build a CLI that emails me a digest of today's top Hacker News stories

Plan tree
  ├─        done  #2 Research the Hacker News Firebase API
  ├─      active  #3 Write the digest CLI (fetch + format + send)
  ├─      active  #4 Verify it runs and emails a sample digest
  ├─     pending  #5 Write a short usage README

Latest episode #7 (running)  $0.0431  in=18,204 out=2,910 tools=11

Recent activity
  4s ago [researcher] decision: top stories live at /v0/topstories.json, then /v0/item/<id>.json
  3s ago [coder] tool_call: write_file hn_digest.py (118 lines)
  1s ago [verifier] tool_call: run "python hn_digest.py --dry-run" -> printed 10 stories

Cumulative spend on this DB: $0.21
```

When done, the dashboard's Goals, Skills, and Facts pages show what's
currently active or blocked, what the swarm distilled from the run, and what
it learned about you.

## Pausing / resuming

If the swarm needs something only you can answer, it pauses and queues a
question — the goal's dashboard page shows the open question (e.g. *"Which
dates are you traveling?"*) and takes your answer inline; the goal resumes
from there.

Goals survive restarts. You can shut your laptop and come back tomorrow.

## Voice input (built-in, offline)

The dashboard's mic button and the `transcribe_audio` tool work with no
provider key: the local Whisper engine ships with the dashboard package, and
the checksum-verified model is fetched automatically when the dashboard
first starts (skipped on egress-locked deployments, or with
`[voice] auto_fetch_model = false`).

Prefer a system engine instead? A `whisper.cpp` binary on PATH (`brew
install whisper-cpp`) or faster-whisper both plug into the same chain. Set
`[voice] stt_backend = "local"` to keep audio on-machine even when
OpenAI/Groq keys are configured.

## Changing models or providers

Re-run the wizard any time:

```bash
maverick init
```

Or edit `~/.maverick/config.toml` directly. The `[models]` section maps each agent role to a `provider:model-id` string. See [`configuration.md`](./configuration.md) for the schema.

## Where data lives

| File | What |
|---|---|
| `~/.maverick/config.toml` | Your config (deployment, models, safety, budget) |
| `~/.maverick/.env` | API keys (chmod 600) |
| `~/.maverick/world.db` | Persistent world model: goals, facts, episodes |
| `~/.maverick/skills/` | Auto-distilled SKILL.md files from successful runs |
| `~/maverick-workspace/` | Default sandbox working directory |
| `~/.maverick/learned-skills/` | Skills distilled by the learning loops |
| `~/.maverick/dreams/` | Consolidated insights, rehearsal queue, learning snapshots |

All local. Nothing is uploaded except your prompts to the cloud LLM you chose.

Once you have a few runs behind you, the learning surface is two commands:
`maverick dream` (consolidate experience) and `maverick domains-lint` (audit
the specialist pack catalog).
