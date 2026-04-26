from __future__ import annotations

from typing import Any

from exocortex.tools.errors import ToolNotFoundError
from exocortex.tools.spec import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as e:
            raise ToolNotFoundError(f"unknown tool: {name}") from e

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def to_mcp_tools(self) -> list[dict[str, Any]]:
        return [t.to_mcp_tool() for t in self.all()]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools
