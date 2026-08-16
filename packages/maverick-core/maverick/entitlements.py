"""Signed-license entitlements — control which features/tiers a deployment runs.

A **license** is an Ed25519-signed JSON document issued by Daybreak Labs that
declares a customer's edition, pricing **tier** (basic / gold / platinum), and
enabled add-on **suites** (e.g. ``fleet``). It is verified **offline** at load
time against the publisher's trusted public key — so an **air-gapped** bank can
be upgraded (platform → Gold, or + Fleet packs) by dropping in a new signed
license file, **no network required**. A connected deployment can additionally
poll an entitlement API, but the signed file is the floor that works everywhere.

Design (kernel rule 1 ethos — *fail open on the core*):

- The **core kernel never hard-stops** on a missing, expired, or invalid
  license. It falls back to the base tier with a warning and keeps running —
  bricking a bank mid-workflow is a worse failure than an unpaid add-on.
- Only **paid add-ons** (Gold/Platinum features, add-on suites like ``fleet``)
  gate **off** when unlicensed. Revenue is protected without ever stopping the
  work a customer already depends on.
- A **grace window** keeps a just-expired license fully live, so a renewal is
  never a fire drill.

Trust anchor: paid features activate **only** when the license is signed by a
key in the deployment's trusted set (``MAVERICK_LICENSE_PUBKEYS`` /
``[license] publisher_pubkeys``, or a key embedded in the shipped build). Absent
a trust anchor a self-signed license grants **nothing** beyond the base tier.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---- tier + capability registry (the single place features gate) -----------
#
# AXIS NOTE: this module is the **platform-tier** axis (Basic/Gold/Platinum),
# gated by a signed *deployment* license — one of the three axes in
# docs/product-portfolio.md ("Canonical naming, editions & SKU map"). It is
# distinct from :mod:`maverick.billing`, whose ``Entitlements``/``feature_allowed``
# gate the **per-tenant billing plan** (free/pro/enterprise) for a multi-tenant
# operator. Same word ("entitlements"), different axis — don't cross the imports.
# The two compose (a capability can be both tier-gated here and plan-gated there;
# e.g. SIEM export is a Gold+ tier capability per that doc AND an ``audit_export``
# billing-plan feature). Keep tier gates here; keep per-tenant plan gates there.

_TIER_RANK = {"basic": 0, "gold": 1, "platinum": 2}
BASE_TIER = "basic"
DEFAULT_GRACE_DAYS = 14

#: Feature name → minimum tier that unlocks it. Anything **not** listed here is
#: a *core* feature and is always allowed (fail-open).
#:
#: EMPTY ON PURPOSE. Upstream this gated paid add-ons behind an Ed25519 license
#: issued by the vendor: external_agents, fleet_governance, fleet_memory and
#: siem_export at "gold"; advanced_evolve, custom_pack_factory and multi_tenant
#: at "platinum". The firm owns this software outright, there is no vendor to
#: buy a tier from, and the console that minted those licenses is deleted -- so
#: every capability is a core capability here. Leaving the registry populated
#: would have let the firm's own platform refuse to run its own features.
#:
#: The machinery is retained rather than ripped out: it is load-bearing for the
#: expiry/grace/trust-anchor paths that other modules import, and an empty
#: registry expresses "nothing is gated" in the module's own terms.
GATED_FEATURES: dict[str, str] = {}

#: Add-on suites that require an explicit entitlement. Empty for the same
#: reason: there are no add-on suites to sell to ourselves.
GATED_SUITES: frozenset[str] = frozenset()

# Status values for a loaded license.
LICENSED = "licensed"   # valid + in date
GRACE = "grace"         # expired but within the grace window (still live)
EXPIRED = "expired"     # expired beyond grace (paid off, core on)
INVALID = "invalid"     # bad signature / untrusted key (paid off, core on)
UNVERIFIED = "unverified"  # unsigned or no trust anchor (paid off, core on)
UNLICENSED = "unlicensed"  # no license file (paid off, core on)

_PAID_ACTIVE = frozenset({LICENSED, GRACE})


# ---- helpers ---------------------------------------------------------------

def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _canon(payload: dict) -> bytes:
    """Canonical JSON for signing/verification: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _body(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in ("sig", "key_id")}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _license_cfg() -> dict:
    """The ``[license]`` config section (config knob — kernel rule 5)."""
    try:
        from .config import load_config
        return dict(load_config().get("license") or {})
    except Exception:  # pragma: no cover - config unavailable => env/embedded only
        return {}


def _default_license_path() -> Path:
    env = os.environ.get("MAVERICK_LICENSE_FILE")
    if env:
        return Path(env).expanduser()
    cfg_file = _license_cfg().get("license_file")
    if cfg_file:
        return Path(str(cfg_file)).expanduser()
    from . import paths
    return paths.maverick_home() / "license.json"


def _trusted_pubkeys(explicit: list[str] | None = None) -> list[str]:
    if explicit is not None:
        return [k.strip().lower() for k in explicit if k.strip()]
    keys: list[str] = []
    env = os.environ.get("MAVERICK_LICENSE_PUBKEYS", "")
    keys += [k.strip().lower() for k in env.split(",") if k.strip()]
    cfg_keys = _license_cfg().get("publisher_pubkeys") or []
    if isinstance(cfg_keys, str):
        cfg_keys = cfg_keys.split(",")
    keys += [str(k).strip().lower() for k in cfg_keys if str(k).strip()]
    keys += [k.lower() for k in _EMBEDDED_PUBKEYS]
    return keys


#: Publisher keys baked into the shipped build. Populated at release time with
#: Daybreak Labs' license public key(s) so paid features work with no config.
_EMBEDDED_PUBKEYS: tuple[str, ...] = ()


# ---- issuing (Daybreak side) + verification (deployment side) --------------

def new_keypair() -> tuple[str, str]:
    """Mint a fresh Ed25519 license keypair → (private_hex, public_hex)."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    priv = ed25519.Ed25519PrivateKey.generate()
    return priv.private_bytes_raw().hex(), priv.public_key().public_bytes_raw().hex()


def sign_license(payload: dict, private_key_hex: str) -> dict:
    """Sign a license payload with the publisher private key. Returns the full
    signed document (payload + ``sig`` + ``key_id`` = the signing public key)."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    body = _body(payload)
    sig = priv.sign(_canon(body)).hex()
    return {**body, "sig": sig, "key_id": priv.public_key().public_bytes_raw().hex()}


def verify_license(doc: dict, trusted_pubkeys: list[str]) -> tuple[bool, str]:
    """Verify a signed license against a trusted key set. Returns (ok, reason).

    A self-consistent signature is **not** enough — the signing key must be in
    ``trusted_pubkeys`` (the publisher key shipped with the build), otherwise
    anyone could self-sign a platinum license.

    Never raises: a non-dict ``doc`` (a hostile API returning a JSON array/
    string/number, or a corrupted license file that parses to valid-but-non-
    object JSON) returns ``(False, "malformed")`` rather than crashing the
    caller — the fail-open contract every chokepoint depends on.
    """
    from cryptography.hazmat.primitives.asymmetric import ed25519
    if not isinstance(doc, dict):
        return False, "malformed"
    sig = doc.get("sig")
    key_id = (doc.get("key_id") or "").lower()
    if not sig or not key_id:
        return False, "unsigned"
    if not trusted_pubkeys:
        return False, "no_trust_anchor"
    if key_id not in [k.lower() for k in trusted_pubkeys]:
        return False, "untrusted_key"
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_id))
        pub.verify(bytes.fromhex(sig), _canon(_body(doc)))
    except Exception:
        return False, "bad_signature"
    return True, "ok"


# ---- the entitlement gate --------------------------------------------------

@dataclass(frozen=True)
class Entitlements:
    """The resolved entitlement state of a deployment. Consult ``allows`` /
    ``suite_enabled`` / ``tier_at_least`` at every paid-feature chokepoint."""
    tier: str = BASE_TIER
    suites: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    features_denied: tuple[str, ...] = ()
    customer: str | None = None
    status: str = UNLICENSED
    expires_at: datetime | None = None
    reason: str = ""

    @property
    def paid_active(self) -> bool:
        return self.status in _PAID_ACTIVE

    def tier_at_least(self, tier: str) -> bool:
        return self.paid_active and _TIER_RANK.get(self.tier, 0) >= _TIER_RANK.get(tier, 99)

    def allows(self, feature: str) -> bool:
        """True iff ``feature`` may run. Core features (not in the registry and
        not an explicit paid grant or denial) always run — fail-open on the
        kernel. Within a live license the checkbox semantics are:

        - ``features`` grants a gated feature à la carte, even below its
          registry tier (e.g. Gold + ``advanced_evolve``).
        - ``features_denied`` switches a feature off even when the tier would
          include it (e.g. Gold minus ``siem_export``). Denial wins over grant.

        Both lists only bind while the license is paid-active (``resolve``
        drops them otherwise), so an expired/invalid license still fails open
        to the base tier — a denial can never outlive the license that made it.
        """
        required = GATED_FEATURES.get(feature)
        if (required is None and feature not in self.features
                and feature not in self.features_denied and feature not in GATED_SUITES):
            return True  # core capability
        if not self.paid_active:
            return False
        if feature in self.features_denied:
            return False  # explicit per-feature denial (checkbox off)
        if required is not None:
            return self.tier_at_least(required) or feature in self.features
        return feature in self.features  # explicit per-feature grant

    def suite_enabled(self, suite: str) -> bool:
        if suite not in GATED_SUITES:
            return True  # core suites always on
        return self.paid_active and suite in self.suites

    def summary(self) -> str:
        who = self.customer or "unlicensed"
        exp = self.expires_at.date().isoformat() if self.expires_at else "—"
        return f"{who} · {self.tier} · {self.status} · expires {exp}"


def resolve(doc: dict | None, *, trusted_pubkeys: list[str] | None = None,
            now: datetime | None = None, grace_days: int = DEFAULT_GRACE_DAYS) -> Entitlements:
    """Turn a (possibly ``None``/invalid) license doc into an Entitlements."""
    if doc is None:
        return Entitlements(status=UNLICENSED, reason="no license file")
    ok, why = verify_license(doc, _trusted_pubkeys(trusted_pubkeys))
    if not ok:
        status = INVALID if why in ("bad_signature", "untrusted_key") else UNVERIFIED
        return Entitlements(status=status, reason=why)
    try:
        exp = _parse_dt(doc.get("expires_at"))
        now = _now(now)
        status = LICENSED
        if exp is not None and now > exp:
            grace = timedelta(days=int(doc.get("grace_days", grace_days)))
            status = GRACE if now <= exp + grace else EXPIRED
    except (ValueError, TypeError):
        # A signed-but-malformed date/grace field (publisher fat-finger, e.g. a
        # unix-timestamp expires_at) must fail to base tier, not crash current().
        return Entitlements(status=INVALID, reason="malformed expiry",
                            customer=doc.get("customer"))
    live = status in _PAID_ACTIVE
    return Entitlements(
        tier=str(doc.get("tier", BASE_TIER)) if live else BASE_TIER,
        suites=tuple(doc.get("suites", [])) if live else (),
        features=tuple(doc.get("features", [])) if live else (),
        features_denied=tuple(doc.get("features_denied", [])) if live else (),
        customer=doc.get("customer"),
        status=status,
        expires_at=exp,
        reason=why,
    )


def load_entitlements(path: str | Path | None = None, *,
                      trusted_pubkeys: list[str] | None = None,
                      now: datetime | None = None) -> Entitlements:
    """Load + verify the license file (default ``~/.maverick/license.json``)."""
    p = Path(path) if path else _default_license_path()
    if not p.exists():
        return Entitlements(status=UNLICENSED, reason="no license file")
    try:
        doc = json.loads(p.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001 — any parse error => run core, warn
        return Entitlements(status=INVALID, reason=f"unreadable: {e}")
    return resolve(doc, trusted_pubkeys=trusted_pubkeys, now=now)


# ---- process-wide cache (cheap to consult at chokepoints) ------------------

_CACHE: Entitlements | None = None


def current(refresh: bool = False) -> Entitlements:
    global _CACHE
    if _CACHE is None or refresh:
        _CACHE = load_entitlements()
    return _CACHE


def reset_cache() -> None:
    global _CACHE
    _CACHE = None


# ---- chokepoint API (what paid features call) ------------------------------

def enforcing() -> bool:
    """True iff paid-feature enforcement is ON for this deployment. **Default
    OFF** — a dev/community box runs everything; enforcement is a deliberate
    production posture (``[license] enforce = true`` or
    ``MAVERICK_LICENSE_ENFORCE=1``). This is what keeps the fail-open promise:
    the gate never activates unless an operator turns it on."""
    from .config import env_flag
    v = env_flag("MAVERICK_LICENSE_ENFORCE")   # shared truthy parser (sibling of
    if v is not None:                          # fleet_memory's gate); None on an
        return v                               # unrecognized value → fall to config
    return bool(_license_cfg().get("enforce", False))


def require(feature: str) -> bool:
    """Chokepoint gate for a paid *feature*: allowed unless enforcement is on
    **and** the license doesn't grant it. Fail-open when not enforcing."""
    if not enforcing():
        return True
    return current().allows(feature)


def require_suite(suite: str) -> bool:
    """Chokepoint gate for a paid add-on *suite* (e.g. ``fleet``)."""
    if not enforcing():
        return True
    return current().suite_enabled(suite)


# ---- connected entitlement API (optional; the signed file is the floor) -----

DEFAULT_REFRESH_TIMEOUT = 10.0


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of a connected-API refresh — for logging/metering, never raised."""
    ok: bool
    reason: str
    status: str
    changed: bool = False


def _http_get_json(url: str, token: str | None, timeout: float) -> dict:
    """Minimal stdlib GET → JSON. No new top-level dep (kernel rule 5). The API
    returns a **signed** license doc, so the signature — not the transport — is
    the trust boundary: an attacker who MITMs the response still cannot forge a
    publisher signature, and :func:`resolve` rejects anything unverified.
    (urllib verifies TLS certs by default for https URLs.)"""
    import urllib.parse
    import urllib.request
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("https", "http"):
        # api_url is operator/config-controlled; refuse file://, ftp://, etc. so
        # a stray config value can't turn a license refresh into a local-file /
        # SSRF read. Prefer https; http is allowed for a trusted segment.
        raise ValueError(f"refusing non-http(s) entitlement API scheme: {scheme!r}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _rollback_reason(path: Path, new_doc: dict) -> str | None:
    """Refusal reason if persisting ``new_doc`` over the license already at
    ``path`` would be a **rollback or subject swap**, else ``None``.

    An attacker on the refresh path can only replay a *validly signed* older
    license (they can't forge one), so freshness — not the signature — is what
    stops a downgrade. Compares the stable subject (``customer``) and
    ``issued_at``; a missing/unreadable installed license means there is nothing
    to roll back from. ``license_id`` is deliberately **not** the subject key —
    it's regenerated on every issue, so a legitimate renewal has a new one."""
    if not path.exists():
        return None
    try:
        cur = json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable floor can always be replaced
        return None
    if not isinstance(cur, dict):
        return None
    cur_cust, new_cust = cur.get("customer"), new_doc.get("customer")
    if cur_cust and new_cust and cur_cust != new_cust:
        return "server license is for a different customer (refused)"
    try:
        cur_iss = _parse_dt(cur.get("issued_at"))
        new_iss = _parse_dt(new_doc.get("issued_at"))
    except (ValueError, TypeError):
        return None
    if cur_iss and new_iss and new_iss < cur_iss:
        return "server license is older than installed (rollback refused)"
    return None


def refresh_from_server(url: str | None = None, *, token: str | None = None,
                        trusted_pubkeys: list[str] | None = None,
                        save: bool = True, timeout: float | None = None,
                        fetch=None) -> RefreshResult:
    """Connected path: pull a fresh signed license from the entitlement API and,
    if it verifies, persist it so the **offline file stays the floor** for the
    next boot (an air-gapped box simply never calls this).

    Fail-open by contract (kernel rule 1): a missing URL, a network error, or an
    unverifiable response NEVER raises and NEVER changes the running entitlement
    — the last-known license file keeps the deployment live. Only a **verified,
    paid-active, and not-a-rollback** license is written, so a spoofed,
    downgraded, or replayed API reply cannot forge, strip, or roll back
    entitlements. ``fetch`` is injectable for tests."""
    cfg = _license_cfg()
    api_url = url or os.environ.get("MAVERICK_LICENSE_API") or cfg.get("api_url")
    if not api_url:
        return RefreshResult(False, "no entitlement API configured", current().status)
    # The token is a secret: resolve it through the secret provider (file/env
    # backends), the same path the SIEM bearer uses — not a raw os.environ read.
    from .secret_provider import get_secret
    api_token = token or get_secret("MAVERICK_LICENSE_API_TOKEN") or cfg.get("api_token")
    to = timeout if timeout is not None else DEFAULT_REFRESH_TIMEOUT
    getter = fetch or (lambda: _http_get_json(str(api_url), api_token, to))
    try:
        doc = getter()
    except Exception as e:  # noqa: BLE001 - network/parse failure must never brick
        return RefreshResult(False, f"fetch failed: {type(e).__name__}", current().status)
    ent = resolve(doc, trusted_pubkeys=trusted_pubkeys)
    if ent.status not in _PAID_ACTIVE:
        # Never overwrite a good local license with an unverifiable server reply.
        return RefreshResult(False, f"server license {ent.status}", current().status)
    changed = False
    if save:
        path = _default_license_path()
        rolled = _rollback_reason(path, doc)
        if rolled:
            return RefreshResult(False, rolled, current().status)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_text = json.dumps(doc, indent=2) + "\n"
            changed = (not path.exists()) or path.read_text("utf-8") != new_text
            path.write_text(new_text, encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - persistence is best-effort
            return RefreshResult(True, f"verified but not saved: {type(e).__name__}",
                                 ent.status, False)
    reset_cache()
    return RefreshResult(True, "ok", ent.status, changed)


# ---- background auto-refresh (near-instant entitlement propagation) --------
#
# The connected path above is pull-based; nothing in the kernel called it on a
# schedule, so a license issued in the vendor console only landed when someone
# ran a manual refresh. This loop closes that gap: a long-lived process (the
# dashboard, `maverick mcp`, …) calls start_refresher() once and the deployment
# then picks up upgrades/downgrades within one interval of the console issuing
# them — checkbox flipped at HQ, feature live at the client ~a minute later.
# Every poll also records a fleet check-in server-side, so the fleet board
# stays near-live for free.

DEFAULT_REFRESH_INTERVAL = 60.0   # seconds; a signed license is ~1 KB of JSON
_MIN_REFRESH_INTERVAL = 30.0      # floor so a config typo can't hammer the API

_REFRESHER_STOP = None            # threading.Event of the live refresher, else None
_REFRESHER_THREAD = None


def refresh_interval_seconds() -> float:
    """The configured auto-refresh interval (``[license] refresh_interval_seconds``
    / ``MAVERICK_LICENSE_REFRESH_INTERVAL``), clamped to a 30 s floor.

    Returns 0 (disabled) when no entitlement API is configured — an offline /
    air-gapped box has nothing to poll — or when the operator explicitly sets
    the interval to 0. Defaults to 60 s when an API is configured, because a
    connected deployment that opted into the entitlement API expects upgrades
    to propagate without a redeploy."""
    cfg = _license_cfg()
    api_url = os.environ.get("MAVERICK_LICENSE_API") or cfg.get("api_url")
    if not api_url:
        return 0.0
    raw = os.environ.get("MAVERICK_LICENSE_REFRESH_INTERVAL")
    if raw is None:
        raw = cfg.get("refresh_interval_seconds", DEFAULT_REFRESH_INTERVAL)
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_INTERVAL
    if interval <= 0:
        return 0.0
    return max(interval, _MIN_REFRESH_INTERVAL)


def start_refresher(interval: float | None = None, *, refresh=None):
    """Start the background license-refresh thread (idempotent; daemon).

    Returns the thread, or ``None`` when auto-refresh is disabled (no API
    configured / interval 0) or a refresher is already running. ``refresh`` is
    injectable for tests and defaults to :func:`refresh_from_server`, which
    never raises — so the loop can't die on a network blip, and a failed poll
    simply leaves the last-known-good license file in charge (fail-open)."""
    global _REFRESHER_STOP, _REFRESHER_THREAD
    if _REFRESHER_THREAD is not None and _REFRESHER_THREAD.is_alive():
        return None
    # An explicit interval argument is caller-controlled (tests use tiny ones);
    # config-sourced values are already floor-clamped in refresh_interval_seconds.
    every = float(interval) if interval is not None else refresh_interval_seconds()
    if every <= 0:
        return None
    import logging
    import threading
    log = logging.getLogger(__name__)
    stop = threading.Event()
    do_refresh = refresh or refresh_from_server

    def _loop() -> None:
        while not stop.wait(every):
            try:
                res = do_refresh()
            except Exception:  # noqa: BLE001 - belt+braces; the loop must survive
                log.debug("license refresh raised unexpectedly", exc_info=True)
                continue
            if getattr(res, "changed", False):
                reset_cache()
                log.info("entitlements updated from server: %s", current().summary())

    t = threading.Thread(target=_loop, name="maverick-license-refresh", daemon=True)
    _REFRESHER_STOP, _REFRESHER_THREAD = stop, t
    t.start()
    return t


def stop_refresher(timeout: float = 2.0) -> None:
    """Signal the refresher to exit and join it briefly (safe to call anytime)."""
    global _REFRESHER_STOP, _REFRESHER_THREAD
    if _REFRESHER_STOP is not None:
        _REFRESHER_STOP.set()
    if _REFRESHER_THREAD is not None and _REFRESHER_THREAD.is_alive():
        _REFRESHER_THREAD.join(timeout=timeout)
    _REFRESHER_STOP = _REFRESHER_THREAD = None


# ---- CLI: python -m maverick.entitlements {keygen,issue,verify,show} -------
# Self-contained so it needs no wiring into the main cli.py. `keygen`/`issue`
# are Daybreak-side (keep the private key secret); `verify`/`show` run on a
# deployment.

def main(argv: list[str] | None = None) -> int:  # pragma: no cover -- CLI shell
    import argparse
    import secrets

    p = argparse.ArgumentParser(prog="maverick.entitlements",
                                description="Issue/verify Maverick license files.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keygen", help="mint a publisher keypair (keep the private key secret)")

    iss = sub.add_parser("issue", help="sign a license (Daybreak side)")
    iss.add_argument("--customer", required=True)
    iss.add_argument("--tier", default="gold", choices=list(_TIER_RANK))
    iss.add_argument("--edition", default="enterprise")
    iss.add_argument("--suites", default="", help="comma-separated add-on suites, e.g. fleet")
    iss.add_argument("--features", default="", help="comma-separated explicit feature grants")
    iss.add_argument("--seats", type=int, default=None)
    iss.add_argument("--expires", default=None, help="YYYY-MM-DD (omit = perpetual)")
    iss.add_argument("--grace-days", type=int, default=DEFAULT_GRACE_DAYS)
    iss.add_argument("--key", required=True, help="publisher private key hex, or @path")
    iss.add_argument("-o", "--out", default="license.json")

    rf = sub.add_parser("refresh", help="one-shot connected refresh (cron/systemd-"
                                        "timer alternative to the in-process loop)")
    rf.add_argument("--url", default=None, help="entitlement API URL (default: config)")

    vf = sub.add_parser("verify", help="verify a license file")
    vf.add_argument("--file", default=None)
    vf.add_argument("--pubkey", default=None, help="comma-separated trusted pubkey hex")

    sh = sub.add_parser("show", help="show the resolved entitlements of a license")
    sh.add_argument("--file", default=None)
    sh.add_argument("--pubkey", default=None)

    args = p.parse_args(argv)

    if args.cmd == "keygen":
        priv, pub = new_keypair()
        print(f"private_key (SECRET): {priv}")
        print(f"public_key  (ship):   {pub}")
        return 0

    if args.cmd == "issue":
        key = args.key
        if key.startswith("@"):
            key = Path(key[1:]).expanduser().read_text("utf-8").strip()
        exp = None
        if args.expires:
            exp = datetime.fromisoformat(args.expires).replace(tzinfo=timezone.utc)\
                .isoformat().replace("+00:00", "Z")
        payload = {
            "customer": args.customer, "edition": args.edition, "tier": args.tier,
            "suites": [s.strip() for s in args.suites.split(",") if s.strip()],
            "features": [f.strip() for f in args.features.split(",") if f.strip()],
            "seats": args.seats,
            "issued_at": _now().isoformat().replace("+00:00", "Z"),
            "expires_at": exp, "grace_days": args.grace_days,
            "license_id": "lic_" + secrets.token_hex(8),
        }
        doc = sign_license(payload, key)
        Path(args.out).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}  ({args.customer} · {args.tier})  key_id={doc['key_id'][:16]}…")
        return 0

    if args.cmd == "refresh":
        res = refresh_from_server(args.url)
        print(f"{'ok' if res.ok else 'FAILED'}: {res.reason} · status={res.status}"
              f"{' · updated' if res.changed else ''}")
        return 0 if res.ok else 1

    # verify / show
    trust = [k.strip() for k in args.pubkey.split(",")] if args.pubkey else None
    ent = load_entitlements(args.file, trusted_pubkeys=trust)
    print(ent.summary())
    if args.cmd == "show":
        print(f"  paid features active: {ent.paid_active}")
        print(f"  suites: {list(ent.suites) or '—'}")
        for feat in sorted(GATED_FEATURES):
            print(f"    {'✓' if ent.allows(feat) else '·'} {feat}")
    return 0 if ent.status in (LICENSED, GRACE) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

