from __future__ import annotations

from pathlib import Path

import anyio

from exocortex.contracts import Event
from exocortex.observability.logging import get_logger

logger = get_logger("exocortex.audit")


class AuditLog:
    """Append-only JSONL persistence for every event.

    Reads are incremental (C2): the parsed events are cached in memory and each
    ``read_all`` only tails the bytes appended since the last read, instead of
    re-reading and re-validating the entire file. The whole web UI is a
    projection over this log and the dashboard polls several endpoints on
    timers, so a full re-parse per request was O(events) work many times a
    minute. This keeps steady-state reads O(new events).

    Correctness under multiple writers: several `AuditLog` instances may point
    at the same file (web server, dispatch service, MCP handlers). ``record``
    therefore does NOT touch the cache — it only appends to the file — and
    ``read_all`` always reconciles from the file by byte offset, so writes from
    any instance are picked up. A shrunk file (truncation / rotation) resets
    the cache, and a partial trailing line (a write observed mid-append) is
    left unconsumed until the next read.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = anyio.Lock()
        self._cache: list[Event] = []
        self._offset = 0  # bytes of `path` already parsed into `_cache`
        self._parses = 0  # observability/test counter: total events parsed

    async def record(self, event: Event) -> None:
        line = event.model_dump_json() + "\n"
        async with self._lock, await anyio.open_file(
            self.path, "a", encoding="utf-8"
        ) as f:
            await f.write(line)
        logger.debug(
            "audit.recorded",
            event_id=str(event.id),
            kind=event.kind,
            task_id=str(event.task_id) if event.task_id else None,
        )

    async def read_all(self) -> list[Event]:
        async with self._lock:
            return list(await self._reconcile())

    async def _reconcile(self) -> list[Event]:
        if not self.path.exists():
            self._cache = []
            self._offset = 0
            return self._cache

        size = self.path.stat().st_size
        if size < self._offset:
            # File shrank — truncated or rotated out from under us. Rebuild.
            self._cache = []
            self._offset = 0
        if size == self._offset:
            return self._cache  # nothing new since last read

        async with await anyio.open_file(self.path, "rb") as f:
            await f.seek(self._offset)
            chunk = await f.read()

        text = chunk.decode("utf-8", errors="replace")
        consumed = len(chunk)
        if not text.endswith("\n"):
            # A partial line (a write observed mid-append). Don't consume it;
            # re-read from here next time.
            partial, sep, tail = text.rpartition("\n")
            consumed -= len(tail.encode("utf-8"))
            text = partial if sep else ""

        for raw in text.split("\n"):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                self._cache.append(Event.model_validate_json(stripped))
                self._parses += 1
            except Exception:  # pragma: no cover - skip a malformed line
                logger.warning("audit.malformed_line", offset=self._offset)

        self._offset += consumed
        return self._cache
