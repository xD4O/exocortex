from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from exocortex.contracts.common import SessionStatus, new_id, now


class Session(BaseModel):
    schema_version: Literal[1] = 1

    id: UUID = Field(default_factory=new_id)
    task_id: UUID
    agent_id: str
    status: SessionStatus = SessionStatus.OPENING
    worktree_path: str | None = None

    started_at: datetime = Field(default_factory=now)
    ended_at: datetime | None = None
