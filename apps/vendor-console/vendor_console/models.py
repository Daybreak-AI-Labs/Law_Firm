"""Typed views over the SQLite rows — plain dataclasses, no ORM."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

ROLES = ("owner", "admin", "support", "viewer")
POSTURES = ("connected", "vpc", "airgapped")
CUSTOMER_STATUSES = ("trial", "active", "suspended", "churned")
TIERS = ("basic", "gold", "platinum")
CHANNELS = ("stable", "edge")
TICKET_STATUSES = ("open", "in_progress", "waiting", "resolved", "closed")
TICKET_PRIORITIES = ("low", "normal", "high", "urgent")

#: Roles allowed to mint/revoke licenses, publish releases, and mutate customers.
WRITE_ROLES = frozenset({"owner", "admin"})
#: Roles allowed to work the support desk (own/triage tickets). Adds support.
SUPPORT_ROLES = frozenset({"owner", "admin", "support"})


@dataclass
class Staff:
    id: int
    email: str
    name: str
    role: str
    totp_enrolled: bool
    disabled: bool
    created_at: float
    session_epoch: int = 0

    @classmethod
    def from_row(cls, r) -> Staff:
        return cls(id=r["id"], email=r["email"], name=r["name"], role=r["role"],
                   totp_enrolled=bool(r["totp_enrolled"]), disabled=bool(r["disabled"]),
                   created_at=r["created_at"], session_epoch=r["session_epoch"])

    @property
    def can_write(self) -> bool:
        return self.role in WRITE_ROLES

    @property
    def can_support(self) -> bool:
        return self.role in SUPPORT_ROLES


@dataclass
class Customer:
    id: int
    name: str
    primary_contact: str
    contact_email: str
    posture: str
    status: str
    notes: str
    created_at: float
    updated_at: float
    channel: str = "stable"

    @classmethod
    def from_row(cls, r) -> Customer:
        keys = r.keys()
        return cls(id=r["id"], name=r["name"], primary_contact=r["primary_contact"],
                   contact_email=r["contact_email"], posture=r["posture"],
                   status=r["status"], notes=r["notes"],
                   created_at=r["created_at"], updated_at=r["updated_at"],
                   channel=r["channel"] if "channel" in keys else "stable")


@dataclass
class License:
    id: int
    customer_id: int
    license_id: str
    tier: str
    suites: list[str]
    features: list[str]
    seats: int | None
    issued_at: str
    expires_at: str | None
    grace_days: int
    key_id: str
    doc: dict
    revoked_at: float | None
    note: str
    created_by: str
    created_at: float

    @classmethod
    def from_row(cls, r) -> License:
        return cls(
            id=r["id"], customer_id=r["customer_id"], license_id=r["license_id"],
            tier=r["tier"], suites=json.loads(r["suites_json"]),
            features=json.loads(r["features_json"]), seats=r["seats"],
            issued_at=r["issued_at"], expires_at=r["expires_at"],
            grace_days=r["grace_days"], key_id=r["key_id"],
            doc=json.loads(r["doc_json"]), revoked_at=r["revoked_at"],
            note=r["note"], created_by=r["created_by"], created_at=r["created_at"])

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass
class AuditEvent:
    id: int
    ts: float
    actor: str
    action: str
    target: str
    detail: dict = field(default_factory=dict)
    prev_hash: str = ""
    hash: str = ""

    @classmethod
    def from_row(cls, r) -> AuditEvent:
        return cls(id=r["id"], ts=r["ts"], actor=r["actor"], action=r["action"],
                   target=r["target"], detail=json.loads(r["detail_json"]),
                   prev_hash=r["prev_hash"], hash=r["hash"])


@dataclass
class Release:
    id: int
    version: str
    channel: str
    min_from: str
    notes: str
    migrations: list[str]
    artifacts: list[dict]
    manifest: dict
    key_id: str
    published_by: str
    yanked_at: float | None
    created_at: float

    @classmethod
    def from_row(cls, r) -> Release:
        return cls(
            id=r["id"], version=r["version"], channel=r["channel"],
            min_from=r["min_from"], notes=r["notes"],
            migrations=json.loads(r["migrations_json"]),
            artifacts=json.loads(r["artifacts_json"]),
            manifest=json.loads(r["manifest_json"]), key_id=r["key_id"],
            published_by=r["published_by"], yanked_at=r["yanked_at"],
            created_at=r["created_at"])

    @property
    def yanked(self) -> bool:
        return self.yanked_at is not None


@dataclass
class Ticket:
    id: int
    customer_id: int | None
    correlation_id: str
    subject: str
    status: str
    priority: str
    tier: str
    agent_version: str
    summary: dict
    bundle: dict
    assignee: str
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, r) -> Ticket:
        return cls(
            id=r["id"], customer_id=r["customer_id"],
            correlation_id=r["correlation_id"], subject=r["subject"],
            status=r["status"], priority=r["priority"], tier=r["tier"],
            agent_version=r["agent_version"], summary=json.loads(r["summary_json"]),
            bundle=json.loads(r["bundle_json"]), assignee=r["assignee"],
            created_at=r["created_at"], updated_at=r["updated_at"])

    @property
    def open(self) -> bool:
        return self.status not in ("resolved", "closed")


@dataclass
class TicketComment:
    id: int
    ticket_id: int
    author: str
    body: str
    kind: str
    created_at: float

    @classmethod
    def from_row(cls, r) -> TicketComment:
        return cls(id=r["id"], ticket_id=r["ticket_id"], author=r["author"],
                   body=r["body"], kind=r["kind"], created_at=r["created_at"])
