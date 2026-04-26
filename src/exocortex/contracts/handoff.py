from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from exocortex.contracts.common import Budget, new_id, now


class WorkspaceState(BaseModel):
    repo_ref: str  # commit SHA
    branch: str
    worktree_path: str
    untracked_manifest: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    summary: str
    rationale: str
    memory_record_id: UUID | None = None
    at: datetime = Field(default_factory=now)


class ToolInvocationCursor(BaseModel):
    pending_ids: list[UUID] = Field(default_factory=list)
    rejected_ids: list[UUID] = Field(default_factory=list)
    completed_ids: list[UUID] = Field(default_factory=list)


class Handoff(BaseModel):
    # The central primitive (CLAUDE-PLAN.MD Bet C). If a subsystem cannot
    # populate its field deterministically, that subsystem has a design bug.
    schema_version: Literal[1] = 1

    id: UUID = Field(default_factory=new_id)
    task_id: UUID
    from_agent: str
    to_agent: str
    sequence_no: int

    goal_restatement: str  # explicit; forces compression, not inheritance
    constraints_active: list[str] = Field(default_factory=list)
    decisions_so_far: list[Decision] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    workspace_state: WorkspaceState | None = None
    tool_invocation_cursor: ToolInvocationCursor = Field(default_factory=ToolInvocationCursor)
    memory_scope_ids: list[str] = Field(default_factory=list)
    expected_output: str
    budget_remaining: Budget = Field(default_factory=Budget)

    created_at: datetime = Field(default_factory=now)
