from __future__ import annotations


class ToolError(Exception):
    """Base for tool-execution failures."""


class ToolNotFoundError(ToolError):
    pass


class ToolTimeoutError(ToolError):
    pass


class ToolArgumentError(ToolError):
    pass


class PolicyViolationError(ToolError):
    """Raised when a tool is invoked despite a deny decision."""
