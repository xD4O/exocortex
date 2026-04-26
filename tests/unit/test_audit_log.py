from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.contracts import Event, EventKind
from exocortex.observability.audit import AuditLog


@pytest.mark.asyncio
async def test_audit_log_roundtrip(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    events = [
        Event(kind=EventKind.TASK_CREATED, payload={"goal": "a"}),
        Event(kind=EventKind.TASK_STATUS_CHANGED, payload={"to": "routed"}),
        Event(kind=EventKind.SESSION_OPENED, agent_id="codex"),
    ]
    for ev in events:
        await log.record(ev)

    restored = await log.read_all()
    assert len(restored) == 3
    assert [e.kind for e in restored] == [e.kind for e in events]
    assert [e.id for e in restored] == [e.id for e in events]


@pytest.mark.asyncio
async def test_read_all_returns_empty_when_log_missing(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "does-not-exist.jsonl")
    assert await log.read_all() == []
