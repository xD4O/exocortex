"""Smoke: `build_mcp_server` registers the six expected tools. No protocol
round-trip here — that's tested end-to-end only when an agent is actually
configured against us."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exocortex.config import Settings
from exocortex.contracts import MemoryScope
from exocortex.memory.durable import DurableMemoryStore
from exocortex.operator.mcp.server import build_mcp_server

EXPECTED_TOOLS = {
    "session_startup",
    "dispatch_task",
    "dispatch_async",
    "dispatch_status",
    "dispatch_wait",
    "dispatch_cancel",
    "dispatch_batch",
    "memory_write",
    "memory_search",
    "memory_list",
    "memory_get",
    "memory_forget",
    "memory_dedup_clusters",
    "memory_merge",
    "memory_chat",
    "profile_observe",
    "profile_recall",
    "profile_freeze_toggle",
    "profile_questions",
    "profile_answer",
    "conversation_start",
    "conversation_turn",
    "conversation_inbox",
    "conversation_history",
    "conversation_close",
    "conversation_delete",
    "trace_recent",
    "agents_list",
    "fs_read",
    "fs_list",
    "shell_exec",
}


@pytest.mark.asyncio
async def test_build_registers_all_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOCORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXOCORTEX_AUDIT_LOG_PATH", str(tmp_path / "data/audit.jsonl"))
    monkeypatch.setenv("EXOCORTEX_MEMORY_DB_PATH", str(tmp_path / "data/memory.db"))

    settings = Settings()
    mcp = build_mcp_server(settings)
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS, f"missing/extra tools: {names ^ EXPECTED_TOOLS}"


@pytest.mark.asyncio
async def test_instructions_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOCORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXOCORTEX_AUDIT_LOG_PATH", str(tmp_path / "data/audit.jsonl"))
    monkeypatch.setenv("EXOCORTEX_MEMORY_DB_PATH", str(tmp_path / "data/memory.db"))
    mcp = build_mcp_server()
    assert mcp is not None


@pytest.mark.asyncio
async def test_memory_write_schema_has_literal_enums(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Literal types on scope + confidence should surface as JSON Schema
    `enum` arrays so agents can see allowed values directly."""
    monkeypatch.setenv("EXOCORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXOCORTEX_AUDIT_LOG_PATH", str(tmp_path / "data/audit.jsonl"))
    monkeypatch.setenv("EXOCORTEX_MEMORY_DB_PATH", str(tmp_path / "data/memory.db"))
    mcp = build_mcp_server()
    tools = await mcp.list_tools()
    write_tool = next(t for t in tools if t.name == "memory_write")
    schema = write_tool.inputSchema
    props = schema["properties"]
    assert "enum" in props["scope"], "scope should be enum-constrained"
    assert set(props["scope"]["enum"]) == {
        "session", "task", "project", "global",
    }
    assert "enum" in props["confidence"], "confidence should be enum-constrained"
    assert set(props["confidence"]["enum"]) == {
        "observed", "inferred", "asserted", "external_claim",
    }
    # All the params the walkthrough found missing should now be in the schema.
    for required_prop in ("scope", "scope_id", "confidence", "tags", "record_type"):
        assert required_prop in props, (
            f"{required_prop} missing from memory_write schema"
        )


@pytest.mark.asyncio
async def test_auto_capture_tools_record_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fs_read / fs_list / shell_exec should auto-write a memory record
    when invoked through MCP. We call the underlying FastMCP tool
    directly to avoid a stdio round-trip in tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("EXOCORTEX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("EXOCORTEX_AUDIT_LOG_PATH", str(data_dir / "audit.jsonl"))
    monkeypatch.setenv("EXOCORTEX_MEMORY_DB_PATH", str(data_dir / "memory.db"))

    target = tmp_path / "hello.txt"
    target.write_text("world", encoding="utf-8")

    mcp = build_mcp_server()
    result = await mcp.call_tool(
        "fs_read", {"path": str(target), "source": "hermes"}
    )
    # MCP call_tool returns (content_blocks, structured_result). Unpack safely.
    structured = None
    if isinstance(result, tuple) and len(result) >= 2:
        structured = result[1]
    else:
        # Some versions return just content list; pull the first text block.
        blocks = result if isinstance(result, list) else []
        if blocks:
            text = getattr(blocks[0], "text", "")
            try:
                structured = json.loads(text)
            except Exception:
                structured = {"content": text}
    assert structured is not None
    assert structured.get("content") == "world" or "world" in str(structured)

    # Auto-capture should have written to the durable store.
    store = DurableMemoryStore(data_dir / "memory.db")
    records = await store.list_by_scope(MemoryScope.PROJECT, "exocortex")
    assert any("fs_read" in r.content for r in records), (
        "fs_read did not auto-record"
    )
    assert any(r.source == "hermes" for r in records)
