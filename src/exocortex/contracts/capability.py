from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AgentCapability(BaseModel):
    # Capabilities are declared per-adapter in code, not discovered at runtime.
    # Conformance tests verify the adapter actually delivers what it declares.
    schema_version: Literal[1] = 1

    agent_id: str
    kind: Literal["bridge", "runner"]

    edit_files: bool = False
    run_shell: bool = False
    long_context: bool = False
    structured_output: bool = False
    mcp_client: bool = False
    mcp_server: bool = False
    interactive: bool = False
    batch: bool = False
