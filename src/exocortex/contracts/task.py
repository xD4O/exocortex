from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from exocortex.contracts.common import Budget, TaskStatus, new_id, now


class Task(BaseModel):
    schema_version: Literal[1] = 1

    id: UUID = Field(default_factory=new_id)
    goal: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    owner: str | None = None
    status: TaskStatus = TaskStatus.PROPOSED
    outputs: dict[str, Any] = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)

    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
