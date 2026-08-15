"""License lifecycle — issue, upsell, revoke, and resolve the doc to serve.

Signs with the **product's own** Ed25519 code (:func:`maverick.entitlements.sign_license`),
so a license this console mints verifies byte-for-byte with the code a customer
runs — no second implementation to drift. The signing **private key** is the
root of trust for the whole business; it lives in the environment / a KMS, never
in the database.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import secrets
import sqlite3
from pathlib import Path

from maverick import entitlements

from . import audit, store
from .models import License

log = logging.getLogger(__name__)


# ---- signing key management ------------------------------------------------

def _key_path() -> Path:
    home = Path(os.environ.get("DAYBREAK_HOME", Path.home() / ".daybreak"))
    return home / "publisher.key"


def signing_key_hex() -> str:
    """The publisher **private** key hex. Prod: ``VENDOR_CONSOLE_SIGNING_KEY``
    (sourced from a KMS/HSM). Dev fallback: a key file generated once, 0600,
    with a loud warning — fine for testing the console, never for real issuance
    (its public half isn't embedded in any customer build)."""
    env = os.environ.get("VENDOR_CONSOLE_SIGNING_KEY")
    if env:
        return env.strip()
    path = _key_path()
    if path.exists():
        return path.read_text("utf-8").strip()
    priv, _pub = entitlements.new_keypair()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(priv, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best effort on non-POSIX
        pass
    log.warning("generated a DEV publisher key at %s — set VENDOR_CONSOLE_SIGNING_KEY "
                "(KMS-backed) before issuing real licenses", path)
    return priv


def public_key_hex(signing_key: str | None = None) -> str:
    """The public half customers embed as a trust anchor (`_EMBEDDED_PUBKEYS`)."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(signing_key or signing_key_hex()))
    return priv.public_key().public_bytes_raw().hex()


# ---- issue / revoke --------------------------------------------------------

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expiry_iso(expires: str | None) -> str | None:
    """Accept ``YYYY-MM-DD`` (or a full ISO string, or blank=perpetual) → ISO Z."""
    if not expires or not expires.strip():
        return None
    s = expires.strip()
    dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue_license(conn: sqlite3.Connection, *, customer_id: int, tier: str,
                  suites: list[str] | None = None, features: list[str] | None = None,
                  features_denied: list[str] | None = None,
                  seats: int | None = None, expires: str | None = None,
                  grace_days: int = 14, actor: str = "system",
                  signing_key: str | None = None) -> License:
    """Mint + sign + persist a new license for a customer, and audit it.

    This is also the **upsell** path: issuing a new license (e.g. Gold→Platinum,
    or +fleet) simply adds a newer row; :func:`active_license_doc` then serves it,
    so a customer upgrades by picking up the new file — no redeploy.

    ``features`` grants a gated feature à la carte below its tier;
    ``features_denied`` switches one off even when the tier includes it —
    together they are the console's per-feature checkboxes (see
    ``maverick.entitlements.Entitlements.allows``)."""
    cust = store.get_customer(conn, customer_id)
    if cust is None:
        raise ValueError(f"no such customer: {customer_id}")
    if tier not in ("basic", "gold", "platinum"):
        raise ValueError(f"unknown tier: {tier!r}")
    lic_id = "lic_" + secrets.token_hex(8)
    issued = _now_iso()
    exp = _expiry_iso(expires)
    payload = {
        "customer": cust.name, "edition": "enterprise", "tier": tier,
        "suites": list(suites or []), "features": list(features or []),
        "seats": seats, "issued_at": issued, "expires_at": exp,
        "grace_days": int(grace_days), "license_id": lic_id,
    }
    if features_denied:
        payload["features_denied"] = list(features_denied)
    doc = entitlements.sign_license(payload, signing_key or signing_key_hex())
    store.add_license(conn, customer_id=customer_id, license_id=lic_id, tier=tier,
                      suites=list(suites or []), features=list(features or []),
                      seats=seats, issued_at=issued, expires_at=exp,
                      grace_days=int(grace_days), key_id=doc["key_id"], doc=doc,
                      note="", created_by=actor)
    audit.record(conn, actor=actor, action="license.issue", target=cust.name,
                 detail={"license_id": lic_id, "tier": tier,
                         "suites": list(suites or []),
                         "features_denied": list(features_denied or []),
                         "expires_at": exp})
    lic = store.get_license(conn, lic_id)
    assert lic is not None
    return lic


def revoke_license(conn: sqlite3.Connection, license_id: str, *, actor: str = "system",
                   note: str = "") -> bool:
    lic = store.get_license(conn, license_id)
    if lic is None:
        return False
    ok = store.revoke_license(conn, license_id, note=note)
    if ok:
        cust = store.get_customer(conn, lic.customer_id)
        audit.record(conn, actor=actor, action="license.revoke",
                     target=cust.name if cust else str(lic.customer_id),
                     detail={"license_id": license_id, "note": note})
    return ok


# ---- the feature-access matrix (checkbox grid) ------------------------------

def _tier_rank(tier: str | None, *, unknown: int = -1) -> int:
    """Rank in the ordered ``models.TIERS`` tuple (basic < gold < platinum)."""
    from .models import TIERS
    return TIERS.index(tier) if tier in TIERS else unknown


def feature_matrix(active_doc: dict | None) -> dict:
    """The per-customer feature checkboxes, derived from the ONE canonical
    registry (``maverick.entitlements.GATED_FEATURES`` / ``GATED_SUITES``) plus
    the customer's active license doc. Returns::

        {"tier": "gold" | None,
         "features": [{"name", "min_tier", "enabled", "source"}, ...],
         "suites":   [{"name", "enabled"}, ...],
         "extra":    ["custom_feature", ...]}   # grants outside the registry

    ``source`` explains WHY a box is checked/unchecked: ``tier`` (included in
    the tier), ``grant`` (à-la-carte add), ``denied`` (explicitly off despite
    tier), or ``off`` (below tier, not granted). The issue form posts the same
    checkbox names back and :func:`grants_from_checkboxes` turns them into the
    minimal ``features``/``features_denied`` lists for the new license."""
    doc = active_doc or {}
    tier = doc.get("tier")
    rank = _tier_rank(tier)
    granted = set(doc.get("features") or [])
    denied = set(doc.get("features_denied") or [])
    lic_suites = set(doc.get("suites") or [])
    features = []
    for name, min_tier in sorted(entitlements.GATED_FEATURES.items()):
        in_tier = rank >= _tier_rank(min_tier, unknown=99)
        if name in denied:
            enabled, source = False, "denied"
        elif in_tier:
            enabled, source = True, "tier"
        elif name in granted:
            enabled, source = True, "grant"
        else:
            enabled, source = False, "off"
        features.append({"name": name, "min_tier": min_tier,
                         "enabled": enabled, "source": source})
    suites = [{"name": s, "enabled": s in lic_suites}
              for s in sorted(entitlements.GATED_SUITES)]
    extra = sorted(granted - set(entitlements.GATED_FEATURES))
    return {"tier": tier, "features": features, "suites": suites, "extra": extra}


def grants_from_checkboxes(tier: str, checked: list[str],
                           extra: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Turn the posted checkbox state into minimal license lists.

    For each registry feature: checked below tier → explicit grant; unchecked
    within tier → explicit denial; otherwise the tier already says it. ``extra``
    passes through unregistered custom grants unchanged. Returns
    ``(features, features_denied)``."""
    rank = _tier_rank(tier)
    on = set(checked)
    grants, denials = list(extra or []), []
    for name, min_tier in sorted(entitlements.GATED_FEATURES.items()):
        in_tier = rank >= _tier_rank(min_tier, unknown=99)
        if name in on and not in_tier:
            grants.append(name)
        elif name not in on and in_tier:
            denials.append(name)
    return grants, denials


# ---- resolve what to serve / display ---------------------------------------

def active_license_doc(conn: sqlite3.Connection, customer_id: int) -> dict | None:
    """The signed doc a connected deployment should currently run: the newest
    non-revoked license. A just-expired license is still served (the customer's
    grace window + renewal path); the running deployment resolves expiry itself
    via :func:`maverick.entitlements.resolve`."""
    for lic in store.list_licenses(conn, customer_id):  # newest first
        if not lic.revoked:
            return lic.doc
    return None


def display_status(lic: License, *, now: float | None = None) -> str:
    """UI status for a license row: revoked | active | grace | expired |
    perpetual — computed from the stored fields (no trust anchor needed)."""
    import time
    if lic.revoked:
        return "revoked"
    if not lic.expires_at:
        return "perpetual"
    exp = _dt.datetime.fromisoformat(lic.expires_at.replace("Z", "+00:00"))
    t = _dt.datetime.fromtimestamp(now if now is not None else time.time(),
                                   _dt.timezone.utc)
    if t <= exp:
        return "active"
    if t <= exp + _dt.timedelta(days=lic.grace_days):
        return "grace"
    return "expired"
