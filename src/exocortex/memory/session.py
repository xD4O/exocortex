from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from exocortex.contracts import MemoryRecord, MemoryScope
from exocortex.contracts.common import now


class SessionMemoryStore:
    """In-memory, per-session ephemeral store with TTL eviction.

    Session memory is the noisy scratch layer. Durable records graduate here
    via explicit promotion; session content is compacted into handoff digests
    but not preserved verbatim.
    """

    def __init__(self) -> None:
        self._records: dict[UUID, MemoryRecord] = {}

    def write(self, record: MemoryRecord) -> None:
        if record.scope != MemoryScope.SESSION:
            raise ValueError(
                f"SessionMemoryStore only accepts scope=session; got {record.scope}"
            )
        self._records[record.id] = record

    def get(self, record_id: UUID) -> MemoryRecord | None:
        self._evict_expired()
        return self._records.get(record_id)

    def list_session(self, session_id: str) -> list[MemoryRecord]:
        self._evict_expired()
        return sorted(
            (r for r in self._records.values() if r.scope_id == session_id),
            key=lambda r: r.timestamp,
        )

    def count(self) -> int:
        self._evict_expired()
        return len(self._records)

    def _evict_expired(self) -> None:
        current = now()
        expired = [
            rid
            for rid, r in self._records.items()
            if r.ttl_seconds is not None
            and _is_expired(r.timestamp, r.ttl_seconds, current)
        ]
        for rid in expired:
            del self._records[rid]


def _is_expired(ts: datetime, ttl_seconds: int, current: datetime) -> bool:
    return current - ts > timedelta(seconds=ttl_seconds)
