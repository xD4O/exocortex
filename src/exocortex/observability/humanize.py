"""One human-readable sentence per event, shared by the CLI and the web (C5).

Before this, the nice per-kind phrasing lived only in the web routes, so
`precog trace` dumped raw ``k=v`` payloads truncated at 80 chars — far noisier
than the web feed, with no phrasing for many kinds. This is the single place
that turns an :class:`Event` into a legible line, so both surfaces agree and
new kinds get phrasing once.
"""

from __future__ import annotations

from typing import Any

from exocortex.contracts import Event, EventKind


def _short(val: object, limit: int = 100) -> str:
    s = str(val)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _arrow(p: dict[str, Any], a: str = "from", b: str = "to") -> str:
    return f"{p.get(a, '?')} → {p.get(b, '?')}"


def _handoff(p: dict[str, Any]) -> str:
    src = p.get("from_agent") or "?"
    dst = p.get("to_agent") or "?"
    goal = _short(p.get("goal_preview") or "", 80)
    fb = " (fallback)" if p.get("fallback_used") else ""
    return f"{src} → {dst}{fb}" + (f" · {goal}" if goal else "")


_FORMATTERS: dict[EventKind, Any] = {
    EventKind.TASK_CREATED: lambda p: _short(p.get("goal") or "task created"),
    EventKind.TASK_STATUS_CHANGED: _arrow,
    EventKind.TASK_COMPLETED: lambda p: "completed"
    + (f": {_short(p.get('goal'))}" if p.get("goal") else ""),
    EventKind.TASK_FAILED: lambda p: "failed"
    + (f": {_short(p.get('error') or p.get('goal'))}" if (p.get("error") or p.get("goal")) else ""),
    EventKind.SESSION_OPENED: lambda p: f"opened via {p.get('via', '?')}"
    + (f" · {p.get('unfinished_count')} unfinished" if p.get("unfinished_count") else ""),
    EventKind.SESSION_STATUS_CHANGED: _arrow,
    EventKind.SESSION_CLOSED: lambda p: "session closed",
    EventKind.MEMORY_WRITTEN: lambda p: f"[{p.get('type', 'record')}] "
    + _short(p.get("content_preview") or p.get("content") or p.get("record_id") or "")
    + (" (durable)" if p.get("durable") else ""),
    EventKind.MEMORY_READ: lambda p: f"read {p.get('op', 'search')}"
    + (f" {p.get('query')!r}" if p.get("query") else "")
    + (f" → {p.get('result_count')} hits" if p.get("result_count") is not None else ""),
    EventKind.MEMORY_FORGOTTEN: lambda p: "forgot "
    + _short(p.get("content_preview") or p.get("record_id") or ""),
    EventKind.MEMORY_MERGED: lambda p: f"merged into {str(p.get('keep_id', '?'))[:8]} "
    f"({p.get('removed_count', 0)} dropped)",
    EventKind.MEMORY_PROMOTED: lambda p: f"{p.get('from', '?')} → {p.get('to', '?')} "
    f"({p.get('cluster_size', 0)} agree)",
    EventKind.MEMORY_CHAT: lambda p: _short(p.get("question") or "chat"),
    EventKind.TOOL_PROPOSED: lambda p: f"proposed {p.get('tool', '?')}",
    EventKind.TOOL_POLICY_CHECKED: lambda p: f"policy: {p.get('decision', '?')}",
    EventKind.TOOL_APPROVED: lambda p: f"approved {p.get('tool', '')}".strip(),
    EventKind.TOOL_REJECTED: lambda p: "rejected"
    + (f": {_short(p.get('reason'), 80)}" if p.get("reason") else ""),
    EventKind.TOOL_EXECUTED: lambda p: f"executed ({p.get('state', 'done')})",
    EventKind.APPROVAL_REQUESTED: lambda p: "approval requested"
    + (f": {_short(p.get('reason'), 80)}" if p.get("reason") else ""),
    EventKind.APPROVAL_RESOLVED: lambda p: f"approval {p.get('resolution', '?')}",
    EventKind.HANDOFF_INITIATED: _handoff,
    EventKind.HANDOFF_ACCEPTED: _handoff,
    EventKind.DISPATCH_FALLBACK: lambda p: f"{p.get('requested', '?')} → "
    f"{p.get('fallback', '?')} (auto-fallback)",
    EventKind.DISPATCH_FAILED: lambda p: f"{p.get('preferred_agent') or 'auto'} · "
    f"{p.get('reason', 'unknown')}",
    EventKind.PROFILE_OBSERVED: lambda p: f"observed {p.get('dimension', '?')}: "
    + _short(p.get("value") or "", 60),
    EventKind.PROFILE_QUESTIONED: lambda p: f"asked about {p.get('dimension', '?')}",
    EventKind.PROFILE_ANSWERED: lambda p: f"answered {p.get('dimension', '?')}",
    EventKind.PROFILE_REDACTED: lambda p: f"redacted {p.get('dimension', 'profile')}",
    EventKind.PROFILE_FROZEN_TOGGLED: lambda p: f"profile freeze = {p.get('frozen', '?')}",
    EventKind.CONVERSATION_OPENED: lambda p: f"opened: {_short(p.get('topic') or '', 80)}",
    EventKind.CONVERSATION_TURN: lambda p: f"[{p.get('from_agent', '?')} → "
    f"{p.get('to_agent', '?')}] {_short(p.get('content') or '', 80)}",
    EventKind.CONVERSATION_CLOSED: lambda p: "conversation closed",
    EventKind.CONVERSATION_DELETED: lambda p: "conversation deleted",
}


def humanize_event(event: Event) -> str:
    """A single, legible sentence describing what an event means."""
    # A producer-supplied reason (C1) is the most human phrasing available.
    if event.reason:
        return event.reason
    p = event.payload or {}
    fn = _FORMATTERS.get(event.kind)
    if fn is not None:
        try:
            return str(fn(p))
        except Exception:  # pragma: no cover - never let a preview crash a view
            pass
    # Fallback: a compact view of the first few payload keys.
    if p:
        return " ".join(f"{k}={_short(v, 40)}" for k, v in list(p.items())[:4])
    return event.kind.value
