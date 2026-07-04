from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from exocortex.contracts import Event, EventKind
from exocortex.contracts.common import now
from exocortex.memory.reflect import ReflectionService
from exocortex.observability.audit import AuditLog


@pytest.mark.asyncio
async def test_window_all_and_override(tmp_path: Path) -> None:
    svc = ReflectionService(audit=AuditLog(tmp_path / "a.jsonl"))
    assert await svc.window_from(max_days=7, all_history=True) is None
    lo = await svc.window_from(max_days=7, override_days=2)
    assert (now() - lo) < timedelta(days=2, hours=1)


@pytest.mark.asyncio
async def test_window_since_last_reflection_capped(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    old = now() - timedelta(days=30)
    await audit.record(Event(kind=EventKind.REFLECTION_COMPLETED,
                             timestamp=old, payload={"status": "completed"}))
    lo = await svc.window_from(max_days=7)
    # capped at now-7d even though last reflection was 30d ago
    assert (now() - lo) < timedelta(days=7, hours=1)
    assert (now() - lo) > timedelta(days=6)
