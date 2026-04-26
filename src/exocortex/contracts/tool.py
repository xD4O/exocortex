from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from exocortex.contracts.common import ApprovalState, PolicyDecision, new_id, now


class Provenance(BaseModel):
    agent_id: str
    task_id: UUID
    session_id: UUID | None = None


class ToolInvocation(BaseModel):
    schema_version: Literal[1] = 1

    id: UUID = Field(default_factory=new_id)
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    workspace_ref: str | None = None  # worktree branch / path

    policy_decision: PolicyDecision | None = None
    approval_state: ApprovalState = ApprovalState.PROPOSED

    result: dict[str, Any] | None = None

    timestamp: datetime = Field(default_factory=now)
