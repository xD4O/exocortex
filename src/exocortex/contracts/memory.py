from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from exocortex.contracts.common import Confidence, MemoryScope, new_id, now


class MemoryRecord(BaseModel):
    schema_version: Literal[1] = 1

    id: UUID = Field(default_factory=new_id)
    type: str
    content: str
    source: str  # agent_id, "operator", or tool_id
    confidence: Confidence
    scope: MemoryScope
    scope_id: str  # session_id | task_id | project_id | "global"
    tags: list[str] = Field(default_factory=list)
    ttl_seconds: int | None = None

    timestamp: datetime = Field(default_factory=now)
