"""Operator-CLI tests for the Phase-7 read-only views.

Tests shell out only through `typer.testing.CliRunner` — the CLI runs in-proc,
so we need env vars to point each command at an isolated data dir.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from exocortex.contracts import (
    Confidence,
    Event,
    EventKind,
    MemoryRecord,
    MemoryScope,
)
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.observability.audit import AuditLog
from exocortex.operator.cli import app


@pytest.fixture
def cli_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path]:
    data = tmp_path / "data"
    audit_path = data / "audit.jsonl"
    memory_path = data / "memory.db"
    monkeypatch.setenv("EXOCORTEX_DATA_DIR", str(data))
    monkeypatch.setenv("EXOCORTEX_AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setenv("EXOCORTEX_MEMORY_DB_PATH", str(memory_path))
    return audit_path, memory_path


def test_tools_command_lists_builtins(cli_env: tuple[Path, Path]) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 0, result.output
    for name in ("fs.read", "fs.write", "fs.list", "shell.exec"):
        assert name in result.output


def test_submit_and_ls_roundtrip(cli_env: tuple[Path, Path]) -> None:
    runner = CliRunner()
    r1 = runner.invoke(app, ["submit", "Refactor the auth middleware"])
    assert r1.exit_code == 0, r1.output
    assert "Created task" in r1.output

    r2 = runner.invoke(app, ["ls"])
    assert r2.exit_code == 0, r2.output
    assert "Refactor the auth middleware" in r2.output


async def _seed_audit(audit_path: Path, task_id: str) -> None:
    audit = AuditLog(audit_path)
    tid = UUID(task_id)
    await audit.record(
        Event(kind=EventKind.TASK_CREATED, task_id=tid, payload={"goal": "demo"})
    )
    await audit.record(
        Event(kind=EventKind.SESSION_OPENED, task_id=tid, agent_id="codex")
    )
    await audit.record(
        Event(
            kind=EventKind.TOOL_PROPOSED,
            task_id=tid,
            agent_id="codex",
            payload={"tool": "fs.read"},
        )
    )
    await audit.record(
        Event(
            kind=EventKind.HANDOFF_INITIATED,
            task_id=tid,
            agent_id="codex",
            payload={"to_agent": "claude_code"},
        )
    )


def test_trace_filters_to_single_task(cli_env: tuple[Path, Path]) -> None:
    audit_path, _ = cli_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    target = str(uuid4())
    other = str(uuid4())

    async def _seed() -> None:
        await _seed_audit(audit_path, target)
        await _seed_audit(audit_path, other)

    asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(app, ["trace", target[:8]])
    assert result.exit_code == 0, result.output
    assert target in result.output
    assert other not in result.output
    for kind in (
        "task.created",
        "session.opened",
        "tool.proposed",
        "handoff.initiated",
    ):
        assert kind in result.output


def test_trace_missing_task_exits_nonzero(cli_env: tuple[Path, Path]) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["trace", "deadbeef"])
    assert result.exit_code != 0


def test_memory_list_by_scope(cli_env: tuple[Path, Path]) -> None:
    _, memory_path = cli_env
    memory_path.parent.mkdir(parents=True, exist_ok=True)

    async def _seed() -> None:
        store = DurableMemoryStore(memory_path)
        emb = DeterministicEmbeddingProvider()
        for i in range(3):
            rec = MemoryRecord(
                type="observation",
                content=f"Record {i} about the auth flow",
                source="codex",
                confidence=Confidence.OBSERVED,
                scope=MemoryScope.TASK,
                scope_id="task-1",
            )
            await store.write(rec, embedding=emb.embed(rec.content))

    asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(
        app, ["memory", "list", "--scope", "task", "--scope-id", "task-1"]
    )
    assert result.exit_code == 0, result.output
    assert "Memory (3 records)" in result.output
    for i in range(3):
        assert f"Record {i}" in result.output


def test_memory_list_rejects_invalid_scope(cli_env: tuple[Path, Path]) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["memory", "list", "--scope", "nonsense", "--scope-id", "x"]
    )
    assert result.exit_code != 0


def test_memory_list_requires_scope_id_when_scope_given(
    cli_env: tuple[Path, Path],
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "list", "--scope", "task"])
    assert result.exit_code != 0


def test_memory_search_returns_hits(cli_env: tuple[Path, Path]) -> None:
    _, memory_path = cli_env
    memory_path.parent.mkdir(parents=True, exist_ok=True)

    async def _seed() -> None:
        store = DurableMemoryStore(memory_path)
        emb = DeterministicEmbeddingProvider()
        for content in (
            "authentication middleware rewrite",
            "memory summarizer compression",
            "completely unrelated cat fact",
        ):
            rec = MemoryRecord(
                type="observation",
                content=content,
                source="codex",
                confidence=Confidence.OBSERVED,
                scope=MemoryScope.PROJECT,
                scope_id="exocortex",
            )
            await store.write(rec, embedding=emb.embed(rec.content))

    asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(
        app, ["memory", "search", "authentication", "--alpha", "1.0"]
    )
    assert result.exit_code == 0, result.output
    assert "authentication middleware rewrite" in result.output


def test_memory_show_displays_full_content(
    cli_env: tuple[Path, Path],
) -> None:
    _, memory_path = cli_env
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    rec = MemoryRecord(
        type="decision",
        content="Chose SQLite over Postgres for single-operator MVP.",
        source="operator",
        confidence=Confidence.ASSERTED,
        scope=MemoryScope.PROJECT,
        scope_id="exocortex",
    )

    async def _seed() -> None:
        store = DurableMemoryStore(memory_path)
        await store.write(rec)

    asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(app, ["memory", "show", str(rec.id)])
    assert result.exit_code == 0, result.output
    assert "Chose SQLite over Postgres" in result.output


def test_memory_show_rejects_invalid_uuid(cli_env: tuple[Path, Path]) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "show", "not-a-uuid"])
    assert result.exit_code != 0


def test_help_text_lists_new_commands(cli_env: tuple[Path, Path]) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("submit", "ls", "ps", "tail", "trace", "memory", "tools"):
        assert cmd in result.output
