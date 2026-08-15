"""Shared Jinja templates + the ``render`` helper (module-level so route modules
can import it without a per-app closure)."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


def _fmt_ts(value) -> str:
    """Jinja filter: epoch seconds → 'YYYY-MM-DD HH:MM' (UTC)."""
    try:
        return _dt.datetime.fromtimestamp(float(value), _dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value)


templates.env.filters["ts"] = _fmt_ts


def render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    # Starlette's current signature: TemplateResponse(request, name, context);
    # it injects `request` into the context itself.
    return templates.TemplateResponse(request, name, ctx)
