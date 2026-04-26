"""Auto-recall: "here's what we were doing" for a fresh agent session.

RecallService reconstructs lightweight task state from the audit log and
pulls recent high-signal memory from the durable store, then packages it
into a RecallSummary suitable for (a) display to a human operator via the
CLI or (b) consumption by an agent on its first turn via the MCP
`session_startup` tool.

Design goals:
- Works whether or not the operator uses the Coordinator — recent memory
  records alone are enough to produce something useful.
- Runs in well under 1s for realistic audit-log sizes (thousands of events).
- Stable JSON shape so hooks and tools can format it themselves.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from exocortex.contracts import Event, EventKind, MemoryRecord, MemoryScope
from exocortex.contracts.common import now
from exocortex.memory.durable import DurableMemoryStore
from exocortex.observability.audit import AuditLog

TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "canceled"})


@dataclass
class UnfinishedTask:
    task_id: str
    goal: str
    status: str
    last_activity: str  # ISO timestamp
    last_activity_age_seconds: int
    agents: list[str]
    event_count: int


@dataclass
class RecallSummary:
    generated_at: str
    agent_id: str | None
    unfinished_tasks: list[UnfinishedTask]
    recent_decisions: list[dict[str, Any]]
    recent_project_memory: list[dict[str, Any]]
    last_agent_activity: dict[str, str]  # agent_id -> ISO timestamp
    total_memory_records: int
    total_events: int
    suggested_prompts: list[str]
    text_for_user: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["unfinished_tasks"] = [asdict(t) for t in self.unfinished_tasks]
        return d


class RecallService:
    def __init__(
        self, *, store: DurableMemoryStore, audit: AuditLog
    ) -> None:
        self._store = store
        self._audit = audit

    async def summarize(
        self,
        *,
        agent_id: str | None = None,
        unfinished_window_days: int = 14,
        decision_window_days: int = 14,
        max_unfinished: int = 8,
        max_decisions: int = 12,
        max_recent_memory: int = 8,
    ) -> RecallSummary:
        current = now()
        events = await self._audit.read_all()
        tasks_by_id = _reconstruct_tasks(events)
        last_agent_activity = _agent_last_seen(events)
        total_events = len(events)

        # Unfinished = non-terminal status AND touched within the window.
        window_cutoff = current - timedelta(days=unfinished_window_days)
        unfinished: list[UnfinishedTask] = []
        for t in tasks_by_id.values():
            if t["status"] in TERMINAL_STATUSES:
                continue
            if t["last_activity"] < window_cutoff:
                continue
            age = int((current - t["last_activity"]).total_seconds())
            unfinished.append(
                UnfinishedTask(
                    task_id=t["id"],
                    goal=t["goal"],
                    status=t["status"],
                    last_activity=t["last_activity"].isoformat(),
                    last_activity_age_seconds=age,
                    agents=sorted(t["agents"]),
                    event_count=t["event_count"],
                )
            )
        unfinished.sort(key=lambda u: u.last_activity_age_seconds)
        unfinished = unfinished[:max_unfinished]

        # Recent project- and global-scope records (highest-signal cross-
        # session state). We scan ALL scope_ids, not just 'exocortex' /
        # 'global' — agents write under different scope_ids depending on
        # how they were prompted, and recall should surface all of them.
        decision_cutoff = current - timedelta(days=decision_window_days)
        project_pairs = await self._store.all_with_embeddings(
            scope=MemoryScope.PROJECT
        )
        global_pairs = await self._store.all_with_embeddings(
            scope=MemoryScope.GLOBAL
        )
        candidates = [r for r, _ in project_pairs] + [r for r, _ in global_pairs]
        decisions = [
            r for r in candidates
            if r.type == "decision" and r.timestamp >= decision_cutoff
        ]
        decisions.sort(key=lambda r: r.timestamp, reverse=True)
        decisions = decisions[:max_decisions]

        recent_any = sorted(candidates, key=lambda r: r.timestamp, reverse=True)
        recent_any = recent_any[:max_recent_memory]

        # Total memory count across all scopes (counts embedded records).
        all_embedded = await self._store.all_with_embeddings()
        total_memory = len(all_embedded)

        suggested = _build_suggested_prompts(unfinished, recent_any)
        text = _build_text_for_user(agent_id, unfinished, decisions, recent_any)

        return RecallSummary(
            generated_at=current.isoformat(),
            agent_id=agent_id,
            unfinished_tasks=unfinished,
            recent_decisions=[_record_to_dict(r) for r in decisions],
            recent_project_memory=[_record_to_dict(r) for r in recent_any],
            last_agent_activity={k: v.isoformat() for k, v in last_agent_activity.items()},
            total_memory_records=total_memory,
            total_events=total_events,
            suggested_prompts=suggested,
            text_for_user=text,
        )


def _reconstruct_tasks(events: list[Event]) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.task_id is None:
            continue
        tid = str(ev.task_id)
        if ev.kind == EventKind.TASK_CREATED:
            tasks[tid] = {
                "id": tid,
                "goal": str(ev.payload.get("goal", "")),
                "status": "proposed",
                "first_seen": ev.timestamp,
                "last_activity": ev.timestamp,
                "agents": set(),
                "event_count": 0,
            }
        if tid not in tasks:
            # Orphan event (e.g. audit log rotated past task creation). Skip.
            continue
        tasks[tid]["last_activity"] = ev.timestamp
        tasks[tid]["event_count"] += 1
        if ev.agent_id:
            agents: set[str] = tasks[tid]["agents"]
            agents.add(ev.agent_id)
        if ev.kind == EventKind.TASK_STATUS_CHANGED:
            to = ev.payload.get("to")
            if isinstance(to, str):
                tasks[tid]["status"] = to
        elif ev.kind == EventKind.TASK_COMPLETED:
            tasks[tid]["status"] = "completed"
        elif ev.kind == EventKind.TASK_FAILED:
            tasks[tid]["status"] = "failed"
    return tasks


def _agent_last_seen(events: list[Event]) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for ev in events:
        if not ev.agent_id:
            continue
        prev = out.get(ev.agent_id)
        if prev is None or ev.timestamp > prev:
            out[ev.agent_id] = ev.timestamp
    return out


def _build_suggested_prompts(
    unfinished: list[UnfinishedTask],
    recent_any: list[MemoryRecord],
) -> list[str]:
    prompts: list[str] = []
    for t in unfinished[:3]:
        goal = t.goal if len(t.goal) <= 80 else t.goal[:77] + "…"
        prompts.append(f"Continue: {goal}")
    if not unfinished and recent_any:
        topic = recent_any[0].content
        snippet = topic if len(topic) <= 80 else topic[:77] + "…"
        prompts.append(f"Continue the topic about: {snippet}")
    prompts.append("Start a new task")
    return prompts


def _build_text_for_user(
    agent_id: str | None,
    unfinished: list[UnfinishedTask],
    decisions: list[MemoryRecord],
    recent_any: list[MemoryRecord],
) -> str:
    who = agent_id or "this session"
    if not unfinished and not recent_any:
        return (
            f"Exocortex has no prior memory for {who}. "
            "We're starting fresh — tell me what you'd like to work on."
        )
    lines: list[str] = []
    if unfinished:
        lines.append(
            f"Picking up where we left off — you have {len(unfinished)} unfinished task"
            f"{'s' if len(unfinished) != 1 else ''}:"
        )
        for i, t in enumerate(unfinished[:4], 1):
            agents_s = ", ".join(t.agents) if t.agents else "—"
            age = _human_age(t.last_activity_age_seconds)
            goal = t.goal if len(t.goal) <= 90 else t.goal[:87] + "…"
            lines.append(
                f"  {i}. **{goal}** ({t.status}, last touched {age} ago by {agents_s})"
            )
    if decisions:
        lines.append("")
        lines.append(f"Recent durable decisions ({len(decisions)}):")
        for d in decisions[:4]:
            c = d.content if len(d.content) <= 90 else d.content[:87] + "…"
            lines.append(f"  — {c} _(by {d.source})_")
    if unfinished:
        lines.append("")
        lines.append(
            "Continue one of these, or start something new — let me know which."
        )
    else:
        lines.append("")
        lines.append("What would you like to work on?")
    return "\n".join(lines)


def _human_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    days = seconds // 86400
    return f"{days}d"


def _record_to_dict(r: MemoryRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "type": r.type,
        "content": r.content,
        "source": r.source,
        "confidence": r.confidence.value,
        "scope": r.scope.value,
        "scope_id": r.scope_id,
        "timestamp": r.timestamp.isoformat(),
    }


# dataclass field shim to silence linter when RecallSummary has list defaults.
_: Any = field
