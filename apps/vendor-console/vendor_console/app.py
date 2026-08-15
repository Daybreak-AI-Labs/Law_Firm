"""Daybreak vendor console — FastAPI app (server-rendered Jinja + HTMX).

Internal control plane: customers, license lifecycle, a serve API the connected
deployments poll, fleet check-ins, and a tamper-evident vendor audit log. Auth is
local accounts + TOTP (see :mod:`.auth`). Run with ``daybreak-console`` or
``uvicorn vendor_console.app:app``.

Routes live in ``routes_auth`` / ``routes_console`` / ``routes_api``; this module
only assembles the app and owns the DB + the auth-redirect exception handler.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, routes_api, routes_auth, routes_console
from .render import render
from .util import same_origin

_HERE = Path(__file__).parent
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(title="Daybreak Vendor Console", docs_url=None, redoc_url=None)
    conn = db.connect(db_path)
    db.init_db(conn)
    app.state.conn = conn

    static_dir = _HERE / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def _csrf_origin(request: Request, call_next):
        # Defence-in-depth over SameSite=Lax: reject a cross-site Origin on any
        # state-changing request (the serve API is header-token auth, exempt).
        if (request.method in _MUTATING and not request.url.path.startswith("/api")
                and not same_origin(request)):
            return JSONResponse({"error": "cross-origin request refused"},
                                status_code=403)
        return await call_next(request)

    @app.exception_handler(StarletteHTTPException)
    async def _on_http_exc(request: Request, exc: StarletteHTTPException):
        # API paths → JSON; a browser 401 → the login screen; other errors render.
        if request.url.path.startswith("/api"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        resp = render(request, "error.html",
                      {"code": exc.status_code, "detail": exc.detail})
        resp.status_code = exc.status_code
        return resp

    app.include_router(routes_api.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_console.router)
    return app


app = create_app()


def main() -> None:  # pragma: no cover - server entry point
    import os

    import uvicorn
    uvicorn.run("vendor_console.app:app",
                host=os.environ.get("VENDOR_CONSOLE_HOST", "127.0.0.1"),
                port=int(os.environ.get("VENDOR_CONSOLE_PORT", "8900")))


if __name__ == "__main__":  # pragma: no cover
    main()
