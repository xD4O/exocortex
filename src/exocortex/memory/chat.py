"""Memory chat — RAG over the exocortex memory store.

Question → hybrid retrieval over memory → prompt with cited records →
local chat model → grounded answer + citations. The chat layer is
read-only (no memory writes from chat). Each invocation emits a
`MEMORY_CHAT` audit event so chats are reproducible after the fact.

See docs/memory-chat-plan.md for the full design.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from exocortex.contracts import Event, EventKind, MemoryRecord, MemoryScope
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.llm import ChatMessage, OllamaChatProvider
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.observability.audit import AuditLog

_SYSTEM_PROMPT = (
    "You are the librarian for an operator's exocortex — a durable, shared "
    "memory store of decisions, observations, and questions across multiple "
    "AI coding agents (Claude Code, Codex, Hermes).\n\n"
    "Answer the user's question using ONLY the memory records provided "
    "below. If the records don't contain the answer, say so plainly — do "
    "not speculate, do not draw from outside knowledge.\n\n"
    "When you make a claim, cite the relevant record id(s) inline using "
    "the format `[id:abcd1234]` (the first 8 characters of the record's "
    "UUID is enough). Cite every claim that comes from a record.\n\n"
    "Be concise. The operator already knows the records exist; they want "
    "the synthesis, not a recap. Three sentences is often the right answer."
)


@dataclass(frozen=True)
class ChatResponse:
    answer: str
    cited_record_ids: list[str]
    retrieved_record_ids: list[str]
    model: str
    embedding_model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "cited_record_ids": list(self.cited_record_ids),
            "retrieved_record_ids": list(self.retrieved_record_ids),
            "model": self.model,
            "embedding_model": self.embedding_model,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass
class MemoryChatService:
    store: DurableMemoryStore
    retrieval: HybridRetrieval
    chat_provider: OllamaChatProvider
    audit: AuditLog
    embedding_model_name: str = "deterministic-stub"
    # Optional: if provided, used for prompt-time embedding queries (v1+).
    # For v0 we drive retrieval with the existing deterministic embedder
    # plus FTS keyword scoring (alpha=1.0).
    semantic_embedder: Any | None = field(default=None)

    async def ask(
        self,
        *,
        question: str,
        top_k: int = 8,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        alpha: float = 1.0,
    ) -> ChatResponse:
        if not question.strip():
            raise ValueError("question must not be empty")
        started = time.monotonic()

        # Retrieve top-K candidate records. v0 = FTS-dominant. Sanitize
        # the question for FTS5 — operator questions naturally end with
        # `?`, contain quotes, etc., which are FTS syntax tokens.
        retrieval_query = _sanitize_fts_query(question)
        hits = await self.retrieval.search(
            retrieval_query, scope=scope, scope_id=scope_id, limit=top_k, alpha=alpha
        )
        retrieved_ids = [str(r.id) for r, _ in hits]

        prompt = _build_prompt(question, [r for r, _ in hits])
        completion = await self.chat_provider.chat(
            [
                ChatMessage(role="system", content=_SYSTEM_PROMPT),
                ChatMessage(role="user", content=prompt),
            ]
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        # Best-effort citation parse: any [id:xxxxxxxx] referenced in the
        # answer. Match against the retrieved set; ignore hallucinated ids.
        cited = _extract_citations(completion.answer, retrieved_ids)

        await self.audit.record(
            Event(
                kind=EventKind.MEMORY_CHAT,
                agent_id="memory_chat",
                payload={
                    "question": question,
                    "model": completion.model,
                    "embedding_model": self.embedding_model_name,
                    "retrieved_record_ids": retrieved_ids,
                    "cited_record_ids": cited,
                    "latency_ms": latency_ms,
                    "input_tokens": completion.input_tokens,
                    "output_tokens": completion.output_tokens,
                },
            )
        )

        return ChatResponse(
            answer=completion.answer,
            cited_record_ids=cited,
            retrieved_record_ids=retrieved_ids,
            model=completion.model,
            embedding_model=self.embedding_model_name,
            latency_ms=latency_ms,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
        )


def _build_prompt(question: str, records: list[MemoryRecord]) -> str:
    if not records:
        return (
            f"No memory records matched the question. Tell the operator that "
            f"the exocortex has no relevant context.\n\nQUESTION: {question}"
        )
    lines = [
        "MEMORY RECORDS (use these to ground your answer; cite them by id):",
        "",
    ]
    for r in records:
        short_id = str(r.id)[:8]
        meta = (
            f"id:{short_id} type:{r.type} source:{r.source} "
            f"confidence:{r.confidence.value} scope:{r.scope.value}:{r.scope_id} "
            f"timestamp:{r.timestamp.isoformat()[:19]}"
        )
        lines.append(f"[{meta}]")
        lines.append(r.content)
        lines.append("")
    lines.append(f"QUESTION: {question}")
    return "\n".join(lines)


def _sanitize_fts_query(question: str) -> str:
    # FTS5 treats `?`, `"`, `(`, `)`, `*`, `:`, `-` as syntax. Strip them
    # and collapse remaining tokens — we want a bag-of-words over content,
    # not a full FTS expression. Empty result falls back to a single
    # token that won't match anything (retrieval still merges in semantic
    # candidates separately).
    cleaned = re.sub(r"[^\w\s]+", " ", question, flags=re.UNICODE)
    tokens = [t for t in cleaned.split() if t]
    return " ".join(tokens) if tokens else "exocortex"


def _extract_citations(answer: str, valid_ids: list[str]) -> list[str]:
    # Citations look like [id:abcd1234] (first 8 chars). Map back to full
    # UUIDs from the retrieved set so the caller (and the UI) can highlight
    # the actual records.
    valid_prefixes = {vid[:8]: vid for vid in valid_ids}
    cited: list[str] = []
    for match in re.finditer(r"\[id:([0-9a-fA-F]{8})\]", answer):
        prefix = match.group(1).lower()
        full = valid_prefixes.get(prefix)
        if full and full not in cited:
            cited.append(full)
    return cited
