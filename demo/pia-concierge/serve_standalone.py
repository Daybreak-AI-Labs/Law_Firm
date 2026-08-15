"""Standalone launcher: the PIA Concierge agent ALONE, no Lightwork platform.

Runs only the agent on :8890 — the ticketing intake, the requester inbox and
chat/voice/guided questionnaire, and the OneTrust tenant where a human reviews
and approves. There is no dashboard, no world-model governance, no signed
audit chain, and no privacy workspace: those come with Lightwork.

    PIA_STANDALONE=1  python serve_standalone.py

This is what a client who buys the agent on its own runs. It imports NOTHING
from ``maverick`` — the scoring engine is the self-contained ``pia_engine``.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Force standalone before anything imports capabilities/backend.
os.environ.setdefault("PIA_STANDALONE", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))


def bind() -> tuple[str, int]:
    """Loopback by default (safe for a desktop install). Deployments behind a
    reverse proxy / in a container set PIA_HOST=0.0.0.0 — and MUST put TLS
    and authentication in front (see DEPLOYMENT.md): the intake links are
    reachable by case id by design."""
    return (os.environ.get("PIA_HOST", "127.0.0.1"),
            int(os.environ.get("WORLD_PORT", "8890")))


async def main() -> None:
    import uvicorn
    from app import app as world_app

    host, port = bind()
    server = uvicorn.Server(uvicorn.Config(
        world_app, host=host, port=port, log_level="warning"))
    print("PIA Concierge — STANDALONE agent (no Lightwork platform)")
    print(f"  Agent + OneTrust review → http://{host}:{port}")
    print("  (dashboard, signed audit, and the privacy workspace need Lightwork)")
    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
