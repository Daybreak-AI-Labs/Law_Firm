"""Single-process launcher: real Lightwork dashboard + the external-world harness.

Both servers run in ONE process and event loop, matching the platform's own
deployment shape (`maverick dashboard` hosts the runner in-process) — and,
critically, giving the Ed25519 audit chain its required single writer: the
dashboard's approval-decision audit rows and the concierge's agent-step rows
chain through the same signer, so `maverick audit verify` stays green.

  :8765  maverick_dashboard.app:app  (goals · workspaces · audit)
  :8890  app:app                     (ServiceNow · inbox · chat/guided intake ·
                                      OneTrust review & approve)
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Run the REPO's platform code, not whatever happens to be pip-installed:
# a stale (non-editable) site-packages install otherwise serves yesterday's
# dashboard templates after a `git pull` — the demo silently demos old code.
# Prepending the checkout's package dirs makes `git pull` + `python serve.py`
# always show what you just pulled, on every OS, with no pip step.
_REPO = Path(__file__).resolve().parents[2]
for _pkg in sorted((_REPO / "packages").glob("maverick-*")):
    # A package root qualifies if it holds an importable top-level package
    # (maverick-core's is `maverick`, the rest are underscored names).
    if any(p.joinpath("__init__.py").is_file()
           for p in _pkg.iterdir() if p.is_dir()):
        sys.path.insert(0, str(_pkg))


def _maybe_seed_workspace() -> None:
    """Self-heal: a fresh MAVERICK_HOME renders bare, empty workspaces.

    When the demo home has never been seeded (no ``.workspace-seeded`` marker
    — the same marker ``seed_workspace.main`` writes and checks), run the
    workspace seeder before serving so the first launch demos a lived-in
    tenant. ``PIA_SEED_WORKSPACE=0`` skips (default "1"); an unset
    MAVERICK_HOME also skips, since the seeder requires an explicit home.
    """
    if os.environ.get("PIA_SEED_WORKSPACE", "1").strip().lower() in ("0", "false", "no"):
        return
    home = os.environ.get("MAVERICK_HOME", "")
    if not home or (Path(home) / ".workspace-seeded").exists():
        return
    print("Workspace not seeded yet — seeding demo history first "
          "(set PIA_SEED_WORKSPACE=0 to skip).")
    import seed_workspace
    seed_workspace.main()   # marker-guarded; writes .workspace-seeded


async def main() -> None:
    import uvicorn
    from app import app as world_app
    from maverick_dashboard.app import app as dash_app

    # Bring-your-own-agent plane ON: the seeded Agentforce agent
    # (seed_workspace._seed_external_agent) needs it for the /external-agents
    # roster, the gateway routes, and the parked approval to show. setdefault
    # so an operator can still force it off with MAVERICK_EXTERNAL_AGENTS=0.
    os.environ.setdefault("MAVERICK_EXTERNAL_AGENTS", "1")

    _maybe_seed_workspace()

    dash_port = int(os.environ.get("DASH_PORT", "8765"))
    world_port = int(os.environ.get("WORLD_PORT", "8890"))

    dash = uvicorn.Server(uvicorn.Config(
        dash_app, host="127.0.0.1", port=dash_port, log_level="warning"))
    world = uvicorn.Server(uvicorn.Config(
        world_app, host="127.0.0.1", port=world_port, log_level="warning"))
    # Server.serve() (unlike run()) installs no signal handlers, so two servers
    # coexist on one loop; Ctrl-C propagates as KeyboardInterrupt to both.
    print(f"Lightwork dashboard  → http://127.0.0.1:{dash_port}")
    print(f"External world       → http://127.0.0.1:{world_port}")
    await asyncio.gather(dash.serve(), world.serve())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
