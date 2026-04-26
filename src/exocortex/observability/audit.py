from __future__ import annotations

from pathlib import Path

import anyio

from exocortex.contracts import Event
from exocortex.observability.logging import get_logger

logger = get_logger("exocortex.audit")


class AuditLog:
    """Append-only JSONL persistence for every event."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = anyio.Lock()

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
        if not self.path.exists():
            return []
        events: list[Event] = []
        async with await anyio.open_file(self.path, encoding="utf-8") as f:
            async for raw in f:
                stripped = raw.strip()
                if stripped:
                    events.append(Event.model_validate_json(stripped))
        return events
