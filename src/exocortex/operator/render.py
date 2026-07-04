from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from exocortex.contracts import Event, EventKind, MemoryRecord
from exocortex.observability.humanize import humanize_event

_KIND_STYLE: dict[EventKind, str] = {
    EventKind.TASK_CREATED: "bold cyan",
    EventKind.TASK_STATUS_CHANGED: "cyan",
    EventKind.TASK_COMPLETED: "bold green",
    EventKind.TASK_FAILED: "bold red",
    EventKind.SESSION_OPENED: "bold magenta",
    EventKind.SESSION_STATUS_CHANGED: "magenta",
    EventKind.SESSION_CLOSED: "magenta",
    EventKind.MEMORY_WRITTEN: "blue",
    EventKind.TOOL_PROPOSED: "yellow",
    EventKind.TOOL_POLICY_CHECKED: "yellow",
    EventKind.TOOL_APPROVED: "green",
    EventKind.TOOL_REJECTED: "bold red",
    EventKind.TOOL_EXECUTED: "green",
    EventKind.APPROVAL_REQUESTED: "bold yellow",
    EventKind.APPROVAL_RESOLVED: "yellow",
    EventKind.HANDOFF_INITIATED: "bold magenta",
    EventKind.HANDOFF_ACCEPTED: "magenta",
}

_CONFIDENCE_GLYPH: dict[str, str] = {
    "observed": "◉",
    "inferred": "◑",
    "asserted": "◔",
    "external_claim": "◌",
}


def render_event_line(ev: Event) -> Text:
    style = _KIND_STYLE.get(ev.kind, "white")
    task_id = str(ev.task_id)[:8] if ev.task_id else "-"
    # Prefer the true actor over the emitter (agent_id may be the platform).
    agent = ev.actor or ev.agent_id or "-"
    line = Text()
    # Full date + time so a trace spanning midnight stays readable (C5).
    line.append(ev.timestamp.strftime("%Y-%m-%d %H:%M:%S"), style="dim")
    line.append("  ")
    line.append(f"{ev.kind:<24}", style=style)
    line.append(f" task={task_id}  {agent:<12} ", style="dim")
    # One human-readable sentence instead of a raw payload dump.
    line.append(humanize_event(ev), style="white")
    return line


def render_timeline(events: list[Event], console: Console) -> None:
    if not events:
        console.print("[dim]No events.[/dim]")
        return
    for ev in events:
        console.print(render_event_line(ev))


def render_memory_record(
    r: MemoryRecord, *, expanded: bool = False
) -> Text:
    glyph = _CONFIDENCE_GLYPH.get(r.confidence.value, "?")
    line = Text()
    line.append(f"{glyph} ", style="bold")
    line.append(f"[{r.type}] ", style="yellow")
    line.append(str(r.id)[:8], style="dim")
    line.append("  ")
    line.append(f"scope={r.scope.value}:{_short(r.scope_id, 16)}", style="cyan")
    line.append(f"  src={r.source}", style="magenta")
    line.append(f"  {r.timestamp.strftime('%Y-%m-%d %H:%M')}", style="dim")
    line.append("\n  ")
    content = r.content if expanded else _short(r.content, 120)
    line.append(content, style="white")
    return line


def render_memory_table(records: list[MemoryRecord]) -> Table:
    table = Table(title=f"Memory ({len(records)} records)")
    table.add_column("ID", style="bold")
    table.add_column("Type")
    table.add_column("Scope")
    table.add_column("Source")
    table.add_column("Confidence")
    table.add_column("Content")
    for r in records:
        table.add_row(
            str(r.id)[:8],
            r.type,
            f"{r.scope.value}:{_short(r.scope_id, 12)}",
            r.source,
            r.confidence.value,
            _short(r.content, 60),
        )
    return table


def _short(val: object, limit: int = 80) -> str:
    s = str(val)
    return s if len(s) <= limit else s[: limit - 1] + "…"
