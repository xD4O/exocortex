"""REST + WebSocket routes for the operator web UI.

Everything here reads from the same stores the CLI uses — `AuditLog`,
`DurableMemoryStore`, `HybridRetrieval` — and treats them as the single source
of truth. Most routes are read-only lenses; a few explicit mutating endpoints
exist (conversation run/close/delete, settings toggles, profile answer/redact).
Those, and the event WebSocket, are protected by the same-origin / token guard
in `security.py` (A2) so a cross-site page cannot drive them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from exocortex.config import Settings
from exocortex.contracts import Event, EventKind, MemoryRecord, MemoryScope
from exocortex.coordination.conversation import (
    ConversationError,
    ConversationService,
    run_rounds,
)
from exocortex.memory.chat import MemoryChatService
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.llm import LocalLLMUnavailableError, OllamaChatProvider
from exocortex.memory.profile import PROFILE_DIMENSIONS, ProfileService
from exocortex.memory.reflect import ReflectionService
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.observability.audit import AuditLog
from exocortex.observability.humanize import humanize_event
from exocortex.observability.logging import get_logger
from exocortex.operator.mcp.dispatch import DispatchService
from exocortex.operator.web.events import EventBroadcaster
from exocortex.operator.web.projection import project_records

logger = get_logger("exocortex.operator.web.routes")

# Hard-coded agent registry per Bet B — no provider-specific dispatch anywhere
# else. Capabilities here mirror the declared flags of each Bridge.
AGENTS: list[dict[str, Any]] = [
    {
        "id": "codex",
        "name": "ChatGPT Codex",
        "kind": "bridge",
        "capabilities": [
            "edit_files",
            "run_shell",
            "long_context",
            "structured_output",
            "interactive",
        ],
    },
    {
        "id": "claude_code",
        "name": "Claude Code",
        "kind": "bridge",
        "capabilities": [
            "edit_files",
            "run_shell",
            "long_context",
            "structured_output",
            "mcp_client",
            "interactive",
        ],
    },
    {
        "id": "hermes",
        "name": "Nous Hermes",
        "kind": "bridge",
        "capabilities": [
            "edit_files",
            "run_shell",
            "long_context",
            "structured_output",
            "batch",
        ],
    },
]


def _preview_memory_written(p: dict[str, Any]) -> str:
    content = str(p.get("content") or "")
    return content[:120] + ("…" if len(content) > 120 else "")


def _preview_memory_chat(p: dict[str, Any]) -> str:
    q = str(p.get("question") or "")
    cited = p.get("cited_record_ids") or []
    return f"{q[:90]} · {len(cited)} citation(s)"


def _preview_memory_forgotten(p: dict[str, Any]) -> str:
    return f"forgot {str(p.get('record_id', '?'))[:8]} — {p.get('content_preview', '')[:80]}"


def _preview_memory_merged(p: dict[str, Any]) -> str:
    return f"merged into {str(p.get('keep_id', '?'))[:8]} ({p.get('removed_count', 0)} dropped)"


def _preview_memory_promoted(p: dict[str, Any]) -> str:
    return f"{p.get('from','?')} → {p.get('to','?')} ({p.get('cluster_size', 0)} agree)"


def _preview_tool(p: dict[str, Any]) -> str:
    return f"{p.get('tool_name', '?')} {str(p.get('args') or '')[:60]}"


def _preview_session_opened(p: dict[str, Any]) -> str:
    return f"via {p.get('via', '?')} · {p.get('unfinished_count', 0)} unfinished"


def _preview_task_created(p: dict[str, Any]) -> str:
    return str(p.get("goal") or "")[:120]


def _preview_status_changed(p: dict[str, Any]) -> str:
    return f"{p.get('from', '?')} → {p.get('to', '?')}"


def _preview_handoff(p: dict[str, Any]) -> str:
    """from → to · short goal — renders chain-of-custody at a glance.

    `?` means the calling agent didn't pass `from_agent` and we couldn't
    auto-infer from a parent task. That's diagnostic — do not paper it
    over with a default that asserts identity (e.g. "operator").
    """
    src = p.get("from_agent") or "?"
    dst = p.get("to_agent") or "?"
    goal = str(p.get("goal_preview") or "")[:80]
    fb = " (fallback)" if p.get("fallback_used") else ""
    if goal:
        return f"{src} → {dst}{fb} · {goal}"
    return f"{src} → {dst}{fb}"


def _preview_dispatch_fallback(p: dict[str, Any]) -> str:
    requested = p.get("requested") or "?"
    fallback = p.get("fallback") or "?"
    return f"{requested} → {fallback} (auto-fallback)"


def _preview_dispatch_failed(p: dict[str, Any]) -> str:
    pref = p.get("preferred_agent") or "auto"
    reason = p.get("reason") or "unknown"
    return f"{pref} · {reason}"


def _preview_task_terminal(p: dict[str, Any]) -> str:
    goal = str(p.get("goal") or "")[:100]
    err = str(p.get("error") or "")[:80]
    if goal and err:
        return f"{goal} · {err}"
    if goal:
        return goal
    if err:
        return err
    return ""


_PREVIEW_BY_KIND: dict[EventKind, Any] = {
    EventKind.MEMORY_WRITTEN: _preview_memory_written,
    EventKind.MEMORY_CHAT: _preview_memory_chat,
    EventKind.MEMORY_FORGOTTEN: _preview_memory_forgotten,
    EventKind.MEMORY_MERGED: _preview_memory_merged,
    EventKind.MEMORY_PROMOTED: _preview_memory_promoted,
    EventKind.TOOL_PROPOSED: _preview_tool,
    EventKind.TOOL_APPROVED: _preview_tool,
    EventKind.TOOL_REJECTED: _preview_tool,
    EventKind.TOOL_EXECUTED: _preview_tool,
    EventKind.SESSION_OPENED: _preview_session_opened,
    EventKind.TASK_CREATED: _preview_task_created,
    EventKind.TASK_STATUS_CHANGED: _preview_status_changed,
    EventKind.HANDOFF_INITIATED: _preview_handoff,
    EventKind.HANDOFF_ACCEPTED: _preview_handoff,
    EventKind.DISPATCH_FALLBACK: _preview_dispatch_fallback,
    EventKind.DISPATCH_FAILED: _preview_dispatch_failed,
    EventKind.TASK_FAILED: _preview_task_terminal,
    EventKind.TASK_COMPLETED: _preview_task_terminal,
}


def _event_preview(event: Event) -> str:
    """One-line human-readable preview of an event for timeline UIs.

    Each kind has its own most-useful field — picking it explicitly beats
    serializing the whole payload, which is noisy and frequently long.
    """
    p = event.payload or {}
    fn = _PREVIEW_BY_KIND.get(event.kind)
    if fn is not None:
        return str(fn(p))
    if event.kind.value.startswith("dispatch."):
        return f"task={str(p.get('task_id') or '?')[:8]}"
    # Kinds without a bespoke web formatter (sessions, profile, conversations,
    # approvals…) fall back to the shared humanizer so they're no longer blank.
    return humanize_event(event)


def _record_to_dict(record: MemoryRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "type": record.type,
        "content": record.content,
        "source": record.source,
        "confidence": record.confidence.value,
        "scope": record.scope.value,
        "scope_id": record.scope_id,
        "tags": list(record.tags),
        "ttl_seconds": record.ttl_seconds,
        "timestamp": record.timestamp.isoformat(),
    }


def _event_to_dict(event: Event) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(event.model_dump_json())
    return parsed


class _ConstellationCache:
    """Per-process cache of the full constellation projection.

    Invalidates when the record count in the store changes. Not crypto-grade —
    if a record is edited in place, we'd miss it. Memory records are append-
    only through the contract though (you write a new record to supersede an
    old one), so count-based invalidation is good enough.
    """

    def __init__(self) -> None:
        self.last_count: int | None = None
        self.payload: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    async def get_or_build(self, store: DurableMemoryStore) -> dict[str, Any]:
        async with self._lock:
            current = await store.count()
            if self.payload is not None and current == self.last_count:
                return self.payload
            self.payload = await self._build(store)
            self.last_count = current
            return self.payload

    @staticmethod
    async def _build(store: DurableMemoryStore) -> dict[str, Any]:
        with_emb = await store.all_with_embeddings()
        # Include all records, even ones without embeddings: list_all_raw
        # isn't needed — `all_with_embeddings` omits them. We fetch all_with
        # embeddings and, separately, those with no embedding by querying
        # the store directly via a helper sql path. For simplicity we treat
        # records without embeddings as "not in the constellation" unless we
        # explicitly want to show them. The store currently only exposes
        # embedded records via `all_with_embeddings`. That's the dataset.
        inputs: list[tuple[str, list[float] | None]] = [
            (str(rec.id), vec) for rec, vec in with_emb
        ]
        projected = project_records(inputs)

        points = []
        for (rec, _vec), proj in zip(with_emb, projected, strict=False):
            points.append(
                {
                    "id": proj.id,
                    "x": proj.x,
                    "y": proj.y,
                    "has_embedding": proj.has_embedding,
                    "type": rec.type,
                    "source": rec.source,
                    "confidence": rec.confidence.value,
                    "scope": rec.scope.value,
                    "scope_id": rec.scope_id,
                    "tags": list(rec.tags),
                    "timestamp": rec.timestamp.isoformat(),
                    "content": rec.content,
                }
            )
        return {"count": len(points), "points": points}


def build_router(  # noqa: PLR0915 - single factory, routes are flat by design
    *,
    audit: AuditLog,
    store: DurableMemoryStore,
    broadcaster: EventBroadcaster,
    settings: Settings | None = None,
) -> APIRouter:
    router = APIRouter()
    retrieval = HybridRetrieval(store, DeterministicEmbeddingProvider())
    constellation_cache = _ConstellationCache()
    cfg = settings or Settings()

    @router.get("/api/status")
    async def status() -> dict[str, Any]:
        events = await audit.read_all()
        now = datetime.now(tz=UTC)
        one_hour_ago = now - timedelta(hours=1)

        task_ids: set[str] = set()
        events_last_hour = 0
        active_agents: set[str] = set()
        for ev in events:
            if ev.task_id is not None:
                task_ids.add(str(ev.task_id))
            if ev.timestamp >= one_hour_ago:
                events_last_hour += 1
                if ev.agent_id:
                    active_agents.add(ev.agent_id)

        return {
            "ok": True,
            "generated_at": now.isoformat(),
            "tasks": len(task_ids),
            "memory_records": await store.count(),
            "events_last_hour": events_last_hour,
            "events_total": len(events),
            "bridges_registered": len(AGENTS),
            "agents_active_last_hour": sorted(active_agents),
            "ws_subscribers": broadcaster.subscriber_count,
        }

    @router.get("/api/tasks")
    async def list_tasks(  # noqa: PLR0912, PLR0915 — per-event-kind dispatch is naturally branchy
        limit: int = Query(50, ge=1, le=500),
        status: str = Query("all", pattern="^(all|open|completed|failed)$"),
    ) -> dict[str, Any]:
        events = await audit.read_all()

        statuses: dict[str, str] = {}
        goals: dict[str, str] = {}
        created_at: dict[str, str] = {}
        last_event_at: dict[str, str] = {}
        agents: dict[str, set[str]] = {}
        event_counts: Counter[str] = Counter()
        parent_links: dict[str, str] = {}     # child task_id → parent task_id
        owning_agent: dict[str, str] = {}     # task_id → owning agent (from to_agent on handoff)

        for ev in events:
            if ev.task_id is None:
                # Some HANDOFF_INITIATED events still link to their child
                # via payload even when ev.task_id is None — handled below.
                if ev.kind == EventKind.HANDOFF_INITIATED and ev.payload:
                    child = ev.payload.get("child_task_id")
                    parent = ev.payload.get("parent_task_id")
                    if isinstance(child, str) and isinstance(parent, str):
                        parent_links[child] = parent
                continue
            tid = str(ev.task_id)
            event_counts[tid] += 1
            ts = ev.timestamp.isoformat()
            last_event_at[tid] = ts
            if ev.agent_id:
                agents.setdefault(tid, set()).add(ev.agent_id)
            if ev.kind == EventKind.TASK_CREATED:
                statuses.setdefault(tid, "proposed")
                goals[tid] = str(ev.payload.get("goal", ""))
                created_at[tid] = ts
            elif ev.kind == EventKind.TASK_STATUS_CHANGED:
                to = ev.payload.get("to")
                if isinstance(to, str):
                    statuses[tid] = to
            elif ev.kind == EventKind.TASK_COMPLETED:
                statuses[tid] = "completed"
            elif ev.kind == EventKind.TASK_FAILED:
                statuses[tid] = "failed"
            elif ev.kind == EventKind.HANDOFF_INITIATED and ev.payload:
                child = ev.payload.get("child_task_id")
                parent = ev.payload.get("parent_task_id")
                if isinstance(child, str) and isinstance(parent, str):
                    parent_links[child] = parent
                to_agent = ev.payload.get("to_agent")
                if (
                    isinstance(child, str)
                    and isinstance(to_agent, str)
                    and to_agent != "auto"
                ):
                    owning_agent[child] = to_agent

        # Pull last decision-type record per task (one scan, latest-wins).
        all_records = await store.all_with_embeddings()
        last_decision: dict[str, str] = {}
        for record, _vec in all_records:
            if record.scope != MemoryScope.TASK:
                continue
            if not record.type.startswith("decision"):
                continue
            last_decision[record.scope_id] = record.content[:160]

        def _status_bucket(s: str) -> str:
            if s in ("completed",):
                return "completed"
            if s in ("failed",):
                return "failed"
            return "open"

        tasks = []
        for tid, st in statuses.items():
            bucket = _status_bucket(st)
            if status not in ("all", bucket):
                continue
            goal = goals.get(tid, "")
            task_agents = sorted(agents.get(tid, set()))
            owner = owning_agent.get(tid) or (task_agents[0] if task_agents else None)
            tasks.append(
                {
                    "id": tid,
                    "task_id": tid,  # alias for UI clarity
                    "title": goal[:80],
                    "status": st,
                    "status_bucket": bucket,
                    "goal": goal,
                    "last_decision": last_decision.get(tid, ""),
                    "created_at": created_at.get(tid),
                    "last_event_at": last_event_at.get(tid),
                    "scope": MemoryScope.TASK.value,
                    "scope_id": tid,
                    "agents": task_agents,
                    "owning_agent": owner,
                    "parent_task_id": parent_links.get(tid),
                    "event_count": event_counts[tid],
                }
            )
        tasks.sort(key=lambda t: t.get("last_event_at") or "", reverse=True)
        return {"count": len(tasks), "tasks": tasks[:limit], "items": tasks[:limit]}

    @router.get("/api/tasks/{task_id}/trace")
    async def task_trace(task_id: str) -> dict[str, Any]:
        events = await audit.read_all()
        matches = [
            ev
            for ev in events
            if ev.task_id is not None and str(ev.task_id).startswith(task_id)
        ]
        if not matches:
            raise HTTPException(status_code=404, detail=f"no events for task {task_id}")
        matches.sort(key=lambda e: e.timestamp)
        return {
            "task_id": str(matches[0].task_id),
            "count": len(matches),
            "events": [_event_to_dict(e) for e in matches],
        }

    @router.get("/api/memory/records")
    async def memory_records(
        scope: str | None = None,
        scope_id: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        if scope is not None:
            try:
                scope_enum = MemoryScope(scope)
            except ValueError as e:
                raise HTTPException(
                    status_code=400, detail=f"invalid scope {scope!r}"
                ) from e
            if scope_id is None:
                raise HTTPException(
                    status_code=400, detail="scope_id is required when scope is given"
                )
            records = await store.list_by_scope(scope_enum, scope_id)
        else:
            pairs = await store.all_with_embeddings()
            records = [r for r, _ in pairs]

        # Newest first for display.
        records.sort(key=lambda r: r.timestamp, reverse=True)
        sliced = records[:limit]
        return {
            "count": len(sliced),
            "total": len(records),
            "records": [_record_to_dict(r) for r in sliced],
        }

    @router.get("/api/memory/search")
    async def memory_search(
        q: str = Query(..., min_length=1),
        scope: str | None = None,
        scope_id: str | None = None,
        alpha: float = Query(0.5, ge=0.0, le=1.0),
        limit: int = Query(20, ge=1, le=200),
    ) -> dict[str, Any]:
        scope_enum: MemoryScope | None = None
        if scope is not None:
            try:
                scope_enum = MemoryScope(scope)
            except ValueError as e:
                raise HTTPException(
                    status_code=400, detail=f"invalid scope {scope!r}"
                ) from e
        hits = await retrieval.search(
            q, scope=scope_enum, scope_id=scope_id, limit=limit, alpha=alpha
        )
        return {
            "query": q,
            "alpha": alpha,
            "count": len(hits),
            "hits": [
                {"score": score, "record": _record_to_dict(record)}
                for record, score in hits
            ],
        }

    @router.get("/api/memory/constellation")
    async def memory_constellation() -> dict[str, Any]:
        return await constellation_cache.get_or_build(store)

    @router.get("/api/agents")
    async def agents_list() -> dict[str, Any]:
        # Aggregate stats per agent_id from the audit log + the static
        # registry. Agents that have only ever appeared in events (e.g.
        # "external", "memory_chat") are surfaced too — the UI lists every
        # actor, not just the bridge-declared ones.
        events = await audit.read_all()
        now = datetime.now(tz=UTC)
        thirty_sec_ago = now - timedelta(seconds=30)

        memory_writes: Counter[str] = Counter()
        tool_invocations: Counter[str] = Counter()
        dispatches: Counter[str] = Counter()
        chat_queries: Counter[str] = Counter()
        total_events: Counter[str] = Counter()
        last_active: dict[str, str] = {}
        first_seen: dict[str, str] = {}
        recently_active: set[str] = set()

        # 24-hour activity histogram per agent for the sparkline.
        # Bucket 0 = oldest hour (24h ago), bucket 23 = current hour.
        hourly_window_start = now - timedelta(hours=24)
        hourly: dict[str, list[int]] = {}

        for ev in events:
            if not ev.agent_id:
                continue
            aid = ev.agent_id
            total_events[aid] += 1
            ts = ev.timestamp.isoformat()
            if aid not in first_seen:
                first_seen[aid] = ts
            last_active[aid] = ts
            if ev.timestamp >= thirty_sec_ago:
                recently_active.add(aid)
            if ev.timestamp >= hourly_window_start:
                hours_ago = int(
                    (now - ev.timestamp).total_seconds() // 3600
                )
                bucket = max(0, min(23, 23 - hours_ago))
                hourly.setdefault(aid, [0] * 24)[bucket] += 1
            if ev.kind == EventKind.MEMORY_WRITTEN:
                memory_writes[aid] += 1
            elif ev.kind in (
                EventKind.TOOL_PROPOSED,
                EventKind.TOOL_APPROVED,
                EventKind.TOOL_EXECUTED,
            ):
                tool_invocations[aid] += 1
            elif ev.kind == EventKind.MEMORY_CHAT:
                chat_queries[aid] += 1
            elif ev.kind.value.startswith("dispatch."):
                dispatches[aid] += 1

        # Index static registry for color/name lookup; pad with seen agents.
        registered = {a["id"]: a for a in AGENTS}
        seen = set(total_events.keys()) | set(registered.keys())
        agents = []
        for aid in sorted(seen):
            base = registered.get(aid, {"id": aid, "name": aid, "kind": "external"})
            agents.append(
                {
                    **base,
                    "agent_id": aid,
                    "recently_active": aid in recently_active,
                    "total_events": int(total_events.get(aid, 0)),
                    "memory_writes": int(memory_writes.get(aid, 0)),
                    "tool_invocations": int(tool_invocations.get(aid, 0)),
                    "dispatches": int(dispatches.get(aid, 0)),
                    "chat_queries": int(chat_queries.get(aid, 0)),
                    "last_active_at": last_active.get(aid),
                    "first_seen_at": first_seen.get(aid),
                    "hourly": hourly.get(aid, [0] * 24),
                }
            )
        return {"count": len(agents), "agents": agents, "items": agents}

    @router.get("/api/activity")
    async def activity_feed(
        limit: int = Query(200, ge=1, le=2000),
        since_ms: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """Recent events across all agents — feeds the activity strip."""
        events = await audit.read_all()
        cutoff: datetime | None = (
            datetime.fromtimestamp(since_ms / 1000.0, tz=UTC) if since_ms else None
        )
        events.sort(key=lambda e: e.timestamp, reverse=True)
        items: list[dict[str, Any]] = []
        for ev in events:
            if cutoff is not None and ev.timestamp <= cutoff:
                break
            items.append(
                {
                    "event_id": str(ev.id),
                    "kind": ev.kind.value,
                    "agent_id": ev.agent_id or "",
                    "timestamp": ev.timestamp.isoformat(),
                    "timestamp_ms": int(ev.timestamp.timestamp() * 1000),
                    "task_id": str(ev.task_id) if ev.task_id else None,
                    "session_id": str(ev.session_id) if ev.session_id else None,
                    "payload_preview": _event_preview(ev),
                }
            )
            if len(items) >= limit:
                break
        return {"count": len(items), "items": items}

    @router.get("/api/agents/{agent_id}/history")
    async def agent_history(
        agent_id: str,
        limit: int = Query(200, ge=1, le=2000),
        since_ms: int = Query(0, ge=0),
        kind: str = Query("*"),
    ) -> dict[str, Any]:
        """Per-agent timeline — feeds the agents-page main panel."""
        events = await audit.read_all()
        cutoff: datetime | None = (
            datetime.fromtimestamp(since_ms / 1000.0, tz=UTC) if since_ms else None
        )
        events.sort(key=lambda e: e.timestamp, reverse=True)
        items: list[dict[str, Any]] = []
        for ev in events:
            if ev.agent_id != agent_id:
                continue
            if cutoff is not None and ev.timestamp <= cutoff:
                break
            if kind not in ("*", ev.kind.value):
                continue
            items.append(
                {
                    "event_id": str(ev.id),
                    "kind": ev.kind.value,
                    "agent_id": ev.agent_id or "",
                    "timestamp": ev.timestamp.isoformat(),
                    "timestamp_ms": int(ev.timestamp.timestamp() * 1000),
                    "task_id": str(ev.task_id) if ev.task_id else None,
                    "session_id": str(ev.session_id) if ev.session_id else None,
                    "payload": dict(ev.payload),
                    "payload_preview": _event_preview(ev),
                }
            )
            if len(items) >= limit:
                break
        return {"agent_id": agent_id, "count": len(items), "items": items}

    @router.get("/api/agents/{agent_id}/context/{event_id}")
    async def agent_event_context(
        agent_id: str, event_id: str
    ) -> dict[str, Any]:
        """The "why" drawer — preceding events in same task/session, plus
        any records the event references."""
        events = await audit.read_all()
        target: Event | None = next(
            (e for e in events if str(e.id) == event_id), None
        )
        if target is None or target.agent_id != agent_id:
            raise HTTPException(
                status_code=404, detail=f"event {event_id} for {agent_id} not found"
            )

        # Preceding context: up to 3 events in the same task or session,
        # strictly before the target's timestamp, newest-first.
        scope_match = []
        for ev in events:
            if ev.timestamp >= target.timestamp:
                continue
            if (target.task_id and ev.task_id == target.task_id) or (
                target.session_id and ev.session_id == target.session_id
            ):
                scope_match.append(ev)
        scope_match.sort(key=lambda e: e.timestamp, reverse=True)
        preceding = [
            {
                "event_id": str(e.id),
                "kind": e.kind.value,
                "agent_id": e.agent_id or "",
                "timestamp": e.timestamp.isoformat(),
                "task_id": str(e.task_id) if e.task_id else None,
                "session_id": str(e.session_id) if e.session_id else None,
                "payload_preview": _event_preview(e),
            }
            for e in scope_match[:3]
        ]

        # Records referenced: scan the payload for UUID-shaped fields, then
        # try to resolve them. Best-effort — payload conventions vary.
        referenced_ids: list[str] = []
        for key in (
            "record_id",
            "keep_id",
            "cited_record_ids",
            "retrieved_record_ids",
            "drop_ids",
        ):
            v = target.payload.get(key)
            if isinstance(v, str):
                referenced_ids.append(v)
            elif isinstance(v, list):
                referenced_ids.extend(str(x) for x in v)

        records: list[dict[str, Any]] = []
        for rid in referenced_ids:
            try:
                rec = await store.get(UUID(rid))
            except (ValueError, TypeError):
                continue
            if rec is not None:
                records.append(_record_to_dict(rec))

        return {
            "event": {
                "event_id": str(target.id),
                "kind": target.kind.value,
                "agent_id": target.agent_id or "",
                "timestamp": target.timestamp.isoformat(),
                "task_id": str(target.task_id) if target.task_id else None,
                "session_id": str(target.session_id) if target.session_id else None,
                "payload": dict(target.payload),
                "payload_preview": _event_preview(target),
            },
            "preceding": preceding,
            "records_referenced": records,
        }

    @router.websocket("/api/events")
    async def events_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = broadcaster.subscribe()
        try:
            # Send a hello frame so the client knows it's live.
            await websocket.send_json(
                {
                    "kind": "__hello__",
                    "subscribers": broadcaster.subscriber_count,
                }
            )
            while True:
                event = await queue.get()
                await websocket.send_json(_event_to_dict(event))
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("ws.send_failed", error=str(exc))
            with contextlib.suppress(Exception):
                await websocket.close()
        finally:
            broadcaster.unsubscribe(queue)

    # --- Memory chat (RAG over memory) ----------------------------------

    @router.get("/api/settings/memory_chat")
    async def memory_chat_status() -> dict[str, Any]:
        """Current state of the memory-chat toggle + endpoint reachability.
        UI uses this on page load to show ON/OFF and surface a 'backend
        unreachable' badge if Ollama isn't running."""
        enabled = cfg.memory_chat_enabled()
        endpoint_reachable = False
        detected_models: list[str] = []
        if enabled:
            chat_provider = OllamaChatProvider(
                model=cfg.memory_chat_chat_model,
                endpoint=cfg.memory_chat_endpoint,
                timeout_seconds=3.0,  # quick health check
            )
            try:
                detected_models = await chat_provider.list_available_models()
                endpoint_reachable = True
            except LocalLLMUnavailableError:
                endpoint_reachable = False
        return {
            "enabled": enabled,
            "endpoint": cfg.memory_chat_endpoint,
            "embedding_model": cfg.memory_chat_embedding_model,
            "chat_model": cfg.memory_chat_chat_model or None,
            "endpoint_reachable": endpoint_reachable,
            "detected_models": detected_models,
        }

    @router.post("/api/settings/memory_chat/toggle")
    async def memory_chat_toggle() -> dict[str, Any]:
        """Flip the master switch. Persistent across processes."""
        flag = cfg.chat_toggle_path
        if flag.exists():
            flag.unlink()
            return {"enabled": False}
        cfg.ensure_dirs()
        flag.write_text("enabled\n")
        return {"enabled": True}

    @router.post("/api/memory/chat")
    async def memory_chat(request: Request) -> dict[str, Any]:
        if not cfg.memory_chat_enabled():
            raise HTTPException(
                status_code=503,
                detail=(
                    "memory chat is OFF — flip the toggle to enable. "
                    "POST /api/settings/memory_chat/toggle"
                ),
            )
        body = await request.json()
        question = str(body.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question is required")
        top_k = int(body.get("top_k") or cfg.memory_chat_default_top_k)
        scope_raw = body.get("scope")
        scope_id_raw = body.get("scope_id")
        scope_enum = MemoryScope(scope_raw) if scope_raw else None
        chat_provider = OllamaChatProvider(
            model=cfg.memory_chat_chat_model,
            endpoint=cfg.memory_chat_endpoint,
            timeout_seconds=float(cfg.memory_chat_timeout_seconds),
            max_tokens=cfg.memory_chat_max_tokens,
        )
        service = MemoryChatService(
            store=store,
            retrieval=retrieval,
            chat_provider=chat_provider,
            audit=audit,
            embedding_model_name=cfg.memory_chat_embedding_model,
        )
        try:
            response = await service.ask(
                question=question,
                top_k=top_k,
                scope=scope_enum,
                scope_id=scope_id_raw if isinstance(scope_id_raw, str) else None,
            )
        except LocalLLMUnavailableError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return response.to_dict()

    # --- Profile (USER-scope memory) -----------------------------------

    def _profile_service() -> ProfileService:
        embedder = DeterministicEmbeddingProvider()
        return ProfileService(
            store=store,
            embedder=embedder,
            retrieval=retrieval,
            audit=audit,
            user_id=cfg.profile_user_id,
            frozen=cfg.profile_frozen(),
        )

    @router.get("/api/profile")
    async def profile_get() -> dict[str, Any]:
        """All USER-scope records grouped into the canonical dimensions
        plus an OTHER bucket for free-form `profile.*` types."""
        service = _profile_service()
        records = await service.list_records()
        canonical_types = [d[0] for d in PROFILE_DIMENSIONS]
        bucket: dict[str, list[dict[str, Any]]] = {t: [] for t in canonical_types}
        bucket["profile.question"] = []
        bucket["other"] = []
        for r in records:
            target = r.type if r.type in bucket else "other"
            entry = _record_to_dict(r)
            entry["evidence"] = [
                t.removeprefix("evidence:")
                for t in r.tags
                if t.startswith("evidence:")
            ]
            bucket[target].append(entry)
        sections = []
        for t in canonical_types:
            sections.append(
                {
                    "type": t,
                    "count": len(bucket[t]),
                    "items": bucket[t],
                }
            )
        if bucket["other"]:
            sections.append(
                {"type": "other", "count": len(bucket["other"]), "items": bucket["other"]}
            )
        return {
            "user_id": cfg.profile_user_id,
            "frozen": cfg.profile_frozen(),
            "sections": sections,
        }

    @router.post("/api/profile/redact")
    async def profile_redact(request: Request) -> dict[str, Any]:
        body = await request.json()
        record_id = str(body.get("record_id") or "").strip()
        if not record_id:
            raise HTTPException(status_code=400, detail="record_id required")
        service = _profile_service()
        try:
            return await service.redact(record_id=record_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/api/profile/questions")
    async def profile_questions(status: str = "open") -> dict[str, Any]:
        service = _profile_service()
        questions = await service.list_questions(status=status)
        return {
            "count": len(questions),
            "items": [
                {
                    "id": q.record_id,
                    "content": q.content,
                    "dimension": q.dimension,
                    "asked_at": q.asked_at,
                    "status": q.status,
                }
                for q in questions
            ],
        }

    @router.post("/api/profile/answer")
    async def profile_answer(request: Request) -> dict[str, Any]:
        body = await request.json()
        question_id = str(body.get("question_id") or "").strip()
        answer = str(body.get("answer") or "").strip()
        if not question_id or not answer:
            raise HTTPException(
                status_code=400, detail="question_id and answer are required"
            )
        service = _profile_service()
        try:
            return await service.answer(question_id=question_id, answer=answer)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/api/profile/gaps")
    async def profile_gaps() -> dict[str, Any]:
        service = _profile_service()
        gaps = await service.find_gaps()
        return {
            "count": len(gaps),
            "items": [
                {
                    "dimension": g.dimension,
                    "target_count": g.target_count,
                    "current_count": g.current_count,
                    "gap": g.gap,
                    "suggested_question": g.suggested_question,
                }
                for g in gaps
            ],
        }

    @router.post("/api/profile/seed_questions")
    async def profile_seed_questions() -> dict[str, Any]:
        """Heuristic seed: for the top N gaps that have no open question
        already, append the suggested question to the queue. Idempotent —
        only adds questions for under-covered dimensions that don't have
        an open question yet."""
        service = _profile_service()
        gaps = await service.find_gaps()
        existing_open = await service.list_questions(status="open")
        open_dims = {q.dimension for q in existing_open}
        added: list[str] = []
        for g in gaps[:5]:
            if g.dimension in open_dims:
                continue
            rec = await service.question(
                content=g.suggested_question, dimension=g.dimension
            )
            added.append(str(rec.id))
        return {"added": added, "count": len(added)}

    @router.get("/api/settings/profile_freeze")
    async def profile_freeze_status() -> dict[str, Any]:
        return {"frozen": cfg.profile_frozen()}

    @router.post("/api/settings/profile_freeze/toggle")
    async def profile_freeze_toggle() -> dict[str, Any]:
        path = cfg.profile_freeze_path
        if path.exists():
            path.unlink()
            new_state = False
        else:
            cfg.ensure_dirs()
            path.write_text("frozen\n")
            new_state = True
        return {"frozen": new_state}

    # --- Dashboard panels + /debug ---------------------------------------

    _FAILURE_KINDS: frozenset[str] = frozenset(
        {
            "task.failed",
            "tool.rejected",
            "approval.requested",
            "dispatch.failed",
        }
    )

    def _is_failure_event(ev: Event) -> bool:
        return ev.kind.value in _FAILURE_KINDS

    def _hint_for(ev: Event) -> list[str]:  # noqa: PLR0911 — kind dispatch
        """Operator-friendly suggestions for a failure event. Knowing why
        something failed is half; knowing what to do next is the other half."""
        p = ev.payload or {}
        kind = ev.kind.value
        if kind == "dispatch.failed":
            reason = str(p.get("reason") or "")
            if reason == "no_bridges_registered":
                return [
                    "Install `codex` or `hermes` on PATH so dispatch has somewhere to route.",
                    "Verify with `which codex` / `which hermes`.",
                ]
            if reason == "no_fallback_for_unsupported_agent":
                return [
                    "claude_code has no headless bridge yet (Phase 4.5).",
                    "Install codex or hermes — dispatch will auto-fall-back.",
                ]
            if reason == "preferred_agent_not_registered":
                return [
                    "Use `dispatch_task` without `preferred_agent` to let the router pick.",
                ]
            return ["See payload for the underlying error."]
        if kind == "task.failed":
            return [
                "Open `/agents` and filter by this task_id for the full timeline.",
                "Re-dispatch with a tighter goal if the failure was scope-related.",
            ]
        if kind == "tool.rejected":
            return [
                "Policy denied this tool call — review `src/exocortex/policy/rules.py`.",
                "If the denial is correct, ignore. If the rule is wrong, update + restart agent.",
            ]
        if kind == "approval.requested":
            return [
                "An agent is waiting on operator approval. Resolve via "
                "the daemon or auto-approve config.",
            ]
        return []

    @router.get("/api/dashboard/attention")
    async def dashboard_attention() -> dict[str, Any]:  # noqa: PLR0912, PLR0915 — multi-source aggregation
        """Items the operator should act on. Each row carries severity +
        title + body + a drilldown URL. Empty list ≠ broken backend; it
        means everything's clear."""
        events = await audit.read_all()
        now = datetime.now(tz=UTC)
        last_24h = now - timedelta(hours=24)
        items: list[dict[str, Any]] = []

        # 1. Recent dispatch failures (last 24h, last 5).
        recent_failed = sorted(
            [
                e for e in events
                if e.kind == EventKind.DISPATCH_FAILED and e.timestamp >= last_24h
            ],
            key=lambda e: e.timestamp,
            reverse=True,
        )[:5]
        for e in recent_failed:
            p = e.payload or {}
            items.append(
                {
                    "kind": "dispatch_failed",
                    "severity": "high",
                    "title": f"dispatch failed — {p.get('reason', 'unknown')}",
                    "body": str(p.get("detail") or p.get("goal_preview") or "")[:200],
                    "since": e.timestamp.isoformat(),
                    "action_url": f"/debug?event={e.id}",
                    "related_event_id": str(e.id),
                }
            )

        # 2. Tasks stuck in_progress > 2h (top 5).
        in_progress: dict[str, datetime] = {}
        last_status: dict[str, tuple[str, datetime]] = {}
        goals: dict[str, str] = {}
        for ev in events:
            if ev.task_id is None:
                continue
            tid = str(ev.task_id)
            if ev.kind == EventKind.TASK_CREATED:
                goals[tid] = str(ev.payload.get("goal", ""))
                in_progress[tid] = ev.timestamp
                last_status[tid] = ("proposed", ev.timestamp)
            elif ev.kind == EventKind.TASK_STATUS_CHANGED:
                to = ev.payload.get("to")
                if isinstance(to, str):
                    last_status[tid] = (to, ev.timestamp)
                    if to == "in_progress":
                        in_progress[tid] = ev.timestamp
                    elif to in ("completed", "failed", "cancelled"):
                        in_progress.pop(tid, None)
            elif ev.kind in (EventKind.TASK_COMPLETED, EventKind.TASK_FAILED):
                in_progress.pop(tid, None)
                last_status[tid] = (ev.kind.value.split(".")[-1], ev.timestamp)
        cutoff_stuck = now - timedelta(hours=2)
        stuck = sorted(
            [(tid, t) for tid, t in in_progress.items() if t < cutoff_stuck],
            key=lambda x: x[1],
        )[:5]
        for tid, since in stuck:
            elapsed = now - since
            items.append(
                {
                    "kind": "task_stuck",
                    "severity": "high" if elapsed > timedelta(hours=6) else "medium",
                    "title": f"task stuck {_human_duration(elapsed)} — {goals.get(tid, '?')[:60]}",
                    "body": "no status change since the last in_progress transition",
                    "since": since.isoformat(),
                    "action_url": f"/agents?task={tid}",
                    "related_task_id": tid,
                }
            )

        # 3. Pending approvals (approval.requested with no matching resolved).
        approval_pending: dict[str, Event] = {}
        closed_sessions: set[str] = set()
        for ev in events:
            if ev.kind == EventKind.APPROVAL_REQUESTED:
                approval_pending[str(ev.id)] = ev
            elif ev.kind == EventKind.APPROVAL_RESOLVED:
                req_id = str(ev.payload.get("request_id") or "")
                approval_pending.pop(req_id, None)
            elif ev.kind == EventKind.SESSION_CLOSED and ev.session_id:
                closed_sessions.add(str(ev.session_id))
        live_approvals = [
            ev for ev in approval_pending.values()
            # zombie guard: a closed session can never answer its approval,
            # and anything older than 48h is noise, not operator work
            if not (ev.session_id and str(ev.session_id) in closed_sessions)
            and (now - ev.timestamp) <= timedelta(hours=48)
        ]
        for ev in live_approvals[:5]:
            if ev.task_id:
                # land the operator on the task's custody chain
                action = f"/?chain={ev.task_id}"
            elif ev.agent_id:
                # land on the requesting agent's inspector
                action = f"/agents?agent={ev.agent_id}"
            else:
                action = "/debug"
            items.append(
                {
                    "kind": "approval_pending",
                    "severity": "medium",
                    "title": "approval pending"
                    + (f" — {ev.agent_id}" if ev.agent_id else ""),
                    "body": str(ev.payload.get("reason") or "agent waiting on operator")[:200],
                    "since": ev.timestamp.isoformat(),
                    "action_url": action,
                    "related_event_id": str(ev.id),
                }
            )

        # 4. Ollama unreachable (only if chat is enabled — otherwise irrelevant).
        if cfg.memory_chat_enabled():
            chat_provider = OllamaChatProvider(
                model=cfg.memory_chat_chat_model,
                endpoint=cfg.memory_chat_endpoint,
                timeout_seconds=2.0,
            )
            try:
                await chat_provider.list_available_models()
            except LocalLLMUnavailableError:
                items.append(
                    {
                        "kind": "ollama_unreachable",
                        "severity": "low",
                        "title": "Ollama unreachable",
                        "body": (
                            f"memory chat is ON but {cfg.memory_chat_endpoint} "
                            f"isn't responding. Run `ollama serve`."
                        ),
                        "since": now.isoformat(),
                        "action_url": "/chat",
                    }
                )

        # 5. Recent tool denials (last 24h, last 3).
        recent_denied = sorted(
            [
                e for e in events
                if e.kind == EventKind.TOOL_REJECTED and e.timestamp >= last_24h
            ],
            key=lambda e: e.timestamp,
            reverse=True,
        )[:3]
        for e in recent_denied:
            p = e.payload or {}
            items.append(
                {
                    "kind": "tool_denied",
                    "severity": "low",
                    "title": f"tool denied — {p.get('tool_name', '?')}",
                    "body": str(p.get("reason") or "")[:200],
                    "since": e.timestamp.isoformat(),
                    "action_url": f"/debug?event={e.id}",
                    "related_event_id": str(e.id),
                }
            )

        # Sort by severity then recency.
        sev_order = {"high": 0, "medium": 1, "low": 2}
        items.sort(key=lambda i: (sev_order.get(i["severity"], 9), i["since"]))
        return {"count": len(items), "items": items}

    @router.get("/api/dashboard/growth")
    async def dashboard_growth() -> dict[str, Any]:
        """What the exocortex has gained recently — the 'is this thing
        useful' signal."""
        events = await audit.read_all()
        now = datetime.now(tz=UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)
        yesterday_start = today_start - timedelta(days=1)

        records_today = sum(
            1 for e in events
            if e.kind == EventKind.MEMORY_WRITTEN and e.timestamp >= today_start
        )
        records_week = sum(
            1 for e in events
            if e.kind == EventKind.MEMORY_WRITTEN and e.timestamp >= week_ago
        )
        chat_queries_today = sum(
            1 for e in events
            if e.kind == EventKind.MEMORY_CHAT and e.timestamp >= today_start
        )

        # Top tags today (vs yesterday for a delta).
        all_records = await store.all_with_embeddings()
        today_tag_counts: dict[str, int] = {}
        yesterday_tag_counts: dict[str, int] = {}
        for r, _ in all_records:
            for tag in r.tags:
                if r.timestamp >= today_start:
                    today_tag_counts[tag] = today_tag_counts.get(tag, 0) + 1
                elif r.timestamp >= yesterday_start:
                    yesterday_tag_counts[tag] = yesterday_tag_counts.get(tag, 0) + 1
        top_tags_raw = sorted(
            today_tag_counts.items(), key=lambda kv: -kv[1]
        )[:6]
        top_tags = []
        for tag, count_today in top_tags_raw:
            prev = yesterday_tag_counts.get(tag, 0)
            delta_pct = (
                ((count_today - prev) / prev * 100.0) if prev else None
            )
            top_tags.append(
                {
                    "tag": tag,
                    "count_today": count_today,
                    "delta_pct": delta_pct,
                }
            )

        # Profile dimensions added this week.
        prof_growth: dict[str, int] = {}
        prof_questions_open = 0
        for r, _ in all_records:
            if r.scope.value != "user":
                continue
            if r.type == "profile.question":
                if any(t == "status:open" for t in r.tags):
                    prof_questions_open += 1
                continue
            if r.timestamp >= week_ago and r.type.startswith("profile."):
                prof_growth[r.type] = prof_growth.get(r.type, 0) + 1
        prof_growth_pairs = sorted(
            prof_growth.items(), key=lambda kv: -kv[1]
        )[:5]
        prof_growth_list = [
            {"dimension": k, "added_week": v} for k, v in prof_growth_pairs
        ]

        return {
            "records_today": records_today,
            "records_week": records_week,
            "chat_queries_today": chat_queries_today,
            "profile_questions_open": prof_questions_open,
            "top_tags": top_tags,
            "profile_dimensions_growing": prof_growth_list,
        }

    @router.get("/api/debug/failures")
    async def debug_failures(  # noqa: PLR0912 — multi-kind aggregation
        limit: int = Query(200, ge=1, le=2000),
        kind: str = Query("*"),
        agent: str = Query("*"),
        since_ms: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        events = await audit.read_all()

        # Build cross-event indexes so we can enrich historical
        # task.failed events that ship with empty payloads (those events
        # were emitted before TASK_FAILED carried `goal`/`error`):
        #   task_id → goal (from TASK_CREATED.payload.goal)
        #   task_id → owning agent (from HANDOFF_INITIATED.payload.to_agent)
        task_goals: dict[str, str] = {}
        task_owners: dict[str, str] = {}
        for ev in events:
            tid = str(ev.task_id) if ev.task_id else None
            if not tid:
                continue
            if ev.kind == EventKind.TASK_CREATED:
                goal = ev.payload.get("goal") if ev.payload else None
                if isinstance(goal, str) and goal:
                    task_goals[tid] = goal
            elif ev.kind == EventKind.HANDOFF_INITIATED and ev.payload:
                child = ev.payload.get("child_task_id")
                to_agent = ev.payload.get("to_agent")
                if (
                    isinstance(child, str)
                    and isinstance(to_agent, str)
                    and to_agent
                    and to_agent != "auto"
                ):
                    task_owners[child] = to_agent

        cutoff: datetime | None = (
            datetime.fromtimestamp(since_ms / 1000.0, tz=UTC) if since_ms else None
        )
        events.sort(key=lambda e: e.timestamp, reverse=True)
        items: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for ev in events:
            if not _is_failure_event(ev):
                continue
            counts[ev.kind.value] = counts.get(ev.kind.value, 0) + 1
            if cutoff is not None and ev.timestamp <= cutoff:
                continue
            if kind not in ("*", ev.kind.value):
                continue
            tid = str(ev.task_id) if ev.task_id else None
            owner_agent = task_owners.get(tid or "", "")
            effective_agent = ev.agent_id or owner_agent
            if agent not in ("*", effective_agent):
                continue
            severity = (
                "high" if ev.kind in (EventKind.DISPATCH_FAILED, EventKind.TASK_FAILED)
                else "medium" if ev.kind == EventKind.TOOL_REJECTED
                else "low"
            )
            preview = _event_preview(ev)
            # Back-fill goal for historical task.failed events whose
            # payloads pre-date the goal-carrying enrichment.
            if not preview and ev.kind == EventKind.TASK_FAILED and tid in task_goals:
                preview = task_goals[tid]
            items.append(
                {
                    "event_id": str(ev.id),
                    "kind": ev.kind.value,
                    "agent_id": effective_agent,
                    "timestamp": ev.timestamp.isoformat(),
                    "timestamp_ms": int(ev.timestamp.timestamp() * 1000),
                    "task_id": tid,
                    "session_id": str(ev.session_id) if ev.session_id else None,
                    "payload_preview": preview,
                    "severity": severity,
                }
            )
            if len(items) >= limit:
                break
        return {"count": len(items), "items": items, "counts_by_kind": counts}

    @router.get("/api/debug/failures/{event_id}/context")
    async def debug_failure_context(event_id: str) -> dict[str, Any]:
        events = await audit.read_all()
        target = next((e for e in events if str(e.id) == event_id), None)
        if target is None or not _is_failure_event(target):
            raise HTTPException(
                status_code=404, detail=f"failure event {event_id} not found"
            )
        scope_match = []
        for ev in events:
            if ev.timestamp >= target.timestamp:
                continue
            if (target.task_id and ev.task_id == target.task_id) or (
                target.session_id and ev.session_id == target.session_id
            ):
                scope_match.append(ev)
        scope_match.sort(key=lambda e: e.timestamp, reverse=True)
        preceding = [
            {
                "event_id": str(e.id),
                "kind": e.kind.value,
                "agent_id": e.agent_id or "",
                "timestamp": e.timestamp.isoformat(),
                "task_id": str(e.task_id) if e.task_id else None,
                "session_id": str(e.session_id) if e.session_id else None,
                "payload_preview": _event_preview(e),
            }
            for e in scope_match[:5]
        ]
        return {
            "event": {
                "event_id": str(target.id),
                "kind": target.kind.value,
                "agent_id": target.agent_id or "",
                "timestamp": target.timestamp.isoformat(),
                "task_id": str(target.task_id) if target.task_id else None,
                "session_id": str(target.session_id) if target.session_id else None,
                "payload": dict(target.payload),
                "payload_preview": _event_preview(target),
            },
            "preceding": preceding,
            "hints": _hint_for(target),
        }

    # --- Handoff chains -------------------------------------------------

    async def _build_chain_for_root(
        root_task_id: str,
        events_by_task: dict[str, list[Event]],
        parent_to_children: dict[str, list[str]],
        task_meta: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Walk down from root_task_id, returning the chain object."""
        ordered_tasks: list[dict[str, Any]] = []
        chain_events: list[dict[str, Any]] = []
        visited: set[str] = set()

        def _visit(tid: str) -> None:
            if tid in visited:
                return
            visited.add(tid)
            meta = task_meta.get(tid, {})
            ordered_tasks.append(
                {
                    "task_id": tid,
                    "agent_id": meta.get("agent_id"),
                    "parent_task_id": meta.get("parent_task_id"),
                    "started_at": meta.get("started_at"),
                    "ended_at": meta.get("ended_at"),
                    "status": meta.get("status", "unknown"),
                    "goal_preview": meta.get("goal_preview", ""),
                }
            )
            for ev in events_by_task.get(tid, []):
                chain_events.append(
                    {
                        "event_id": str(ev.id),
                        "kind": ev.kind.value,
                        "agent_id": ev.agent_id or "",
                        "timestamp_ms": int(ev.timestamp.timestamp() * 1000),
                        "parent_task_id": ev.payload.get("parent_task_id"),
                        "child_task_id": ev.payload.get("child_task_id"),
                        "from_agent": ev.payload.get("from_agent"),
                        "to_agent": ev.payload.get("to_agent"),
                    }
                )
            for child in parent_to_children.get(tid, []):
                _visit(child)

        _visit(root_task_id)

        agents_path: list[str] = []
        for t in ordered_tasks:
            aid = t.get("agent_id")
            if aid and (not agents_path or agents_path[-1] != aid):
                agents_path.append(aid)

        # Aggregate timing + status: chain runs from earliest started to
        # latest ended; status is failed if any task failed, completed if
        # all completed, otherwise running.
        starts = [t["started_at"] for t in ordered_tasks if t.get("started_at")]
        ends = [t["ended_at"] for t in ordered_tasks if t.get("ended_at")]
        statuses = [t.get("status") for t in ordered_tasks]
        chain_status = (
            "failed" if "failed" in statuses
            else "running" if any(s == "in_progress" for s in statuses)
            else "completed" if all(s == "completed" for s in statuses)
            else "mixed"
        )
        return {
            "chain_id": root_task_id,
            "hops": len({t.get("agent_id") for t in ordered_tasks if t.get("agent_id")}),
            "depth": len(ordered_tasks),
            "agents_path": agents_path,
            "started_at": min(starts) if starts else None,
            "ended_at": max(ends) if ends else None,
            "status": chain_status,
            "tasks": ordered_tasks,
            "events": chain_events,
        }

    async def _gather_chain_data() -> tuple[  # noqa: PLR0912 — multi-kind aggregation
        dict[str, list[Event]], dict[str, list[str]], dict[str, dict[str, Any]], list[str]
    ]:
        """Build the data structures used by both chain endpoints. Walks the
        audit log once, builds parent/child links from HANDOFF_INITIATED
        events (which carry parent_task_id + child_task_id) and from task
        creation events (which carry parent_task_id in inputs)."""
        events = await audit.read_all()
        events_by_task: dict[str, list[Event]] = {}
        task_meta: dict[str, dict[str, Any]] = {}
        parent_link: dict[str, str] = {}  # child → parent

        for ev in events:
            if ev.task_id is not None:
                events_by_task.setdefault(str(ev.task_id), []).append(ev)
            tid = str(ev.task_id) if ev.task_id else None
            if ev.kind == EventKind.TASK_CREATED and tid:
                meta = task_meta.setdefault(tid, {})
                meta["goal_preview"] = str(ev.payload.get("goal", ""))[:160]
                meta["started_at"] = ev.timestamp.isoformat()
                inputs = ev.payload.get("inputs") or {}
                parent = inputs.get("parent_task_id")
                if isinstance(parent, str):
                    parent_link[tid] = parent
            elif ev.kind == EventKind.HANDOFF_INITIATED and ev.payload:
                child = ev.payload.get("child_task_id")
                parent = ev.payload.get("parent_task_id")
                if isinstance(child, str) and isinstance(parent, str):
                    parent_link[child] = parent
                # The dispatched (child) task's owning agent is the
                # to_agent from the handoff payload — the audit log's
                # event-level agent_id on this event is "exocortex"
                # (the dispatcher itself).
                to_agent = ev.payload.get("to_agent")
                if isinstance(child, str) and isinstance(to_agent, str) and to_agent != "auto":
                    meta = task_meta.setdefault(child, {})
                    meta["agent_id"] = to_agent
            elif ev.kind == EventKind.TASK_STATUS_CHANGED and tid:
                to = ev.payload.get("to")
                if isinstance(to, str):
                    meta = task_meta.setdefault(tid, {})
                    meta["status"] = to
                    if to in ("completed", "failed", "cancelled"):
                        meta["ended_at"] = ev.timestamp.isoformat()
            elif ev.kind == EventKind.TASK_COMPLETED and tid:
                meta = task_meta.setdefault(tid, {})
                meta["status"] = "completed"
                meta["ended_at"] = ev.timestamp.isoformat()
            elif ev.kind == EventKind.TASK_FAILED and tid:
                meta = task_meta.setdefault(tid, {})
                meta["status"] = "failed"
                meta["ended_at"] = ev.timestamp.isoformat()
            if ev.agent_id and tid and ev.kind not in (
                EventKind.TASK_CREATED,
                EventKind.TASK_COMPLETED,
                EventKind.TASK_FAILED,
                EventKind.TASK_STATUS_CHANGED,
            ):
                # First non-housekeeping event with an agent_id sets the
                # task's owning agent.
                meta = task_meta.setdefault(tid, {})
                meta.setdefault("agent_id", ev.agent_id)

        # Build forward index parent → [children].
        parent_to_children: dict[str, list[str]] = {}
        for child, parent in parent_link.items():
            parent_to_children.setdefault(parent, []).append(child)

        # Roots = tasks that have no parent in our link map but DO appear
        # in task_meta.
        all_tasks = set(task_meta.keys())
        children_set = set(parent_link.keys())
        roots = sorted(all_tasks - children_set)
        return events_by_task, parent_to_children, task_meta, roots

    @router.get("/api/handoffs/chains")
    async def handoffs_chains(
        limit: int = Query(20, ge=1, le=200),
        min_depth: int = Query(1, ge=1, le=10),
        since_ms: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        events_by_task, parent_to_children, task_meta, roots = (
            await _gather_chain_data()
        )
        cutoff_ms: int | None = since_ms or None
        chains: list[dict[str, Any]] = []
        for root in roots:
            chain = await _build_chain_for_root(
                root, events_by_task, parent_to_children, task_meta
            )
            if chain["depth"] < min_depth:
                continue
            if cutoff_ms is not None:
                started = chain.get("started_at") or ""
                if not started:
                    continue
                try:
                    started_ms = int(
                        datetime.fromisoformat(started).timestamp() * 1000
                    )
                except (ValueError, TypeError):
                    continue
                if started_ms < cutoff_ms:
                    continue
            chains.append(chain)
        # Newest first.
        chains.sort(key=lambda c: c.get("started_at") or "", reverse=True)
        return {"count": len(chains[:limit]), "items": chains[:limit]}

    @router.get("/api/handoffs/chain/{task_id}")
    async def handoffs_chain_for_task(task_id: str) -> dict[str, Any]:
        events_by_task, parent_to_children, task_meta, roots = (
            await _gather_chain_data()
        )
        if task_id not in task_meta:
            raise HTTPException(
                status_code=404, detail=f"task {task_id} not found"
            )
        # Walk up to root from this task.
        parent_link: dict[str, str] = {}
        for parent, kids in parent_to_children.items():
            for k in kids:
                parent_link[k] = parent
        cur = task_id
        seen: set[str] = set()
        while cur in parent_link and cur not in seen:
            seen.add(cur)
            cur = parent_link[cur]
        return await _build_chain_for_root(
            cur, events_by_task, parent_to_children, task_meta
        )

    # --- Conversations -------------------------------------------------

    convo_service = ConversationService(audit=audit)
    convo_dispatcher = DispatchService(settings=cfg)

    @router.get("/api/conversations")
    async def conversations_list(
        status: str = Query("*", pattern="^(open|closed|\\*)$"),
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        items = await convo_service.list_rooms(status=status, limit=limit)
        return {"count": len(items), "items": [c.to_dict() for c in items]}

    @router.post("/api/conversations")
    async def conversations_create(request: Request) -> dict[str, Any]:
        body = await request.json()
        topic = str(body.get("topic") or "").strip()
        participants = body.get("participants") or []
        if not isinstance(participants, list):
            raise HTTPException(
                status_code=400, detail="participants must be a list"
            )
        try:
            convo = await convo_service.open(
                topic=topic,
                participants=[str(p) for p in participants],
                opened_by="operator",
            )
        except ConversationError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return convo.to_dict()

    @router.get("/api/conversations/{conversation_id}")
    async def conversations_get(conversation_id: str) -> dict[str, Any]:
        snap = await convo_service.get(conversation_id)
        if snap is None:
            raise HTTPException(
                status_code=404, detail=f"conversation {conversation_id} not found"
            )
        return snap

    @router.post("/api/conversations/{conversation_id}/turn")
    async def conversations_turn(
        conversation_id: str, request: Request
    ) -> dict[str, Any]:
        body = await request.json()
        try:
            t = await convo_service.add_turn(
                conversation_id=conversation_id,
                from_agent=str(body.get("from_agent") or "operator"),
                to_agent=str(body.get("to_agent") or ""),
                content=str(body.get("content") or ""),
                in_reply_to=body.get("in_reply_to"),
            )
        except ConversationError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {
            "turn_id": t.turn_id,
            "timestamp_ms": t.timestamp_ms,
        }

    @router.post("/api/conversations/{conversation_id}/close")
    async def conversations_close(conversation_id: str) -> dict[str, Any]:
        try:
            return await convo_service.close(
                conversation_id=conversation_id, closed_by="operator"
            )
        except ConversationError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.delete("/api/conversations/{conversation_id}")
    async def conversations_delete(conversation_id: str) -> dict[str, Any]:
        try:
            return await convo_service.delete(
                conversation_id=conversation_id, deleted_by="operator"
            )
        except ConversationError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.post("/api/conversations/{conversation_id}/run")
    async def conversations_run(
        conversation_id: str, request: Request
    ) -> dict[str, Any]:
        body = await request.json() if (await request.body()) else {}
        rounds = max(1, min(50, int(body.get("rounds", 1))))
        # Per-turn timeout. Default 300s — codex on a complex prompt can
        # take >2 minutes; 120s was too aggressive and caused most
        # conversation timeouts you see in /debug.
        max_wait_seconds = max(30, min(900, int(body.get("max_wait_seconds", 300))))
        try:
            results = await run_rounds(
                service=convo_service,
                dispatcher=convo_dispatcher,
                conversation_id=conversation_id,
                rounds=rounds,
                max_wait_seconds=max_wait_seconds,
            )
        except ConversationError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {
            "rounds": rounds,
            "max_wait_seconds": max_wait_seconds,
            "dispatched": results,
        }

    # --- Reflect (insights) ---------------------------------------------

    @router.get("/api/insights")
    async def list_insights(include_resolved: bool = False) -> dict[str, Any]:
        svc = ReflectionService(audit=audit)
        return {"items": await svc.list_insights(include_resolved=include_resolved)}

    @router.post("/api/insights/{insight_id}/dismiss")
    async def dismiss_insight(insight_id: str) -> dict[str, Any]:
        return await ReflectionService(audit=audit).dismiss(insight_id)

    @router.post("/api/insights/{insight_id}/accept")
    async def accept_insight(insight_id: str) -> dict[str, Any]:
        try:
            return await ReflectionService(audit=audit).accept(insight_id, apply=False)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @router.post("/api/insights/{insight_id}/act")
    async def act_insight(insight_id: str) -> dict[str, Any]:
        try:
            return await ReflectionService(audit=audit).accept(
                insight_id,
                apply=True,
                store=store,
                embedder=DeterministicEmbeddingProvider(),
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    # Required by FastAPI type checker — surface Request for future use.
    _ = Request
    return router


def _human_duration(td: timedelta) -> str:
    """Compact "3h17m" / "2d4h" / "45s" for UI display."""
    total = int(td.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        h = total // 3600
        m = (total % 3600) // 60
        return f"{h}h{m:02d}m" if m else f"{h}h"
    d = total // 86400
    h = (total % 86400) // 3600
    return f"{d}d{h}h" if h else f"{d}d"
