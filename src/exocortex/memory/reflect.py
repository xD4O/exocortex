from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from exocortex.contracts import EventKind
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
