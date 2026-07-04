from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from exocortex.contracts.common import PolicyDecision, new_id, now


class EventKind(StrEnum):
    TASK_CREATED = "task.created"
    TASK_STATUS_CHANGED = "task.status_changed"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"

    SESSION_OPENED = "session.opened"
    SESSION_STATUS_CHANGED = "session.status_changed"
    SESSION_CLOSED = "session.closed"

    MEMORY_WRITTEN = "memory.written"
    MEMORY_READ = "memory.read"
    MEMORY_FORGOTTEN = "memory.forgotten"
    MEMORY_MERGED = "memory.merged"
    MEMORY_CHAT = "memory.chat"
    MEMORY_PROMOTED = "memory.promoted"

    # User-profile lifecycle (USER-scope records)
    PROFILE_OBSERVED = "profile.observed"
    PROFILE_REDACTED = "profile.redacted"
    PROFILE_FROZEN_TOGGLED = "profile.frozen_toggled"
    PROFILE_QUESTIONED = "profile.questioned"
    PROFILE_ANSWERED = "profile.answered"

    TOOL_PROPOSED = "tool.proposed"
    TOOL_POLICY_CHECKED = "tool.policy_checked"
    TOOL_APPROVED = "tool.approved"
    TOOL_REJECTED = "tool.rejected"
    TOOL_EXECUTED = "tool.executed"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"

    HANDOFF_INITIATED = "handoff.initiated"
    HANDOFF_ACCEPTED = "handoff.accepted"

    # Dispatch lifecycle (pre-task-creation failures get captured here too,
    # so the audit log has full coverage of every dispatch attempt — including
    # the ones that never produce a Task).
    DISPATCH_FAILED = "dispatch.failed"
    DISPATCH_FALLBACK = "dispatch.fallback"

    # Multi-agent conversations — N agents exchange messages in a room.
    CONVERSATION_OPENED = "conversation.opened"
    CONVERSATION_TURN = "conversation.turn"
    CONVERSATION_CLOSED = "conversation.closed"
    CONVERSATION_DELETED = "conversation.deleted"


class Event(BaseModel):
    # Events are append-only and fully timestamped. This discipline is what
    # makes precog trace (CLAUDE-PLAN.MD §5.4) reconstructible.
    schema_version: Literal[1] = 1

    id: UUID = Field(default_factory=new_id)
    kind: EventKind
    timestamp: datetime = Field(default_factory=now)

    task_id: UUID | None = None
    session_id: UUID | None = None
    agent_id: str | None = None

    # Chain-of-custody + causality, promoted from the untyped payload (C1).
    # `agent_id` is the emitter (often the platform, e.g. "exocortex"); `actor`
    # is the agent that actually performed the work, so WHO/WHY/ORDER can be
    # reconstructed without payload spelunking and survive sloppy producers.
    actor: str | None = None
    parent_task_id: UUID | None = None
    caused_by_event_id: UUID | None = None
    reason: str | None = None

    payload: dict[str, Any] = Field(default_factory=dict)
    policy_decision: PolicyDecision | None = None
