from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio

from exocortex.contracts import Confidence, MemoryRecord, MemoryScope
from exocortex.memory.embedding import pack_embedding, unpack_embedding

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL DEFAULT 1,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    ttl_seconds INTEGER,
    timestamp TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_records(scope, scope_id);
CREATE INDEX IF NOT EXISTS idx_memory_timestamp ON memory_records(timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    id UNINDEXED,
    content,
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_records BEGIN
    INSERT INTO memory_fts(id, content) VALUES (NEW.id, NEW.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_records BEGIN
    DELETE FROM memory_fts WHERE id = OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE OF content ON memory_records BEGIN
    UPDATE memory_fts SET content = NEW.content WHERE id = NEW.id;
END;
"""


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=UUID(row["id"]),
        type=row["type"],
        content=row["content"],
        source=row["source"],
        confidence=Confidence(row["confidence"]),
        scope=MemoryScope(row["scope"]),
        scope_id=row["scope_id"],
        tags=json.loads(row["tags_json"]),
        ttl_seconds=row["ttl_seconds"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
    )


class DurableMemoryStore:
    """SQLite-backed memory store. Every row has provenance columns; FTS5 index
    is kept in sync via triggers; embeddings are stored as packed-float BLOBs.

    Async methods wrap blocking sqlite3 calls via anyio.to_thread.run_sync so
    the event loop stays responsive.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        self._lock = anyio.Lock()

    async def close(self) -> None:
        async with self._lock:
            await anyio.to_thread.run_sync(self._conn.close)

    async def write(
        self, record: MemoryRecord, *, embedding: list[float] | None = None
    ) -> None:
        def _sync() -> None:
            blob = pack_embedding(embedding) if embedding is not None else None
            self._conn.execute(
                """
                INSERT OR REPLACE INTO memory_records (
                    id, schema_version, type, content, source, confidence,
                    scope, scope_id, tags_json, ttl_seconds, timestamp, embedding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.id),
                    record.schema_version,
                    record.type,
                    record.content,
                    record.source,
                    record.confidence.value,
                    record.scope.value,
                    record.scope_id,
                    json.dumps(record.tags),
                    record.ttl_seconds,
                    record.timestamp.isoformat(),
                    blob,
                ),
            )
            self._conn.commit()

        async with self._lock:
            await anyio.to_thread.run_sync(_sync)

    async def get(self, record_id: UUID) -> MemoryRecord | None:
        def _sync() -> sqlite3.Row | None:
            cur = self._conn.execute(
                "SELECT * FROM memory_records WHERE id = ?", (str(record_id),)
            )
            row: sqlite3.Row | None = cur.fetchone()
            return row

        async with self._lock:
            row = await anyio.to_thread.run_sync(_sync)
        return _row_to_record(row) if row else None

    async def count(self) -> int:
        def _sync() -> int:
            return int(
                self._conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0]
            )

        async with self._lock:
            return await anyio.to_thread.run_sync(_sync)

    async def search_fts(
        self,
        query: str,
        *,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        def _sync() -> list[sqlite3.Row]:
            sql = """
                SELECT mr.*
                FROM memory_fts f
                JOIN memory_records mr ON mr.id = f.id
                WHERE memory_fts MATCH ?
            """
            params: list[Any] = [query]
            if scope is not None:
                sql += " AND mr.scope = ?"
                params.append(scope.value)
            if scope_id is not None:
                sql += " AND mr.scope_id = ?"
                params.append(scope_id)
            sql += " ORDER BY bm25(memory_fts) LIMIT ?"
            params.append(limit)
            return list(self._conn.execute(sql, params))

        async with self._lock:
            rows = await anyio.to_thread.run_sync(_sync)
        return [_row_to_record(r) for r in rows]

    async def list_by_scope(
        self, scope: MemoryScope, scope_id: str
    ) -> list[MemoryRecord]:
        def _sync() -> list[sqlite3.Row]:
            return list(
                self._conn.execute(
                    "SELECT * FROM memory_records "
                    "WHERE scope = ? AND scope_id = ? "
                    "ORDER BY timestamp",
                    (scope.value, scope_id),
                )
            )

        async with self._lock:
            rows = await anyio.to_thread.run_sync(_sync)
        return [_row_to_record(r) for r in rows]

    async def all_with_embeddings(
        self,
        *,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
    ) -> list[tuple[MemoryRecord, list[float]]]:
        def _sync() -> list[sqlite3.Row]:
            sql = "SELECT * FROM memory_records WHERE embedding IS NOT NULL"
            params: list[Any] = []
            if scope is not None:
                sql += " AND scope = ?"
                params.append(scope.value)
            if scope_id is not None:
                sql += " AND scope_id = ?"
                params.append(scope_id)
            return list(self._conn.execute(sql, params))

        async with self._lock:
            rows = await anyio.to_thread.run_sync(_sync)
        return [(_row_to_record(r), unpack_embedding(r["embedding"])) for r in rows]
