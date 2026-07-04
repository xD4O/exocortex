from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from exocortex.contracts import Event, EventKind
from exocortex.memory.reflect import ReflectionService
from exocortex.observability.audit import AuditLog


def _proposed(iid: str) -> Event:
    return Event(kind=EventKind.INSIGHT_PROPOSED,
                 payload={"insight_id": iid, "kind": "gap", "title": "t",
                          "detail": "d", "refs": [str(uuid.uuid4())]})


@pytest.mark.asyncio
async def test_projection_status(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    a, b, c = "ins-a", "ins-b", "ins-c"
    await audit.record(_proposed(a))
    await audit.record(_proposed(b))
    await audit.record(_proposed(c))
    await audit.record(Event(kind=EventKind.INSIGHT_ACCEPTED, payload={"insight_id": b}))
    await audit.record(Event(kind=EventKind.INSIGHT_DISMISSED, payload={"insight_id": c}))

    open_q = await svc.list_insights()
    ids = {i["insight_id"] for i in open_q}
    assert ids == {a}                       # only unresolved by default
    assert open_q[0]["status"] == "proposed"

    allq = await svc.list_insights(include_resolved=True)
    by_id = {i["insight_id"]: i["status"] for i in allq}
    assert by_id == {a: "proposed", b: "accepted", c: "dismissed"}


@pytest.mark.asyncio
async def test_projection_newest_first_and_preserves_payload(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    for iid in ("first", "second", "third"):
        await audit.record(_proposed(iid))
    items = await svc.list_insights()
    # Newest proposed first — would fail if `.reverse()` were dropped.
    assert [i["insight_id"] for i in items] == ["third", "second", "first"]
    # Non-status payload fields survive the fold.
    assert all(i.get("kind") == "gap" and "title" in i for i in items)
