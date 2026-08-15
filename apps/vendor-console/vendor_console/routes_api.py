"""Machine routes a connected deployment polls, authenticated by the customer's
serve token (Bearer, header-only):
  - GET  /api/v1/license  — current signed license  (refresh_from_server)
  - GET  /api/v1/release  — latest signed release manifest for the channel
  - POST /api/v1/support  — file a support bundle as a ticket
Plus /healthz (unauthenticated liveness + audit-chain integrity)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException

from . import audit, licensing, releases, store, support
from .models import Customer
from .util import client_ip, hash_token

router = APIRouter()

_MAX_BUNDLE_BYTES = 1_000_000   # 1 MB — a support bundle is small; cap storage DoS


def _bearer(request: Request) -> str:
    # Header-only: never accept the serve token in the query string, where it
    # would leak into proxy/access logs, browser history, and Referer.
    authz = request.headers.get("authorization", "")
    return authz[7:].strip() if authz.lower().startswith("bearer ") else ""


def _customer_from_token(request: Request) -> Customer:
    token = _bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing serve token")
    cust = store.customer_by_serve_token_hash(request.app.state.conn, hash_token(token))
    if cust is None:
        raise HTTPException(status_code=401, detail="invalid serve token")
    return cust


async def _read_limited_body(request: Request) -> bytes:
    clen = request.headers.get("content-length", "")
    if clen.isdigit() and int(clen) > _MAX_BUNDLE_BYTES:
        raise HTTPException(status_code=413, detail="support bundle too large (max 1 MB)")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BUNDLE_BYTES:
            raise HTTPException(status_code=413, detail="support bundle too large (max 1 MB)")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/healthz")
def healthz(request: Request) -> PlainTextResponse:
    ok, broken = audit.verify_chain(request.app.state.conn)
    return PlainTextResponse("ok" if ok else f"audit-chain-broken:{broken}",
                             status_code=200 if ok else 500)


@router.get("/api/v1/license")
def serve_license(request: Request):
    conn = request.app.state.conn
    cust = _customer_from_token(request)
    doc = licensing.active_license_doc(conn, cust.id)
    store.record_checkin(
        conn, customer_id=cust.id,
        deployment_id=request.query_params.get("deployment", ""),
        version=request.query_params.get("version", ""),
        license_status="served" if doc else "none", ip=client_ip(request))
    if doc is None:
        raise HTTPException(status_code=404, detail="no active license")
    return JSONResponse(doc)


@router.get("/api/v1/release")
def serve_release(request: Request):
    """The latest signed release manifest for the caller's channel. The
    deployment runs ``release_update.plan_upgrade(current, manifest)`` itself to
    decide whether/how to apply."""
    conn = request.app.state.conn
    cust = _customer_from_token(request)
    channel = request.query_params.get("channel") or cust.channel
    manifest = releases.manifest_to_serve(conn, channel)
    store.record_checkin(
        conn, customer_id=cust.id,
        deployment_id=request.query_params.get("deployment", ""),
        version=request.query_params.get("version", ""),
        license_status=f"release:{channel}", ip=client_ip(request))
    if manifest is None:
        raise HTTPException(status_code=404, detail="no release on channel")
    return JSONResponse(manifest)


@router.post("/api/v1/support")
async def intake_support(request: Request):
    """File a redacted support bundle as a ticket (deduped by correlation_id)."""
    conn = request.app.state.conn
    cust = _customer_from_token(request)
    raw = await _read_limited_body(request)
    try:
        bundle = json.loads(raw or b"{}")
    except Exception as e:  # noqa: BLE001 - malformed body → 400, not a 500
        raise HTTPException(status_code=400, detail="body must be a JSON bundle") from e
    if not isinstance(bundle, dict):
        raise HTTPException(status_code=400, detail="bundle must be a JSON object")
    tkt = support.intake_bundle(conn, customer_id=cust.id, bundle=bundle,
                                actor=f"deployment:{cust.name}")
    return JSONResponse({"ticket_id": tkt.id, "correlation_id": tkt.correlation_id,
                         "status": tkt.status}, status_code=201)
