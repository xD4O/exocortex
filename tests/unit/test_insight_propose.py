from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from exocortex.contracts import EventKind
from exocortex.observability.audit import AuditLog
from exocortex.operator.mcp.server import _propose_insight  # helper under test


@pytest.mark.asyncio
async def test_propose_emits_event(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    rid = str(uuid.uuid4())
    out = await _propose_insight(audit, kind="contradiction", title="t", detail="d",
                                 refs=[rid], reflection_id=str(uuid.uuid4()),
                                 action_type="supersede",
                                 action_payload={"stale_record_id": rid})
    assert "insight_id" in out
    events = await audit.read_all()
    proposed = [e for e in events if e.kind == EventKind.INSIGHT_PROPOSED]
    assert len(proposed) == 1
    assert proposed[0].payload["kind"] == "contradiction"
    assert proposed[0].payload["refs"] == [rid]


@pytest.mark.asyncio
async def test_propose_rejects_empty_refs(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    with pytest.raises(ValueError):
        await _propose_insight(audit, kind="gap", title="t", detail="d",
                               refs=[], reflection_id=str(uuid.uuid4()))
