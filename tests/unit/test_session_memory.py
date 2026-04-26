from __future__ import annotations

from datetime import timedelta

import pytest

from exocortex.contracts import Confidence, MemoryRecord, MemoryScope
from exocortex.contracts.common import now
from exocortex.memory.session import SessionMemoryStore


def _session_rec(
    content: str, *, session_id: str = "s1", ttl: int | None = None
) -> MemoryRecord:
    return MemoryRecord(
        type="observation",
        content=content,
        source="codex",
        confidence=Confidence.OBSERVED,
        scope=MemoryScope.SESSION,
        scope_id=session_id,
        ttl_seconds=ttl,
    )


def test_write_and_list_session() -> None:
    store = SessionMemoryStore()
    store.write(_session_rec("a", session_id="s1"))
    store.write(_session_rec("b", session_id="s1"))
    store.write(_session_rec("other", session_id="s2"))

    listed = store.list_session("s1")
    assert [r.content for r in listed] == ["a", "b"]


def test_rejects_non_session_scope() -> None:
    store = SessionMemoryStore()
    bad = MemoryRecord(
        type="x",
        content="x",
        source="x",
        confidence=Confidence.OBSERVED,
        scope=MemoryScope.TASK,
        scope_id="t1",
    )
    with pytest.raises(ValueError):
        store.write(bad)


def test_ttl_eviction() -> None:
    store = SessionMemoryStore()
    r = _session_rec("expires soon", ttl=60)
    # Backdate the record 2 minutes to simulate expiry.
    r.timestamp = now() - timedelta(minutes=2)
    store.write(r)

    assert store.get(r.id) is None
    assert store.count() == 0


def test_ttl_does_not_evict_fresh() -> None:
    store = SessionMemoryStore()
    r = _session_rec("still fresh", ttl=3600)
    store.write(r)
    assert store.get(r.id) is not None
    assert store.count() == 1
