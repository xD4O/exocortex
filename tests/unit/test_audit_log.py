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


@pytest.mark.asyncio
async def test_read_all_is_incremental(tmp_path: Path) -> None:
    """C2: a second read only parses the newly appended events, not the whole
    file again."""
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(3):
        await log.record(Event(kind=EventKind.TASK_CREATED, payload={"n": i}))
    assert len(await log.read_all()) == 3
    assert log._parses == 3  # noqa: SLF001

    for i in range(2):
        await log.record(Event(kind=EventKind.MEMORY_WRITTEN, payload={"n": i}))
    got = await log.read_all()
    assert len(got) == 5
    # Only the 2 new events were parsed on the second read (5 total, not 8).
    assert log._parses == 5  # noqa: SLF001


@pytest.mark.asyncio
async def test_read_all_picks_up_cross_instance_writes(tmp_path: Path) -> None:
    """A different AuditLog instance on the same file (web vs dispatch) still
    sees appended events via the byte-offset reconcile."""
    path = tmp_path / "audit.jsonl"
    reader = AuditLog(path)
    writer = AuditLog(path)
    await writer.record(Event(kind=EventKind.TASK_CREATED, payload={"n": 0}))
    assert len(await reader.read_all()) == 1
    await writer.record(Event(kind=EventKind.TASK_COMPLETED, payload={"n": 1}))
    assert len(await reader.read_all()) == 2


@pytest.mark.asyncio
async def test_read_all_resets_on_truncation(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(4):
        await log.record(Event(kind=EventKind.TASK_CREATED, payload={"n": i}))
    assert len(await log.read_all()) == 4
    # Simulate rotation: replace the file with a single fresh event.
    fresh = Event(kind=EventKind.SESSION_OPENED, agent_id="codex")
    path.write_text(fresh.model_dump_json() + "\n", encoding="utf-8")
    got = await log.read_all()
    assert len(got) == 1
    assert got[0].id == fresh.id


@pytest.mark.asyncio
async def test_read_all_ignores_partial_trailing_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    ev = Event(kind=EventKind.TASK_CREATED, payload={"n": 0})
    # A complete line plus a partial (mid-append) second line with no newline.
    with path.open("w", encoding="utf-8") as f:
        f.write(ev.model_dump_json() + "\n")
        f.write('{"kind":"task.created","partial')
    assert len(await log.read_all()) == 1  # partial line not consumed
    # Complete the partial line; the next read picks it up.
    ev2 = Event(kind=EventKind.TASK_COMPLETED, payload={"n": 1})
    with path.open("w", encoding="utf-8") as f:
        f.write(ev.model_dump_json() + "\n")
        f.write(ev2.model_dump_json() + "\n")
    assert len(await log.read_all()) == 2
