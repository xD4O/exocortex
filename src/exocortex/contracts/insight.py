from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from exocortex.contracts.common import Confidence, new_id, now


class InsightKind(StrEnum):
    CONTRADICTION = "contradiction"
    PATTERN = "pattern"
    GAP = "gap"
    SYNTHESIS = "synthesis"


class SuggestedAction(BaseModel):
    type: Literal["supersede", "create_rule", "track_gap",
                  "record_decision", "none"] = "none"
    stale_record_id: UUID | None = None      # supersede
    rule: dict[str, Any] | None = None        # create_rule (a Rule literal)
    question: str | None = None               # track_gap
    dimension: str | None = None              # track_gap
    content: str | None = None                # record_decision


class Insight(BaseModel):
    schema_version: Literal[1] = 1
    id: UUID = Field(default_factory=new_id)
    kind: InsightKind
    title: str
    detail: str
    refs: list[UUID] = Field(min_length=1)   # grounding is mandatory
    suggested_action: SuggestedAction = Field(default_factory=SuggestedAction)
    confidence: Confidence = Confidence.INFERRED
    reflection_id: UUID
    created_at: datetime = Field(default_factory=now)
