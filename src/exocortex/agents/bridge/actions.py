from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InvokeTool:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    plan_b: str = ""


@dataclass(frozen=True)
class WriteMemory:
    content: str
    durable: bool = False
    type: str = "observation"


@dataclass(frozen=True)
class NoteDecision:
    summary: str
    rationale: str


@dataclass(frozen=True)
class RaiseQuestion:
    question: str


@dataclass(frozen=True)
class RequestHandoff:
    to_agent: str
    expected_output: str = ""


@dataclass(frozen=True)
class TaskDone:
    success: bool = True


AgentAction = (
    InvokeTool
    | WriteMemory
    | NoteDecision
    | RaiseQuestion
    | RequestHandoff
    | TaskDone
)
