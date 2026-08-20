"""Bounded request and response models for the firm REST API."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class SignoffIn(BaseModel):
    """Attorney decision bound to the exact deliverable version reviewed."""

    decision: str = Field(..., pattern="^(approved|rejected)$")
    expected_updated_at: float = Field(..., gt=0)
    note: str | None = Field(default=None, max_length=2000)


class ConflictPreflightIn(BaseModel):
    """Candidate names for an opaque firm-wide conflict check."""

    client_name: str | None = Field(default=None, max_length=200)
    adverse_parties: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _bounded_names(self) -> ConflictPreflightIn:
        if not (self.client_name or "").strip() and not self.adverse_parties:
            raise ValueError("at least one client or party name is required")
        if any(
            not str(name or "").strip() or len(str(name).strip()) > 200
            for name in self.adverse_parties
        ):
            raise ValueError("party names must be between 1 and 200 characters")
        return self


class GoalIn(BaseModel):
    title: str = Field(..., max_length=200)
    description: str = Field("", max_length=16000)
    max_dollars: float = Field(5.0, ge=0.0, le=100.0)
    max_wall_seconds: float = Field(3600.0, ge=1.0, le=86400.0)
    max_depth: int = Field(3, ge=1, le=5)
    template: str | None = None
    params: dict[str, str] | None = None
    domain: str | None = Field(None, max_length=120)
    project_id: int | None = Field(default=None, gt=0)


class GoalOut(BaseModel):
    id: int
    status: str
    title: str
    description: str | None = None
    result: str | None = None
    project_id: int | None = None


class GoalEventOut(BaseModel):
    id: int
    agent: str
    kind: str
    content: str
    ts: float


class GoalEventsResponse(BaseModel):
    status: str
    result: str | None
    next_id: int
    events: list[GoalEventOut]


class OutcomeIn(BaseModel):
    """A direct, matter-ACL-gated downstream outcome for local learning."""

    goal_id: int
    episode_id: int
    value: float
    kind: str = Field("", max_length=120)


class FeedbackIn(BaseModel):
    rating: str = Field(..., pattern="^(up|down)$")
    note: str | None = Field(default=None, max_length=2000)


class DeliverableEditIn(BaseModel):
    text: str = Field(..., max_length=200_000)


class AnswerIn(BaseModel):
    question_id: int
    answer: str = Field(..., max_length=16_000)


class AttachmentOut(BaseModel):
    id: int
    filename: str
    mime: str
    size_bytes: int
    sha256: str


class HaltIn(BaseModel):
    reason: str = Field("manual via dashboard", max_length=200)


class RetitleIn(BaseModel):
    title: str = Field(..., max_length=200)


class ChildIn(BaseModel):
    title: str = Field(..., max_length=200)
    description: str = Field("", max_length=16_000)


class ComposeIn(BaseModel):
    title: str = Field(..., max_length=200)
    project_id: int | None = Field(None, gt=0)
    domain: str | None = Field(None, max_length=120)
    steps: list[str] = Field(default_factory=list, max_length=50)
    budget_dollars: float | None = Field(None, ge=0.0, le=100.0)
    channel: str | None = Field(None, max_length=64)
    priority: str | None = Field(None, max_length=16)
