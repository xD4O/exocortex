from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from exocortex.contracts import Confidence, Event, EventKind, MemoryRecord, MemoryScope
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

    async def _find_proposed(self, insight_id: str) -> dict[str, Any] | None:
        for ev in await self.audit.read_all():
            if ev.kind == EventKind.INSIGHT_PROPOSED and \
               (ev.payload or {}).get("insight_id") == insight_id:
                return dict(ev.payload)
        return None

    async def dismiss(self, insight_id: str, *, note: str = "") -> dict[str, Any]:
        await self.audit.record(Event(kind=EventKind.INSIGHT_DISMISSED, actor="operator",
                                      payload={"insight_id": insight_id, "note": note}))
        return {"insight_id": insight_id, "status": "dismissed"}

    async def accept(self, insight_id: str, *, apply: bool = False,
                     store: Any = None, embedder: Any = None) -> dict[str, Any]:
        payload = await self._find_proposed(insight_id)
        if payload is None:
            raise ValueError(f"unknown insight {insight_id}")
        action = payload.get("suggested_action") or {"type": "none"}
        acted = None
        if apply:
            acted = await self._apply_action(payload, action, store, embedder)
        await self.audit.record(Event(kind=EventKind.INSIGHT_ACCEPTED, actor="operator",
                                      payload={"insight_id": insight_id, "acted": acted}))
        return {"insight_id": insight_id, "status": "accepted",
                "applied": apply, "proposed_action": action, "acted": acted}

    async def _apply_action(self, payload: dict[str, Any], action: dict[str, Any],
                            store: Any, embedder: Any) -> dict[str, Any]:
        atype = action.get("type", "none")
        if atype == "supersede" and store is not None and embedder is not None:
            stale_id = action.get("stale_record_id")
            title = payload.get("title", "")
            detail = payload.get("detail", "")
            rec = MemoryRecord(
                type="correction",
                content=f"Supersedes {stale_id}: {title} — {detail}",
                source="reflect", confidence=Confidence.INFERRED,
                scope=MemoryScope.PROJECT, scope_id="exocortex",
                tags=["supersedes:" + str(stale_id)])
            await store.write(rec, embedding=embedder.embed(rec.content))
            return {"superseded_by": str(rec.id), "stale_record_id": stale_id}
        # create_rule / track_gap / record_decision: v1 returns the drafted payload
        # for the caller (CLI/web) to persist on explicit confirm.
        return {"drafted": action}


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
    except Exception as e:  # noqa: BLE001 — dispatch crashed; keep proposed insights
        count = await svc.count_for_run(rid)
        await svc.complete_run(rid, status="failed", count=count, error=str(e))
        return {"status": "failed", "reflection_id": rid,
                "insight_count": count, "error": str(e)}

    result = result or {}
    count = await svc.count_for_run(rid)          # only this run's insights
    dispatch_status = result.get("status")
    if dispatch_status in ("failed", "timeout", "cancelled"):
        await svc.complete_run(rid, status="failed", count=count,
                               error=result.get("error") or f"dispatch {dispatch_status}")
        return {"status": "failed", "reflection_id": rid, "insight_count": count,
                "dispatched_to": result.get("dispatched_to"),
                "dispatch_status": dispatch_status}
    await svc.complete_run(rid, status="completed", count=count)
    return {"status": "completed", "reflection_id": rid, "insight_count": count,
            "dispatched_to": result.get("dispatched_to")}
