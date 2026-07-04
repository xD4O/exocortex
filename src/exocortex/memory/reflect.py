from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from exocortex.contracts import Event, EventKind
from exocortex.contracts.common import new_id, now
from exocortex.observability.audit import AuditLog


@dataclass
class ReflectionService:
    audit: AuditLog

    async def list_insights(self, *, include_resolved: bool = False) -> list[dict[str, Any]]:
        events = await self.audit.read_all()
        proposed: dict[str, dict[str, Any]] = {}
        status: dict[str, str] = {}
        for ev in events:
            iid = (ev.payload or {}).get("insight_id")
            if not iid:
                continue
            if ev.kind == EventKind.INSIGHT_PROPOSED:
                proposed[iid] = dict(ev.payload)
                status.setdefault(iid, "proposed")
            elif ev.kind == EventKind.INSIGHT_ACCEPTED:
                status[iid] = "accepted"
            elif ev.kind == EventKind.INSIGHT_DISMISSED:
                status[iid] = "dismissed"
        out: list[dict[str, Any]] = []
        for iid, payload in proposed.items():
            st = status.get(iid, "proposed")
            if not include_resolved and st != "proposed":
                continue
            out.append({**payload, "status": st})
        out.reverse()  # newest first (audit is chronological)
        return out

    async def window_from(self, *, max_days: int, override_days: int | None = None,
                          all_history: bool = False) -> datetime | None:
        current = now()
        if all_history:
            return None
        cap = current - timedelta(days=override_days if override_days is not None else max_days)
        if override_days is not None:
            return cap
        last = None
        for ev in await self.audit.read_all():
            if ev.kind == EventKind.REFLECTION_COMPLETED:
                last = ev.timestamp
        if last is None:
            return cap
        return max(last, cap)  # never reflect further back than the cap

    async def start_run(self, *, agent: str, window_from: datetime | None) -> str:
        rid = str(new_id())
        await self.audit.record(Event(
            kind=EventKind.REFLECTION_STARTED, actor="reflect", reason="reflection run",
            payload={"reflection_id": rid, "agent": agent,
                     "window_from": window_from.isoformat() if window_from else None}))
        return rid

    async def complete_run(self, reflection_id: str, *, status: str, count: int,
                           error: str | None = None) -> None:
        await self.audit.record(Event(
            kind=EventKind.REFLECTION_COMPLETED, actor="reflect",
            reason=f"reflection {status} ({count} insights)",
            payload={"reflection_id": reflection_id, "status": status,
                     "insight_count": count, "error": error}))

    async def count_for_run(self, reflection_id: str) -> int:
        # Count only THIS run's insights — not every insight ever proposed.
        items = await self.list_insights(include_resolved=True)
        return sum(1 for i in items if i.get("reflection_id") == reflection_id)


async def run_reflection(*, audit: AuditLog, store: Any, settings: Any, dispatch: Any,
                         since_days: int | None = None,
                         all_history: bool = False) -> dict[str, Any]:
    """Run one reflection pass. `dispatch` is a callable with the same kwargs
    as DispatchService.dispatch (goal, preferred_agent, from_agent,
    max_wait_seconds) — injected so this is unit-testable without a real agent.
    Records REFLECTION_STARTED/COMPLETED; the dispatched agent proposes insights
    via the insight_propose tool during the run."""
    from exocortex.coordination.reflect_goal import build_reflect_goal  # noqa: PLC0415

    svc = ReflectionService(audit=audit)
    lo = await svc.window_from(max_days=settings.reflect_window_days,
                               override_days=since_days, all_history=all_history)
    pairs = await store.all_with_embeddings()
    records = [r for r, _ in pairs if lo is None or r.timestamp >= lo]
    agent = settings.reflect_agent or "codex"
    rid = await svc.start_run(agent=agent, window_from=lo)
    goal = build_reflect_goal(rid, records, settings.reflect_max_insights)
    try:
        result = await dispatch(goal=goal,
                                preferred_agent=settings.reflect_agent or None,
                                from_agent="reflect", max_wait_seconds=600)
        count = await svc.count_for_run(rid)          # only this run's insights
        await svc.complete_run(rid, status="completed", count=count)
        return {"status": "completed", "reflection_id": rid,
                "insight_count": count,
                "dispatched_to": (result or {}).get("dispatched_to")}
    except Exception as e:  # noqa: BLE001 — record failure, keep proposed insights
        await svc.complete_run(rid, status="failed", count=0, error=str(e))
        return {"status": "failed", "reflection_id": rid, "error": str(e)}
