"""Per-role credit tracking for counterfactual swarm routing.

Counterfactual swarm credit (``maverick.credit``) tells us, after each fan-out,
which sub-agent *roles* actually moved the answer. This module accumulates that
signal across runs so the orchestrator can prefer the roles that historically
contribute and stop spawning the ones that ride along adding nothing. It's the
routing consumer of CSCA (the donation record is the learning consumer).

In the firm posture every store is authenticated ciphertext under an exact
matter plus hashed-principal namespace. Reads re-resolve live authority and
have no domain, tenant, or global fallback. Missing context, revocation, and
authentication failures therefore yield no routing signal. Explicit legacy
mode retains the old global and department-scoped compatibility format.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import data_dir

log = logging.getLogger(__name__)

DEFAULT_PATH = data_dir("role_stats.json")
_lock = threading.Lock()

_SAFE_ROLE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_ROLE_LEN = 40


def safe_role(role: object) -> str | None:
    """Return a prompt-safe role key for routing memory, or None.

    Swarm roles are model-controlled.  Role stats are persisted across runs and
    later rendered into the orchestrator brief, so only compact identifier-like
    strings may enter this store.  Non-identifier runs are collapsed to hyphens
    and capped in length to avoid carrying prompt text across sessions.
    """
    if not isinstance(role, str):
        return None
    cleaned = _SAFE_ROLE_RE.sub("-", role.strip()).strip("-_").lower()
    if not cleaned:
        return None
    return cleaned[:_MAX_ROLE_LEN].rstrip("-_") or None


# Separator between the department tag and the role in a scoped stat key.
# "::" never appears in domain pack names (TOML stems) or role strings.
_SCOPE_SEP = "::"


def _safe_key(key: object) -> str | None:
    """Sanitize a possibly department-scoped stat key, preserving the scope.

    A scoped key is ``<domain>::<role>``; sanitize each segment with
    ``safe_role`` so model-controlled text can never enter the persisted store,
    while keeping the ``::`` separator intact so department lookups still work.
    """
    if not isinstance(key, str):
        return None
    if _SCOPE_SEP in key:
        domain, _, role = key.partition(_SCOPE_SEP)
        domain_key, role_key = safe_role(domain), safe_role(role)
        if domain_key is None or role_key is None:
            return None
        return f"{domain_key}{_SCOPE_SEP}{role_key}"
    return safe_role(key)


@dataclass
class RoleStat:
    runs: int = 0
    credit_sum: float = 0.0
    last: float = 0.0

    @property
    def avg_credit(self) -> float:
        return self.credit_sum / self.runs if self.runs else 0.0


def _resolve(path: Path | None) -> Path:
    if path is not None:
        return path
    return _tenant_path("role_stats.json", DEFAULT_PATH)


def _secure_execution() -> bool:
    try:
        from .security_defaults import secure_by_default

        return bool(secure_by_default())
    except Exception:
        return True


def _secure_scope_path() -> Path | None:
    """Resolve one live matter/principal namespace; no global fallback."""
    if not _secure_execution():
        return None
    try:
        from .file_lock import ensure_private_directory
        from .matter_context import refresh_matter_context

        context = refresh_matter_context()
        owner_scope = hashlib.sha256(context.principal.encode("utf-8")).hexdigest()
        root = data_dir(
            "role-stats",
            f"matter-{context.matter_id}",
            f"owner-{owner_scope}",
        )
        ensure_private_directory(root)
        return root / "role_stats.json"
    except Exception:
        return None


def _active_path(path: Path | None) -> Path | None:
    return _secure_scope_path() if _secure_execution() else _resolve(path)


def _tenant_path(name: str, legacy):
    """Item-30 isolation: with an ACTIVE tenant, this store lives under the
    tenant's data dir (one tenant's learned memory can never feed another's
    runs); single-tenant resolution keeps the legacy location unchanged."""
    try:
        from .paths import current_tenant, data_dir
        if current_tenant():
            return data_dir(*name.split("/"))
    except Exception:  # pragma: no cover -- isolation never blocks resolution
        pass
    return legacy



def _load(path: Path, *, strict: bool = False) -> dict[str, RoleStat]:
    if not path.exists():
        return {}
    try:
        from .learning_crypto import decode_text, protected_learning_enabled

        stored = path.read_text(encoding="utf-8")
        decoded = decode_text(stored)
        if decoded is None:
            if strict and protected_learning_enabled() and stored.strip():
                raise RuntimeError("role stats store authentication failed")
            return {}
        raw = json.loads(decoded)
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, RoleStat] = {}
    if not isinstance(raw, dict):
        return {}
    for role, entry in raw.items():
        role_key = _safe_key(role)
        if role_key is None or not isinstance(entry, dict):
            continue
        try:
            st = RoleStat(
                runs=int(entry.get("runs", 0)),
                credit_sum=float(entry.get("credit_sum", 0.0)),
                last=float(entry.get("last", 0.0)),
            )
        except (TypeError, ValueError):
            continue
        if role_key in out:
            prev = out[role_key]
            prev.runs += st.runs
            prev.credit_sum += st.credit_sum
            prev.last = max(prev.last, st.last)
        else:
            out[role_key] = st
    return out


def _save(stats: dict[str, RoleStat], path: Path) -> None:
    # Atomic temp+replace: a bare write_text truncates in place, so a concurrent
    # _load() reader sees a half-written file -> its JSONDecodeError is swallowed
    # as an empty store and all accumulated routing credit is silently discarded.
    from .file_lock import atomic_write_text
    from .learning_crypto import encode_text

    body = json.dumps({k: asdict(v) for k, v in stats.items()})
    atomic_write_text(path, encode_text(body))


def record(role: str, credit: float, path: Path | None = None, *,
           domain: str | None = None) -> None:
    """Accumulate one (role, marginal-credit) observation. Fail-safe no-op.

    Firm mode ignores caller paths/domains and records only after resolving the
    exact live matter/principal namespace. Legacy mode also writes a
    department-scoped key when ``domain`` is supplied.
    """
    role_key = safe_role(role)
    if role_key is None:
        return
    secure = _secure_execution()
    path = _active_path(path)
    if path is None:
        return
    keys = [role_key]
    if domain and not secure:
        domain_key = safe_role(domain)
        if domain_key:
            keys.append(f"{domain_key}{_SCOPE_SEP}{role_key}")
    # In-process lock + cross-process flock: this is a per-fan-out hot path hit
    # from multiple concurrent processes; without the flock two writers both
    # load the same store and the second save clobbers the first's credit.
    from .file_lock import cross_process_lock
    with _lock, cross_process_lock(path):
        try:
            stats = _load(path, strict=secure)
            for key in keys:
                st = stats.get(key) or RoleStat()
                st.runs += 1
                st.credit_sum += float(credit)
                st.last = time.time()
                stats[key] = st
            _save(stats, path)
        except (OSError, RuntimeError) as e:  # pragma: no cover
            log.debug("role_stats record failed: %s", e)


def record_credit(credit_by_name: dict[str, float], name_to_role: dict[str, str],
                  path: Path | None = None, *, domain: str | None = None) -> None:
    """Record a whole fan-out's credit, mapping agent names to their roles."""
    if not credit_by_name:
        return
    for name, c in credit_by_name.items():
        role = name_to_role.get(name)
        if role:
            record(role, c, path=path, domain=domain)


def top_roles(k: int = 5, *, min_runs: int = 2, path: Path | None = None,
              domain: str | None = None) -> list[tuple[str, float]]:
    """Roles ranked by average credit (only those with >= ``min_runs`` samples).

    Firm mode reads only the exact live matter/principal namespace. Legacy
    mode uses ``domain`` to select department-scoped compatibility entries.
    """
    secure = _secure_execution()
    resolved = _active_path(path)
    if resolved is None:
        return []
    stats = _load(resolved)
    # Keys are stored under safe_role(domain) (see record); sanitize the lookup
    # the same way so a domain needing normalization still matches its entries.
    domain_key = safe_role(domain) if domain and not secure else None
    prefix = f"{domain_key}{_SCOPE_SEP}" if domain_key else None
    ranked = []
    for key, st in stats.items():
        if st.runs < min_runs:
            continue
        if prefix is not None:
            if not key.startswith(prefix):
                continue
            ranked.append((key[len(prefix):], st.avg_credit))
        elif _SCOPE_SEP not in key:
            ranked.append((key, st.avg_credit))
    ranked.sort(key=lambda x: -x[1])
    return ranked[: max(1, k)]


def guidance(path: Path | None = None, *, domain: str | None = None) -> str | None:
    """A one-line brief addendum nudging toward high-credit roles, or None.

    Firm mode requires current matter authority and never falls back across a
    matter or principal. Legacy mode retains its department/global fallback.
    """
    try:
        from . import credit
        if not credit.enabled():
            return None
    except Exception:  # pragma: no cover
        return None
    if _secure_execution():
        top = top_roles(3, path=path)
        helpful = [role for role, value in top if value > 0]
        if not helpful:
            return None
        return (
            "Matter routing memory: these roles contributed most in prior "
            f"authorized runs — prefer them where they fit: {', '.join(helpful)}."
        )
    if domain:
        top = top_roles(3, path=path, domain=domain)
        helpful = [r for r, c in top if c > 0]
        if helpful:
            return (
                f"Routing memory ({domain}): these roles have contributed most "
                "on this department's past swarms — prefer them where they "
                f"fit: {', '.join(helpful)}."
            )
    top = top_roles(3, path=path)
    helpful = [r for r, c in top if c > 0]
    if not helpful:
        return None
    return (
        "Routing memory: these roles have contributed most on past swarms — "
        f"prefer them where they fit: {', '.join(helpful)}."
    )


__all__ = [
    "RoleStat",
    "safe_role",
    "record",
    "record_credit",
    "top_roles",
    "guidance",
    "DEFAULT_PATH",
]
