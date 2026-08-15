"""Auth routes: first-run setup, login, TOTP enrolment + verification, logout.

Hardening baked in: atomic first-owner bootstrap, timing-equalized login (no
email enumeration), per-identifier rate limiting on password + TOTP, single-use
TOTP (RFC 6238 §5.2), fail-closed Secure cookies, and epoch-bumped logout that
revokes live sessions server-side.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from . import audit, auth, ratelimit, security, store
from .render import render
from .util import client_ip, secure_cookie

router = APIRouter()

# A fixed hash so login spends PBKDF2 time even when the email is unknown/disabled
# — equalizes response timing so an attacker can't enumerate staff by latency.
_DUMMY_PW_HASH = security.hash_password("timing-equalizer")


@router.get("/setup")
def setup_form(request: Request):
    if store.count_staff(request.app.state.conn) > 0:
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html", {})


@router.post("/setup")
def setup_submit(request: Request, email: str = Form(...), name: str = Form(""),
                 password: str = Form(...)):
    conn = request.app.state.conn
    if store.count_staff(conn) > 0:
        return RedirectResponse("/login", status_code=303)
    if len(password) < 10:
        return render(request, "setup.html",
                      {"error": "Password must be at least 10 characters."})
    sid = store.create_first_owner(conn, email=email, name=name,
                                   pw_hash=security.hash_password(password))
    if sid is None:                        # lost the race — an owner already exists
        return RedirectResponse("/login", status_code=303)
    audit.record(conn, actor=email, action="staff.create", target=email,
                 detail={"role": "owner", "bootstrap": True})
    resp = RedirectResponse("/enroll-totp", status_code=303)
    auth.set_session(resp, staff_id=sid, stage="enroll", secure=secure_cookie(request))
    return resp


@router.get("/login")
def login_form(request: Request):
    conn = request.app.state.conn
    if store.count_staff(conn) == 0:
        return RedirectResponse("/setup", status_code=303)
    if auth.load_staff(request) is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {})


@router.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    conn = request.app.state.conn
    key = "login:" + email.strip().lower()
    if ratelimit.locked(key):
        return render(request, "login.html",
                      {"error": "Too many attempts — try again later."})
    row = store.get_staff_by_email(conn, email)
    if row is None or row["disabled"]:
        security.verify_password(password, _DUMMY_PW_HASH)     # equalize timing
        ratelimit.record_failure(key)
        return render(request, "login.html", {"error": "Invalid credentials."})
    if not security.verify_password(password, row["pw_hash"]):
        ratelimit.record_failure(key)
        return render(request, "login.html", {"error": "Invalid credentials."})
    ratelimit.record_success(key)
    enrolled = bool(row["totp_enrolled"])
    audit.record(conn, actor=row["email"], action="staff.login.password",
                 target=row["email"], detail={"ip": client_ip(request)})
    resp = RedirectResponse("/totp" if enrolled else "/enroll-totp", status_code=303)
    auth.set_session(resp, staff_id=row["id"], stage="totp" if enrolled else "enroll",
                     secure=secure_cookie(request))
    return resp


@router.get("/totp")
def totp_form(request: Request):
    if auth.load_staff(request, stage="totp") is None:
        return RedirectResponse("/login", status_code=303)
    return render(request, "totp.html", {})


@router.post("/totp")
def totp_submit(request: Request, code: str = Form(...)):
    conn = request.app.state.conn
    st = auth.load_staff(request, stage="totp")
    if st is None:
        return RedirectResponse("/login", status_code=303)
    key = "totp:" + str(st.id)
    if ratelimit.locked(key):
        return render(request, "totp.html",
                      {"error": "Too many attempts — try again later."})
    row = store.get_staff_row(conn, st.id)
    step = security.totp_match_step(row["totp_secret"], code,
                                    after_step=row["totp_last_step"])
    if step is None:
        ratelimit.record_failure(key)
        return render(request, "totp.html", {"error": "Wrong or expired code."})
    store.set_totp_last_step(conn, st.id, step)                 # single-use
    ratelimit.record_success(key)
    audit.record(conn, actor=st.email, action="staff.login.totp", target=st.email,
                 detail={"ip": client_ip(request)})
    resp = RedirectResponse("/", status_code=303)
    auth.set_session(resp, staff_id=st.id, stage="full", role=st.role,
                     epoch=st.session_epoch, secure=secure_cookie(request))
    return resp


@router.get("/enroll-totp")
def enroll_form(request: Request):
    conn = request.app.state.conn
    st = auth.load_staff(request, stage="enroll")
    if st is None:
        return RedirectResponse("/login", status_code=303)
    row = store.get_staff_row(conn, st.id)
    secret = row["totp_secret"] or security.new_totp_secret()
    if not row["totp_secret"]:
        store.set_totp(conn, st.id, secret, enrolled=False)
    return render(request, "enroll_totp.html",
                  {"secret": secret, "uri": security.totp_uri(secret, st.email)})


@router.post("/enroll-totp")
def enroll_submit(request: Request, code: str = Form(...)):
    conn = request.app.state.conn
    st = auth.load_staff(request, stage="enroll")
    if st is None:
        return RedirectResponse("/login", status_code=303)
    key = "totp:" + str(st.id)
    if ratelimit.locked(key):
        return render(request, "enroll_totp.html",
                      {"error": "Too many attempts — try again later."})
    row = store.get_staff_row(conn, st.id)
    step = security.totp_match_step(row["totp_secret"], code,
                                    after_step=row["totp_last_step"])
    if step is None:
        ratelimit.record_failure(key)
        return render(request, "enroll_totp.html",
                      {"secret": row["totp_secret"],
                       "uri": security.totp_uri(row["totp_secret"], st.email),
                       "error": "Wrong code — check your authenticator and retry."})
    store.set_totp(conn, st.id, row["totp_secret"], enrolled=True)
    store.set_totp_last_step(conn, st.id, step)                 # single-use
    ratelimit.record_success(key)
    audit.record(conn, actor=st.email, action="staff.totp.enroll", target=st.email)
    resp = RedirectResponse("/", status_code=303)
    auth.set_session(resp, staff_id=st.id, stage="full", role=st.role,
                     epoch=st.session_epoch, secure=secure_cookie(request))
    return resp


@router.post("/logout")
def logout(request: Request):
    st = auth.load_staff(request)
    if st is not None:
        store.bump_session_epoch(request.app.state.conn, st.id)   # revoke live sessions
    resp = RedirectResponse("/login", status_code=303)
    auth.clear_session(resp)
    return resp
