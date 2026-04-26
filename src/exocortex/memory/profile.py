"""User-profile memory layer.

Builds up facts about the operator themselves — preferences, skills, goals,
constraints, routines, communication style, relationships, values — across
agents, sessions, and time. Distinct from project/task memory: those are
about the *work*, this is about *the person doing the work*.

Architecturally a thin lens over the existing memory store, with USER scope
and `profile.*` record types. The promotion ladder still applies (multiple
agents corroborating an inference → confidence bumps), and the audit log
still captures every observation. What's new:

- Heuristic gap analysis: a target schema names which dimensions we'd like
  to understand (preference, skill, goal, ...) and how many records is
  "enough" coverage. Dimensions with thinner-than-target coverage drive
  the question queue.

- Question queue: open questions live as `profile.question` records with a
  `status:open` tag. Operator answers them via `precog profile answer` /
  the web UI. Answering creates a new `profile.<dimension>` record and
  flips the question's tag to `status:answered`.

The chat layer is read-only against this store too — agents observe via
`profile_observe`, never via free-form chat hallucination.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import anyio

from exocortex.contracts import (
    Confidence,
    Event,
    EventKind,
    MemoryRecord,
    MemoryScope,
)
from exocortex.memory.dedup import _delete_record
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import EmbeddingProvider
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.observability.audit import AuditLog


async def _update_tags(
    store: DurableMemoryStore, record_id: UUID, tags: list[str]
) -> None:
    """Tags are stored as JSON in the durable store; the model rebuilds
    them on read. Update in place via the connection — same approach as
    `_delete_record` in dedup.py."""

    def _sync() -> None:
        store._conn.execute(  # noqa: SLF001
            "UPDATE memory_records SET tags_json = ? WHERE id = ?",
            (json.dumps(tags), str(record_id)),
        )
        store._conn.commit()  # noqa: SLF001

    async with store._lock:  # noqa: SLF001
        await anyio.to_thread.run_sync(_sync)

# Canonical profile dimensions + target record counts. Dimensions outside
# this list are still allowed (free-form `profile.*`) — they just don't
# drive gap analysis. The phrasings here are also the templates we fall
# back to when we generate questions for an under-covered dimension.
PROFILE_DIMENSIONS: tuple[tuple[str, int, str], ...] = (
    (
        "profile.preference",
        5,
        "What tools, styles, or workflows do you prefer when working?",
    ),
    (
        "profile.skill",
        5,
        "What are some skills or expertise areas you'd want me to know about?",
    ),
    (
        "profile.goal",
        3,
        "What are your current goals — work or personal — that I should keep in mind?",
    ),
    (
        "profile.constraint",
        3,
        "What constraints affect how you work? (timezone, deep-work hours, no-meeting zones, etc.)",
    ),
    (
        "profile.routine",
        3,
        "Are there daily or weekly routines I should know about?",
    ),
    (
        "profile.communication_style",
        3,
        "How do you prefer to be communicated with? (terse vs detailed, blunt vs diplomatic, etc.)",
    ),
    (
        "profile.relationship",
        3,
        "Who are key people I should remember — collaborators, family, mentors?",
    ),
    (
        "profile.value",
        3,
        "What do you value highly? (in work, in tools, in life)",
    ),
)


class ProfileFrozenError(RuntimeError):
    """Raised when an observe is attempted while the freeze flag is set."""


@dataclass(frozen=True)
class ProfileQuestion:
    record_id: str
    content: str
    dimension: str
    asked_at: str
    status: str  # "open" | "answered" | "skipped"


@dataclass(frozen=True)
class ProfileGap:
    dimension: str
    target_count: int
    current_count: int
    suggested_question: str

    @property
    def gap(self) -> int:
        return max(0, self.target_count - self.current_count)


@dataclass
class ProfileService:
    """Lens over USER-scope memory + the question queue."""

    store: DurableMemoryStore
    embedder: EmbeddingProvider
    retrieval: HybridRetrieval
    audit: AuditLog
    user_id: str = "operator"
    frozen: bool = False
    # Optional fields populated by the caller; kept here so handlers don't
    # need to thread settings through every method.
    extra_tags: list[str] = field(default_factory=list)

    # --- Reads --------------------------------------------------------------

    async def list_records(
        self, *, dimensions: tuple[str, ...] | None = None
    ) -> list[MemoryRecord]:
        records = await self.store.list_by_scope(MemoryScope.USER, self.user_id)
        if dimensions:
            allowed = set(dimensions)
            records = [r for r in records if r.type in allowed]
        # Newest first for display.
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records

    async def find_gaps(self) -> list[ProfileGap]:
        records = await self.list_records()
        counts: dict[str, int] = {}
        for r in records:
            counts[r.type] = counts.get(r.type, 0) + 1
        gaps: list[ProfileGap] = []
        for dim, target, question in PROFILE_DIMENSIONS:
            current = counts.get(dim, 0)
            if current < target:
                gaps.append(
                    ProfileGap(
                        dimension=dim,
                        target_count=target,
                        current_count=current,
                        suggested_question=question,
                    )
                )
        gaps.sort(key=lambda g: -g.gap)
        return gaps

    async def list_questions(
        self, *, status: str = "open"
    ) -> list[ProfileQuestion]:
        records = await self.list_records(dimensions=("profile.question",))
        out: list[ProfileQuestion] = []
        for r in records:
            tags = list(r.tags)
            status_tag = next(
                (t for t in tags if t.startswith("status:")), "status:open"
            )
            current = status_tag.removeprefix("status:")
            if status not in ("*", current):
                continue
            dim_tag = next(
                (t for t in tags if t.startswith("dimension:")), "dimension:unknown"
            )
            out.append(
                ProfileQuestion(
                    record_id=str(r.id),
                    content=r.content,
                    dimension=dim_tag.removeprefix("dimension:"),
                    asked_at=r.timestamp.isoformat(),
                    status=current,
                )
            )
        return out

    async def recall(
        self, question: str, *, top_k: int = 8
    ) -> list[tuple[MemoryRecord, float]]:
        """RAG-style retrieval restricted to USER-scope records."""
        return await self.retrieval.search(
            question,
            scope=MemoryScope.USER,
            scope_id=self.user_id,
            limit=top_k,
            alpha=1.0,
        )

    async def voice_prefix(self) -> str:
        """Concatenated communication-style + value records that an agent can
        prepend to its system prompt. Truncated to a reasonable length so we
        don't blow context budgets."""
        records = await self.list_records(
            dimensions=("profile.communication_style", "profile.value")
        )
        if not records:
            return ""
        lines = ["Operator profile (use as priors, not facts):"]
        for r in records[:6]:
            lines.append(f"- {r.content}")
        return "\n".join(lines)

    # --- Writes -------------------------------------------------------------

    async def observe(
        self,
        *,
        content: str,
        type: str = "profile.preference",
        source: str = "external",
        confidence: Confidence = Confidence.INFERRED,
        evidence_record_ids: list[str] | None = None,
        agent_id: str | None = None,
    ) -> MemoryRecord | None:
        """Drop a candidate profile fact. No-ops (and audits the rejection)
        when the freeze flag is set so callers always know what happened.
        """
        if self.frozen:
            await self.audit.record(
                Event(
                    kind=EventKind.PROFILE_OBSERVED,
                    agent_id=agent_id or source,
                    payload={
                        "status": "frozen",
                        "would_have_recorded": content[:200],
                        "type": type,
                    },
                )
            )
            return None

        if not type.startswith("profile."):
            type = f"profile.{type}"

        evidence = list(evidence_record_ids or [])
        tags = [f"evidence:{rid}" for rid in evidence] + list(self.extra_tags)

        record = MemoryRecord(
            type=type,
            content=content.strip(),
            source=source,
            confidence=confidence,
            scope=MemoryScope.USER,
            scope_id=self.user_id,
            tags=tags,
        )
        await self.store.write(
            record, embedding=self.embedder.embed(record.content)
        )
        await self.audit.record(
            Event(
                kind=EventKind.PROFILE_OBSERVED,
                agent_id=agent_id or source,
                payload={
                    "record_id": str(record.id),
                    "type": type,
                    "content_preview": content[:200],
                    "evidence": evidence,
                    "confidence": confidence.value,
                },
            )
        )
        return record

    async def question(
        self,
        *,
        content: str,
        dimension: str,
        agent_id: str | None = None,
    ) -> MemoryRecord:
        """Append an open question to the queue."""
        if not dimension.startswith("profile."):
            dimension = f"profile.{dimension}"
        tags = [f"dimension:{dimension}", "status:open"]
        record = MemoryRecord(
            type="profile.question",
            content=content.strip(),
            source=agent_id or "exocortex",
            confidence=Confidence.OBSERVED,
            scope=MemoryScope.USER,
            scope_id=self.user_id,
            tags=tags,
        )
        await self.store.write(
            record, embedding=self.embedder.embed(record.content)
        )
        await self.audit.record(
            Event(
                kind=EventKind.PROFILE_QUESTIONED,
                agent_id=agent_id or "exocortex",
                payload={
                    "record_id": str(record.id),
                    "dimension": dimension,
                    "content_preview": content[:200],
                },
            )
        )
        return record

    async def answer(
        self,
        *,
        question_id: str,
        answer: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Close an open question and create the corresponding profile record."""
        try:
            qid = UUID(question_id)
        except (ValueError, TypeError) as e:
            raise ValueError(f"invalid question_id {question_id!r}") from e

        existing = await self.store.get(qid)
        if existing is None or existing.type != "profile.question":
            raise ValueError(f"question {question_id} not found")

        # Derive dimension from the question's tags.
        dim_tag = next(
            (t for t in existing.tags if t.startswith("dimension:")),
            "dimension:profile.preference",
        )
        dimension = dim_tag.removeprefix("dimension:")

        # New profile record carrying the answer.
        new_record = MemoryRecord(
            type=dimension,
            content=answer.strip(),
            source=agent_id or "operator",
            confidence=Confidence.ASSERTED,  # operator-asserted via answer
            scope=MemoryScope.USER,
            scope_id=self.user_id,
            tags=[f"answers:{question_id}"],
        )
        await self.store.write(
            new_record, embedding=self.embedder.embed(new_record.content)
        )

        # Mark the original question as answered.
        new_tags = [t for t in existing.tags if not t.startswith("status:")] + [
            "status:answered",
            f"answer:{new_record.id}",
        ]
        await _update_tags(self.store, qid, new_tags)

        await self.audit.record(
            Event(
                kind=EventKind.PROFILE_ANSWERED,
                agent_id=agent_id or "operator",
                payload={
                    "question_id": question_id,
                    "answer_record_id": str(new_record.id),
                    "dimension": dimension,
                },
            )
        )
        return {
            "status": "answered",
            "new_record_id": str(new_record.id),
            "dimension": dimension,
        }

    async def redact(
        self,
        *,
        record_id: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Hard-delete a USER-scope record. Audits the deletion with a content
        preview so it's reproducible (audit log is append-only)."""
        try:
            rid = UUID(record_id)
        except (ValueError, TypeError) as e:
            raise ValueError(f"invalid record_id {record_id!r}") from e
        existing = await self.store.get(rid)
        if existing is None:
            return {"status": "not_found"}
        if existing.scope != MemoryScope.USER:
            raise ValueError(
                f"record {record_id} is not USER-scope; refuse to redact via profile API"
            )
        await _delete_record(self.store, str(rid))
        await self.audit.record(
            Event(
                kind=EventKind.PROFILE_REDACTED,
                agent_id=agent_id or "operator",
                payload={
                    "record_id": record_id,
                    "type": existing.type,
                    "content_preview": existing.content[:200],
                },
            )
        )
        return {"status": "redacted", "record_id": record_id}

    async def export(self) -> dict[str, Any]:
        """Full dump of every USER-scope record for the operator. Used by
        `precog profile export` for data portability."""
        records = await self.list_records()
        return {
            "user_id": self.user_id,
            "frozen": self.frozen,
            "count": len(records),
            "records": [
                {
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
                for r in records
            ],
        }
