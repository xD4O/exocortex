from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from exocortex.contracts import PolicyDecisionKind


class ConditionKind(StrEnum):
    TOOL_EQUALS = "tool_equals"
    TOOL_IN = "tool_in"
    ARG_EQUALS = "arg_equals"
    ARG_CONTAINS_ANY = "arg_contains_any"
    PATH_ARG_UNDER_WORKSPACE = "path_arg_under_workspace"
    CWD_UNDER_WORKSPACE = "cwd_under_workspace"


class Condition(BaseModel):
    kind: ConditionKind
    arg: str | None = None
    value: str | None = None
    values: list[str] = Field(default_factory=list)


class Rule(BaseModel):
    # Rules are data, not code. Evaluated top-to-bottom; first matching rule
    # with stop=True wins. If none match, DeclarativeRuleEngine returns the
    # engine's default_outcome. See CLAUDE-PLAN.MD §Bet D.
    schema_version: Literal[1] = 1

    id: str
    description: str = ""
    conditions: list[Condition] = Field(default_factory=list)
    outcome: PolicyDecisionKind
    stop: bool = True
