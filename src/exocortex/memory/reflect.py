from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from exocortex.contracts import EventKind
from exocortex.contracts.common import now
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
