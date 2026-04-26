"""Audit-log tailer that fans out new events to WebSocket subscribers.

Reads the append-only JSONL audit log incrementally — on each poll it reads
from the last known byte offset and ships every complete line. This is
intentionally simple (no inotify, no fsevents) because the audit log is only
appended to, and we already own the write path elsewhere. Poll interval is
small enough (250ms) for a live feed without being a CPU hog.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

import anyio

from exocortex.contracts import Event
from exocortex.observability.logging import get_logger

logger = get_logger("exocortex.operator.web.events")


class EventBroadcaster:
    """Holds a set of subscriber queues; `publish` fans out to all of them.

    Subscribers use `subscribe()` as an async context manager to get their own
    queue; slow consumers are bounded (queue maxsize) and dropped messages are
    counted rather than blocking the tailer.
    """

    def __init__(self, queue_maxsize: int = 500) -> None:
        self._queue_maxsize = queue_maxsize
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._dropped = 0

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: Event) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped += 1
                logger.warning(
                    "ws.subscriber_queue_full", dropped_total=self._dropped
                )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


async def tail_audit_log(
    path: Path,
    broadcaster: EventBroadcaster,
    *,
    poll_interval: float = 0.25,
) -> None:
    """Forever-loop: tail `path`, deserialize each new line into an Event, and
    broadcast it. Cooperates with asyncio cancellation (lifespan shutdown).
    """
    last_offset = 0
    leftover = ""

    # Start from end-of-file so we only stream *new* events after server start.
    if path.exists():
        last_offset = path.stat().st_size

    while True:
        try:
            if path.exists():
                size = path.stat().st_size
                if size < last_offset:
                    # File was truncated / rotated; reset.
                    last_offset = 0
                    leftover = ""
                if size > last_offset:
                    chunk = await anyio.to_thread.run_sync(
                        _read_chunk, path, last_offset, size
                    )
                    last_offset = size
                    data = leftover + chunk
                    lines = data.split("\n")
                    # Last segment may be incomplete; save it.
                    leftover = lines[-1]
                    for raw in lines[:-1]:
                        stripped = raw.strip()
                        if not stripped:
                            continue
                        try:
                            event = Event.model_validate_json(stripped)
                        except ValueError:
                            logger.warning(
                                "ws.audit_parse_failed", line_preview=stripped[:120]
                            )
                            continue
                        broadcaster.publish(event)
        except Exception as exc:  # noqa: BLE001 — we want the loop to continue
            logger.warning("ws.audit_tail_error", error=str(exc))
        await asyncio.sleep(poll_interval)


def _read_chunk(path: Path, start: int, end: int) -> str:
    with path.open("r", encoding="utf-8") as f:
        f.seek(start)
        return f.read(end - start)


async def run_tailer_task(
    path: Path, broadcaster: EventBroadcaster
) -> asyncio.Task[None]:
    """Spawn the tailer as an asyncio task and return the handle."""
    task = asyncio.create_task(tail_audit_log(path, broadcaster))
    return task


async def stop_tailer(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
