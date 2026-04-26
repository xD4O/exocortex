from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from exocortex.contracts.common import ApprovalResolution, new_id, now


class ApprovalRequest(BaseModel):
    # Approvals are first-class audited entities, not prompts (CLAUDE-PLAN.MD §4).
    # Every request must include `plan_b` — the deny-preview (§5.3c) — so the
    # operator sees what the agent will do instead of being denied.
    schema_version: Literal[1] = 1

    id: UUID = Field(default_factory=new_id)
    invocation_id: UUID
    reason_from_agent: str
    plan_b: str
    redacted_context: str
    allowed_duration_seconds: int
    kill_switch_armed: bool = True

    created_at: datetime = Field(default_factory=now)
    resolved_at: datetime | None = None
    resolution: ApprovalResolution | None = None
