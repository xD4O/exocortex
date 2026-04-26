"""Pure handler layer for the exocortex MCP server.

Each public method is a callable primitive that takes already-constructed
collaborators (store, embedder, audit, retrieval) and returns a plain
dict. Tests call them directly; the FastMCP layer in `server.py` wraps
them as MCP tools.

Keeping this separation means: (a) we can test handler behavior without
spinning up the MCP protocol, and (b) swapping transports (e.g. HTTP MCP
later) is a re-wiring task, not a rewrite.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from exocortex.config import Settings
from exocortex.contracts import (
    Confidence,
    Event,
    EventKind,
    MemoryRecord,
    MemoryScope,
)
from exocortex.coordination.conversation import (
    ConversationError,
    ConversationService,
)
from exocortex.memory.chat import MemoryChatService
from exocortex.memory.dedup import _delete_record, find_dedup_clusters, merge_records
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import EmbeddingProvider
from exocortex.memory.llm import LocalLLMUnavailableError, OllamaChatProvider
from exocortex.memory.profile import ProfileService
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.observability.audit import AuditLog
from exocortex.operator.recall import RecallService


def _record_to_dict(r: MemoryRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "type": r.type,
        "content": r.content,
        "source": r.source,
        "confidence": r.confidence.value,
        "scope": r.scope.value,
        "scope_id": r.scope_id,
        "tags": list(r.tags),
        "timestamp": r.timestamp.isoformat(),
    }


def _event_to_dict(e: Event) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "kind": e.kind.value,
        "timestamp": e.timestamp.isoformat(),
        "task_id": str(e.task_id) if e.task_id else None,
        "session_id": str(e.session_id) if e.session_id else None,
        "agent_id": e.agent_id,
        "payload": dict(e.payload),
    }


class MemoryHandlers:
    """Stateless facade over the memory + audit stack.

    The instance holds references to the concrete stores so handlers can be
    called without re-wiring; construction is cheap enough that a single
    instance per process is fine.
    """

    def __init__(
        self,
        *,
        store: DurableMemoryStore,
        embedder: EmbeddingProvider,
        retrieval: HybridRetrieval,
        audit: AuditLog,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.retrieval = retrieval
        self.audit = audit
        self.settings = settings or Settings()
        self.recall = RecallService(store=store, audit=audit)

    async def memory_chat(
        self,
        *,
        question: str,
        top_k: int = 8,
        scope: str | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        """Natural-language query over the memory store. Off by default —
        operator must enable via `precog chat-toggle on`. Returns a
        grounded answer + citations, never writes back to memory."""
        if not self.settings.memory_chat_enabled():
            return {
                "status": "disabled",
                "error": (
                    "memory chat is OFF. Enable with `precog chat-toggle on` "
                    "or via the UI header toggle."
                ),
            }
        scope_enum = MemoryScope(scope) if scope else None
        chat_provider = OllamaChatProvider(
            model=self.settings.memory_chat_chat_model,
            endpoint=self.settings.memory_chat_endpoint,
            timeout_seconds=float(self.settings.memory_chat_timeout_seconds),
            max_tokens=self.settings.memory_chat_max_tokens,
        )
        service = MemoryChatService(
            store=self.store,
            retrieval=self.retrieval,
            chat_provider=chat_provider,
            audit=self.audit,
            embedding_model_name=self.settings.memory_chat_embedding_model,
        )
        try:
            response = await service.ask(
                question=question,
                top_k=top_k,
                scope=scope_enum,
                scope_id=scope_id,
            )
        except LocalLLMUnavailableError as e:
            return {"status": "llm_unavailable", "error": str(e)}
        return {"status": "ok", **response.to_dict()}

    def _conversation_service(self) -> ConversationService:
        return ConversationService(audit=self.audit)

    async def conversation_start(
        self,
        *,
        topic: str,
        participants: list[str],
        opened_by: str = "operator",
    ) -> dict[str, Any]:
        """Open a new conversation room for ≥2 agents on a topic."""
        try:
            convo = await self._conversation_service().open(
                topic=topic, participants=list(participants), opened_by=opened_by
            )
        except ConversationError as e:
            return {"status": "error", "error": str(e)}
        return {"status": "ok", **convo.to_dict()}

    async def conversation_turn(
        self,
        *,
        conversation_id: str,
        from_agent: str,
        to_agent: str,
        content: str,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Add one turn to an open conversation. Audit-logged."""
        try:
            t = await self._conversation_service().add_turn(
                conversation_id=conversation_id,
                from_agent=from_agent,
                to_agent=to_agent,
                content=content,
                in_reply_to=in_reply_to,
            )
        except ConversationError as e:
            return {"status": "error", "error": str(e)}
        return {
            "status": "ok",
            "turn_id": t.turn_id,
            "timestamp_ms": t.timestamp_ms,
        }

    async def conversation_inbox(
        self, *, agent_id: str, limit: int = 20, since_ms: int = 0
    ) -> dict[str, Any]:
        """Pending messages addressed to you in any open conversation."""
        items = await self._conversation_service().inbox(
            agent_id=agent_id, limit=limit, since_ms=since_ms
        )
        return {"count": len(items), "items": items}

    async def conversation_history(
        self, *, conversation_id: str
    ) -> dict[str, Any]:
        snap = await self._conversation_service().get(conversation_id)
        if snap is None:
            return {"status": "not_found", "conversation_id": conversation_id}
        return {"status": "ok", **snap}

    async def conversation_close(
        self, *, conversation_id: str, closed_by: str = "operator"
    ) -> dict[str, Any]:
        try:
            return {
                "status": "ok",
                **await self._conversation_service().close(
                    conversation_id=conversation_id, closed_by=closed_by
                ),
            }
        except ConversationError as e:
            return {"status": "error", "error": str(e)}

    async def conversation_delete(
        self, *, conversation_id: str, deleted_by: str = "operator"
    ) -> dict[str, Any]:
        try:
            return {
                "status": "ok",
                **await self._conversation_service().delete(
                    conversation_id=conversation_id, deleted_by=deleted_by
                ),
            }
        except ConversationError as e:
            return {"status": "error", "error": str(e)}

    def _profile_service(self) -> ProfileService:
        return ProfileService(
            store=self.store,
            embedder=self.embedder,
            retrieval=self.retrieval,
            audit=self.audit,
            user_id=self.settings.profile_user_id,
            frozen=self.settings.profile_frozen(),
        )

    async def profile_observe(
        self,
        *,
        content: str,
        type: str = "preference",
        confidence: str = "inferred",
        evidence_record_ids: list[str] | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Drop a candidate fact about the operator into USER-scope memory.

        Use sparingly — observations get audited; the operator can review
        and redact via `/profile`. When the freeze flag is set, returns
        `{"status": "frozen"}` and writes nothing.
        """
        try:
            confidence_enum = Confidence(confidence)
        except ValueError as e:
            raise ValueError(f"unknown confidence {confidence!r}") from e
        service = self._profile_service()
        record = await service.observe(
            content=content,
            type=type,
            source=agent_id or "external",
            confidence=confidence_enum,
            evidence_record_ids=list(evidence_record_ids or []),
            agent_id=agent_id,
        )
        if record is None:
            return {
                "status": "frozen",
                "error": "profile collection is paused — operator must unfreeze",
            }
        return {
            "status": "ok",
            "record_id": str(record.id),
            "type": record.type,
        }

    async def profile_recall(
        self,
        *,
        question: str,
        top_k: int = 8,
    ) -> dict[str, Any]:
        """RAG-style retrieval restricted to USER-scope records. Use this
        before answering anything where the operator's preferences,
        constraints, goals, or communication style might matter."""
        service = self._profile_service()
        hits = await service.recall(question, top_k=top_k)
        return {
            "count": len(hits),
            "records": [
                {
                    "score": score,
                    **_record_to_dict(record),
                }
                for record, score in hits
            ],
        }

    async def profile_freeze_toggle(self) -> dict[str, Any]:
        """Flip the master switch on profile collection. When frozen, all
        `profile_observe` calls return `{"status": "frozen"}` and no records
        are written. Persistent across processes via a flag-file."""
        path = self.settings.profile_freeze_path
        if path.exists():
            path.unlink()
            new_state = False
        else:
            self.settings.ensure_dirs()
            path.write_text("frozen\n")
            new_state = True
        await self.audit.record(
            Event(
                kind=EventKind.PROFILE_FROZEN_TOGGLED,
                agent_id="operator",
                payload={"frozen": new_state},
            )
        )
        return {"frozen": new_state}

    async def profile_questions(
        self, *, status: str = "open", limit: int = 5
    ) -> dict[str, Any]:
        """Return the queue of questions the exocortex would like to ask
        the operator. Default status filter is `open`. Pass `*` for all."""
        service = self._profile_service()
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
                for q in questions[:limit]
            ],
        }

    async def profile_answer(
        self,
        *,
        question_id: str,
        answer: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Mark a question answered, append the answer as a profile record."""
        service = self._profile_service()
        return await service.answer(
            question_id=question_id, answer=answer, agent_id=agent_id
        )

    async def session_startup(
        self, *, agent_id: str | None = None
    ) -> dict[str, Any]:
        """Fetch "here's what we were working on" for a fresh session.

        Agents should call this on their FIRST TURN of a new session. The
        returned `text_for_user` is a ready-to-show summary; render it so
        the operator can pick up where they left off or start fresh.
        """
        summary = await self.recall.summarize(agent_id=agent_id)
        # Record the recall so the audit log shows when agents reconnected.
        await self.audit.record(
            Event(
                kind=EventKind.SESSION_OPENED,
                agent_id=agent_id or "external",
                payload={
                    "via": "mcp.session_startup",
                    "unfinished_count": len(summary.unfinished_tasks),
                    "recent_decisions": len(summary.recent_decisions),
                },
            )
        )
        out = summary.to_dict()
        # Augment with a profile voice-prefix so agents starting a session
        # know how to talk to the operator. Best-effort — empty string on
        # any failure means agents just use their default voice.
        try:
            voice = await self._profile_service().voice_prefix()
        except Exception:  # noqa: BLE001
            voice = ""
        out["profile_voice"] = voice
        return out

    # --- Writes ------------------------------------------------------------

    async def memory_write(
        self,
        *,
        content: str,
        source: str = "external",
        scope: str = "project",
        scope_id: str = "default",
        type: str = "observation",
        confidence: str = "observed",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Persist a memory record + publish a `memory.written` event so the
        UI flashes a new star in real time.

        Scope must be one of: session, task, project, global.
        Confidence must be one of: observed, inferred, asserted, external_claim.
        """
        try:
            scope_enum = MemoryScope(scope)
        except ValueError as e:
            raise ValueError(
                f"invalid scope {scope!r}; use session|task|project|global"
            ) from e
        try:
            conf_enum = Confidence(confidence)
        except ValueError as e:
            raise ValueError(
                f"invalid confidence {confidence!r}; use observed|inferred|asserted|external_claim"
            ) from e

        record = MemoryRecord(
            content=content,
            source=source,
            scope=scope_enum,
            scope_id=scope_id,
            type=type,
            confidence=conf_enum,
            tags=list(tags or []),
        )
        await self.store.write(record, embedding=self.embedder.embed(content))
        await self.audit.record(
            Event(
                kind=EventKind.MEMORY_WRITTEN,
                agent_id=source,
                payload={
                    "record_id": str(record.id),
                    "durable": True,
                    "via": "mcp",
                    "scope": scope_enum.value,
                    "scope_id": scope_id,
                },
            )
        )
        return {
            "id": str(record.id),
            "scope": scope_enum.value,
            "scope_id": scope_id,
            "timestamp": record.timestamp.isoformat(),
        }

    # --- Reads -------------------------------------------------------------

    async def memory_search(
        self,
        *,
        query: str,
        scope: str | None = None,
        scope_id: str | None = None,
        limit: int = 10,
        alpha: float = 0.5,
    ) -> dict[str, Any]:
        """Hybrid (keyword + semantic) retrieval over the shared memory store.

        Call this at the start of a session to recall prior context. Use
        alpha=1.0 for keyword-only, 0.0 for semantic-only, 0.5 for balanced.
        """
        scope_enum = MemoryScope(scope) if scope else None
        hits = await self.retrieval.search(
            query,
            scope=scope_enum,
            scope_id=scope_id,
            limit=limit,
            alpha=alpha,
        )
        return {
            "query": query,
            "count": len(hits),
            "results": [
                {**_record_to_dict(r), "score": score} for r, score in hits
            ],
        }

    async def memory_list(
        self, *, scope: str, scope_id: str, limit: int = 50
    ) -> dict[str, Any]:
        """Enumerate records in a specific scope, most-recent-last."""
        records = await self.store.list_by_scope(MemoryScope(scope), scope_id)
        if limit and len(records) > limit:
            records = records[-limit:]
        return {
            "scope": scope,
            "scope_id": scope_id,
            "count": len(records),
            "records": [_record_to_dict(r) for r in records],
        }

    async def memory_forget(self, *, record_id: str) -> dict[str, Any]:
        """Hard-delete one memory record by UUID. Audit-logged. The
        record's content is gone after this call; the audit log retains
        the fact that it existed and was forgotten (immutability of the
        audit log is preserved)."""
        try:
            rid = UUID(record_id)
        except ValueError as e:
            raise ValueError(f"invalid UUID: {record_id}") from e
        existing = await self.store.get(rid)
        if existing is None:
            return {"status": "not_found", "record_id": record_id}
        deleted = await _delete_record(self.store, record_id)
        if deleted:
            await self.audit.record(
                Event(
                    kind=EventKind.MEMORY_FORGOTTEN,
                    agent_id=existing.source,
                    payload={
                        "record_id": record_id,
                        "scope": existing.scope.value,
                        "scope_id": existing.scope_id,
                        "content_preview": existing.content[:120],
                    },
                )
            )
        return {
            "status": "forgotten" if deleted else "not_found",
            "record_id": record_id,
        }

    async def memory_dedup_clusters(
        self,
        *,
        scope: str | None = None,
        scope_id: str | None = None,
        threshold: float = 0.92,
    ) -> dict[str, Any]:
        """Find clusters of near-duplicate records. Reports only — does
        not mutate. Use memory_merge to act on a cluster."""
        scope_enum = MemoryScope(scope) if scope else None
        clusters = await find_dedup_clusters(
            self.store, scope=scope_enum, scope_id=scope_id, threshold=threshold
        )
        return {
            "threshold": threshold,
            "cluster_count": len(clusters),
            "clusters": [
                {
                    "size": c.size,
                    "canonical": _record_to_dict(c.canonical),
                    "duplicates": [_record_to_dict(d) for d in c.duplicates],
                }
                for c in clusters
            ],
        }

    async def memory_merge(
        self, *, keep_id: str, drop_ids: list[str]
    ) -> dict[str, Any]:
        """Delete the records in drop_ids; the canonical (keep_id) stays.
        Audit-logged."""
        kept = await self.store.get(UUID(keep_id))
        if kept is None:
            raise ValueError(f"keep_id {keep_id} not found")
        removed = await merge_records(
            self.store, keep_id=keep_id, drop_ids=list(drop_ids)
        )
        await self.audit.record(
            Event(
                kind=EventKind.MEMORY_MERGED,
                agent_id=kept.source,
                payload={
                    "keep_id": keep_id,
                    "drop_ids": list(drop_ids),
                    "removed_count": removed,
                },
            )
        )
        return {
            "keep_id": keep_id,
            "removed_count": removed,
            "remaining": _record_to_dict(kept),
        }

    async def memory_get(self, *, record_id: str) -> dict[str, Any] | None:
        """Fetch one memory record by UUID. Returns None if not found."""
        try:
            rid = UUID(record_id)
        except ValueError as e:
            raise ValueError(f"invalid UUID: {record_id}") from e
        record = await self.store.get(rid)
        return _record_to_dict(record) if record else None

    async def trace_recent(
        self, *, task_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Recent events from the audit log, optionally filtered to a task
        prefix. Useful for a fresh agent session to see what happened last.
        """
        events = await self.audit.read_all()
        if task_id:
            events = [
                e for e in events
                if e.task_id is not None and str(e.task_id).startswith(task_id)
            ]
        events = events[-limit:]
        return {
            "count": len(events),
            "events": [_event_to_dict(e) for e in events],
        }

    async def agents_list(self) -> dict[str, Any]:
        """List the agent capabilities exocortex knows about. Mirrors the
        `capability()` declarations in each Bridge subclass; kept in sync
        manually because this is a cross-process stable interface, not a
        runtime registry."""
        return {"count": len(_AGENT_CAPABILITIES), "agents": list(_AGENT_CAPABILITIES)}


# Mirror of Bridge.capability() declarations. If you change a Bridge's
# capability flags, update this list too.
_AGENT_CAPABILITIES: list[dict[str, Any]] = [
    {
        "agent_id": "codex",
        "name": "ChatGPT Codex",
        "kind": "bridge",
        "capabilities": [
            "edit_files",
            "run_shell",
            "long_context",
            "structured_output",
            "mcp_client",
            "mcp_server",
            "interactive",
        ],
    },
    {
        "agent_id": "claude_code",
        "name": "Claude Code",
        "kind": "bridge",
        "capabilities": [
            "edit_files",
            "run_shell",
            "long_context",
            "structured_output",
            "mcp_client",
            "mcp_server",
            "interactive",
        ],
    },
    {
        "agent_id": "hermes",
        "name": "Nous Research Hermes",
        "kind": "bridge",
        "capabilities": [
            "edit_files",
            "run_shell",
            "long_context",
            "structured_output",
            "mcp_client",
            "mcp_server",
            "interactive",
        ],
    },
]
