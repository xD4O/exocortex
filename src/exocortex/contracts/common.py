from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def new_id() -> UUID:
    return uuid4()


def now() -> datetime:
    return datetime.now(tz=UTC)


class MemoryScope(StrEnum):
    SESSION = "session"
    TASK = "task"
    PROJECT = "project"
    GLOBAL = "global"
    # User-profile memory: facts about the operator themselves
    # (preferences, skills, goals, constraints, routines, etc.)
    USER = "user"


class Confidence(StrEnum):
    # Discrete; floats invite false precision (see CLAUDE-PLAN.MD §4).
    OBSERVED = "observed"
    INFERRED = "inferred"
    ASSERTED = "asserted"
    EXTERNAL_CLAIM = "external_claim"


class TaskStatus(StrEnum):
    PROPOSED = "proposed"
    ROUTED = "routed"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_HANDOFF = "awaiting_handoff"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class SessionStatus(StrEnum):
    OPENING = "opening"
    ACTIVE = "active"
    AWAITING_INPUT = "awaiting_input"
    HANDING_OFF = "handing_off"
    CLOSED = "closed"
    TERMINATED = "terminated"


class ApprovalState(StrEnum):
    PROPOSED = "proposed"
    POLICY_CHECKED = "policy_checked"
    APPROVED = "approved"
    AUTO_APPROVED = "auto_approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PolicyDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    DEGRADE = "degrade"


class ApprovalResolution(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"
    KILLED = "killed"


class Budget(BaseModel):
    tokens_limit: int | None = None
    wallclock_seconds: int | None = None
    approvals_limit: int | None = None
    dollars_limit: float | None = None


class PolicyDecision(BaseModel):
    kind: PolicyDecisionKind
    rule_id: str | None = None
    reason: str
    evaluated_at: datetime = Field(default_factory=now)
