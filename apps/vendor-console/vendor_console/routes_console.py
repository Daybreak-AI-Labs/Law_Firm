"""Console routes: customers, license lifecycle, fleet health, audit log."""
from __future__ import annotations

import json
import re
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException

from . import audit, licensing, releases, store, support
from .auth import require_staff, require_support, require_write
from .models import Staff
from .render import render
from .util import hash_token

router = APIRouter()


def _parse_artifacts(raw: str) -> list[dict]:
    """Parse a textarea of ``name  sha256  size`` lines into artifact dicts.
    Blank lines and a leading header line are ignored; size defaults to 0."""
    out: list[dict] = []
    for line in (raw or "").splitlines():
        parts = line.replace(",", " ").split()
        if len(parts) < 2 or parts[0].lower() in ("name", "artifact"):
            continue
        size = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        out.append({"name": parts[0], "sha256": parts[1], "size": size})
    return out


def _csv(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").replace(",", "\n").splitlines() if x.strip()]


@router.get("/")
def home(_staff: Staff = Depends(require_staff)):
    return RedirectResponse("/customers", status_code=303)


@router.get("/customers")
def customers_list(request: Request, q: str = "", staff: Staff = Depends(require_staff)):
    conn = request.app.state.conn
    return render(request, "customers.html",
                  {"staff": staff, "customers": store.list_customers(conn, search=q),
                   "q": q, "section": "customers"})


@router.post("/customers")
def customers_create(request: Request, name: str = Form(...),
                     primary_contact: str = Form(""), contact_email: str = Form(""),
                     posture: str = Form("connected"),
                     staff: Staff = Depends(require_write)):
    conn = request.app.state.conn
    if not name.strip():
        return RedirectResponse("/customers", status_code=303)
    token = "svc_" + secrets.token_urlsafe(24)
    cid = store.create_customer(conn, name=name.strip(), primary_contact=primary_contact,
                                contact_email=contact_email, posture=posture,
                                serve_token_hash=hash_token(token))
    audit.record(conn, actor=staff.email, action="customer.create",
                 target=name.strip(), detail={"posture": posture})
    return render(request, "serve_token.html",
                  {"staff": staff, "customer": store.get_customer(conn, cid),
                   "token": token, "section": "customers"})


@router.get("/customers/{customer_id}")
def customer_detail(request: Request, customer_id: int,
                    staff: Staff = Depends(require_staff)):
    conn = request.app.state.conn
    cust = store.get_customer(conn, customer_id)
    if cust is None:
        raise HTTPException(status_code=404, detail="no such customer")
    rows = [{"lic": lic, "status": licensing.display_status(lic)}
            for lic in store.list_licenses(conn, customer_id)]
    return render(request, "customer_detail.html",
                  {"staff": staff, "customer": cust, "licenses": rows,
                   "matrix": licensing.feature_matrix(
                       licensing.active_license_doc(conn, customer_id)),
                   "checkin": store.latest_checkin_for(conn, customer_id),
                   "pubkey": licensing.public_key_hex(), "section": "customers"})


@router.post("/customers/{customer_id}/channel")
def set_customer_channel(request: Request, customer_id: int, channel: str = Form(...),
                         staff: Staff = Depends(require_write)):
    conn = request.app.state.conn
    from .models import CHANNELS
    if channel in CHANNELS:
        store.update_customer(conn, customer_id, channel=channel)
        audit.record(conn, actor=staff.email, action="customer.channel",
                     target=str(customer_id), detail={"channel": channel})
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@router.post("/customers/{customer_id}/licenses")
def issue_license(request: Request, customer_id: int, tier: str = Form(...),
                  suites_on: list[str] = Form([]), features_on: list[str] = Form([]),
                  features_extra: str = Form(""),
                  seats: str = Form(""), expires: str = Form(""),
                  grace_days: int = Form(14), staff: Staff = Depends(require_write)):
    """Issue/upsell from the feature-access checkboxes: ``features_on`` are the
    checked registry features (grants below tier / denials for unchecked
    in-tier ones are derived), ``suites_on`` the checked add-on suites, and
    ``features_extra`` a comma-separated escape hatch for custom grants."""
    conn = request.app.state.conn
    from maverick.entitlements import GATED_SUITES
    extra = [f.strip() for f in features_extra.replace(",", "\n").splitlines()
             if f.strip()]
    features, denied = licensing.grants_from_checkboxes(tier, features_on, extra)
    try:
        licensing.issue_license(
            conn, customer_id=customer_id, tier=tier,
            suites=[s for s in suites_on if s in GATED_SUITES],
            features=features, features_denied=denied,
            seats=int(seats) if seats.strip() else None,
            expires=expires, grace_days=grace_days, actor=staff.email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return RedirectResponse(f"/customers/{customer_id}", status_code=303)


@router.post("/licenses/{license_id}/revoke")
def revoke_license(request: Request, license_id: str, note: str = Form(""),
                   staff: Staff = Depends(require_write)):
    conn = request.app.state.conn
    lic = store.get_license(conn, license_id)
    if lic is None:
        raise HTTPException(status_code=404, detail="no such license")
    licensing.revoke_license(conn, license_id, actor=staff.email, note=note)
    return RedirectResponse(f"/customers/{lic.customer_id}", status_code=303)


@router.get("/licenses/{license_id}/download")
def download_license(request: Request, license_id: str,
                     _staff: Staff = Depends(require_staff)):
    conn = request.app.state.conn
    lic = store.get_license(conn, license_id)
    if lic is None:
        raise HTTPException(status_code=404, detail="no such license")
    body = json.dumps(lic.doc, indent=2) + "\n"
    # license_id is server-generated (lic_<hex>), but sanitize before it lands in
    # a response header regardless — never let a lookup key shape headers.
    safe = re.sub(r"[^A-Za-z0-9_-]", "", license_id)[:40]
    return Response(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="license-{safe}.json"'})


@router.get("/fleet")
def fleet(request: Request, staff: Staff = Depends(require_staff)):
    conn = request.app.state.conn
    return render(request, "fleet.html",
                  {"staff": staff, "rows": store.latest_checkins(conn),
                   "section": "fleet"})


@router.get("/audit")
def audit_view(request: Request, staff: Staff = Depends(require_write)):
    conn = request.app.state.conn
    ok, broken = audit.verify_chain(conn)
    return render(request, "audit.html",
                  {"staff": staff, "events": audit.recent(conn),
                   "chain_ok": ok, "broken": broken, "section": "audit"})


# ---- releases --------------------------------------------------------------

@router.get("/releases")
def releases_list(request: Request, staff: Staff = Depends(require_staff)):
    conn = request.app.state.conn
    return render(request, "releases.html",
                  {"staff": staff, "releases": store.list_releases(conn),
                   "section": "releases"})


@router.post("/releases")
def releases_publish(request: Request, version: str = Form(...),
                     channel: str = Form("stable"), min_from: str = Form(""),
                     notes: str = Form(""), migrations: str = Form(""),
                     artifacts: str = Form(""), staff: Staff = Depends(require_write)):
    conn = request.app.state.conn
    try:
        releases.publish_release(
            conn, version=version.strip(), channel=channel, min_from=min_from.strip(),
            notes=notes, migrations=_csv(migrations),
            artifacts=_parse_artifacts(artifacts), actor=staff.email)
    except Exception as e:  # noqa: BLE001 - bad version/sha/etc → surface a 400
        raise HTTPException(status_code=400, detail=f"publish failed: {e}") from e
    return RedirectResponse("/releases", status_code=303)


@router.post("/releases/{release_id}/yank")
def releases_yank(request: Request, release_id: int, reason: str = Form(""),
                  staff: Staff = Depends(require_write)):
    conn = request.app.state.conn
    releases.yank_release(conn, release_id, actor=staff.email, reason=reason)
    return RedirectResponse("/releases", status_code=303)


@router.get("/releases/{release_id}/manifest")
def releases_manifest(request: Request, release_id: int,
                      _staff: Staff = Depends(require_staff)):
    conn = request.app.state.conn
    rel = store.get_release(conn, release_id)
    if rel is None:
        raise HTTPException(status_code=404, detail="no such release")
    body = json.dumps(rel.manifest, indent=2) + "\n"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", rel.version)[:40]
    return Response(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="manifest-{safe}.json"'})


# ---- support desk ----------------------------------------------------------

@router.get("/support")
def tickets_list(request: Request, status: str = "",
                 staff: Staff = Depends(require_staff)):
    conn = request.app.state.conn
    return render(request, "tickets.html",
                  {"staff": staff, "tickets": store.list_tickets(conn, status=status),
                   "counts": store.ticket_status_counts(conn), "status": status,
                   "section": "support"})


@router.get("/support/{ticket_id}")
def ticket_detail(request: Request, ticket_id: int,
                  staff: Staff = Depends(require_staff)):
    conn = request.app.state.conn
    tkt = store.get_ticket(conn, ticket_id)
    if tkt is None:
        raise HTTPException(status_code=404, detail="no such ticket")
    cust = store.get_customer(conn, tkt.customer_id) if tkt.customer_id else None
    return render(request, "ticket_detail.html",
                  {"staff": staff, "ticket": tkt, "customer": cust,
                   "comments": store.list_comments(conn, ticket_id),
                   "section": "support"})


@router.post("/support/{ticket_id}/comment")
def ticket_comment(request: Request, ticket_id: int, body: str = Form(...),
                   staff: Staff = Depends(require_support)):
    conn = request.app.state.conn
    if body.strip():
        store.add_comment(conn, ticket_id=ticket_id, author=staff.email,
                          body=body.strip())
    return RedirectResponse(f"/support/{ticket_id}", status_code=303)


@router.post("/support/{ticket_id}/status")
def ticket_set_status(request: Request, ticket_id: int, status: str = Form(...),
                      note: str = Form(""), staff: Staff = Depends(require_support)):
    conn = request.app.state.conn
    from .models import TICKET_STATUSES
    if status not in TICKET_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    support.set_status(conn, ticket_id, status, actor=staff.email, note=note)
    return RedirectResponse(f"/support/{ticket_id}", status_code=303)


@router.post("/support/{ticket_id}/assign")
def ticket_assign(request: Request, ticket_id: int, assignee: str = Form(""),
                  priority: str = Form(""), staff: Staff = Depends(require_support)):
    conn = request.app.state.conn
    fields: dict = {"assignee": assignee.strip()}   # blank clears the assignee
    if priority.strip():
        fields["priority"] = priority.strip()
    store.update_ticket(conn, ticket_id, **fields)
    return RedirectResponse(f"/support/{ticket_id}", status_code=303)
