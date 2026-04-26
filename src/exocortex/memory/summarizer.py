from __future__ import annotations

from typing import Protocol

from exocortex.contracts import (
    Budget,
    Decision,
    Handoff,
    MemoryRecord,
    Task,
    ToolInvocationCursor,
    WorkspaceState,
)


class Summarizer(Protocol):
    """Real summarizers are LLM-backed (CI nightly per CLAUDE-PLAN.MD §6). The
    deterministic TruncatingSummarizer below is the fallback + test fixture.
    """

    def summarize(self, records: list[MemoryRecord], *, max_chars: int) -> str: ...


class TruncatingSummarizer:
    """Deterministic fallback: concat records by type+confidence, truncate.

    Fidelity is terrible, but it exercises the handoff pipeline end-to-end
    without an LLM dependency. The regression suite (CLAUDE-PLAN.MD R8) will
    compare LLM summarizer output against known-good bundles in CI nightly.
    """

    def summarize(self, records: list[MemoryRecord], *, max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        parts = [
            f"[{r.type}/{r.confidence.value}] {r.content}"
            for r in sorted(records, key=lambda r: r.timestamp)
        ]
        joined = "\n".join(parts)
        return joined if len(joined) <= max_chars else joined[:max_chars]


def build_handoff(
    *,
    task: Task,
    from_agent: str,
    to_agent: str,
    sequence_no: int,
    session_records: list[MemoryRecord],
    decisions: list[Decision],
    open_questions: list[str],
    workspace: WorkspaceState | None,
    cursor: ToolInvocationCursor,
    memory_scope_ids: list[str],
    expected_output: str,
    budget_remaining: Budget,
    summarizer: Summarizer,
    digest_char_budget: int = 2000,
) -> tuple[Handoff, str]:
    """Build a Handoff bundle with a summarizer-produced digest.

    Returns (Handoff, digest_string). Caller decides whether to persist the
    digest as a MemoryRecord or attach it to the bundle's `goal_restatement`.
    """
    digest = summarizer.summarize(session_records, max_chars=digest_char_budget)
    goal_restatement = _compose_restatement(task.goal, digest)

    handoff = Handoff(
        task_id=task.id,
        from_agent=from_agent,
        to_agent=to_agent,
        sequence_no=sequence_no,
        goal_restatement=goal_restatement,
        constraints_active=list(task.constraints),
        decisions_so_far=decisions,
        open_questions=open_questions,
        workspace_state=workspace,
        tool_invocation_cursor=cursor,
        memory_scope_ids=memory_scope_ids,
        expected_output=expected_output,
        budget_remaining=budget_remaining,
    )
    return handoff, digest


def _compose_restatement(goal: str, digest: str) -> str:
    if not digest:
        return goal
    return f"{goal}\n\n---\nSession digest:\n{digest}"
