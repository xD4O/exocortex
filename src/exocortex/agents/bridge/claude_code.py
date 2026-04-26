from __future__ import annotations

from exocortex.agents.bridge.base import Bridge
from exocortex.contracts import AgentCapability


class ClaudeCodeBridge(Bridge):
    """Wraps Claude Code (MCP client AND host — exposes its own tools via MCP,
    and consumes our MCP server) as a Bridge-shaped adapter.

    Differs from CodexBridge only in capability declaration; the runtime
    lifecycle is identical, which is what Bet B promised.
    """

    def capability(self) -> AgentCapability:
        return AgentCapability(
            agent_id=self.agent_id,
            kind="bridge",
            edit_files=True,
            run_shell=True,
            long_context=True,
            structured_output=True,
            mcp_client=True,
            mcp_server=True,
            interactive=True,
            batch=False,
        )
