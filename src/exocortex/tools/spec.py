from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolCategory(StrEnum):
    FS = "fs"
    SHELL = "shell"
    GIT = "git"
    WEB = "web"
    MCP = "mcp"


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    """Metadata + handler for a single tool. Shape maps cleanly to MCP Tool
    (name, description, inputSchema) so the same spec can be exposed to agents
    that consume our MCP server in Phase 4.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    category: ToolCategory
    risk_tier: RiskTier
    handler: ToolHandler

    def to_mcp_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
