from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from exocortex.config import Settings
from exocortex.contracts import Event, EventKind, MemoryScope
from exocortex.core.events import EventBus
from exocortex.core.task_manager import TaskManager
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.reflect import ReflectionService, run_reflection
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.observability.audit import AuditLog
from exocortex.observability.humanize import humanize_event
from exocortex.observability.logging import configure_logging
from exocortex.operator.render import (
    render_event_line,
    render_memory_record,
    render_memory_table,
    render_timeline,
)
from exocortex.policy.engine import PolicyEngine
from exocortex.tools.builtin import register_builtins
from exocortex.tools.registry import ToolRegistry

app = typer.Typer(
    help="Exocortex operator CLI. Read-only views run against the audit log "
    "and memory DB. Live approval + TUI land with the daemon in a later phase.",
    no_args_is_help=True,
)
memory_app = typer.Typer(help="Browse and search the memory store.")
app.add_typer(memory_app, name="memory")
profile_app = typer.Typer(help="USER-scope memory: facts about the operator.")
app.add_typer(profile_app, name="profile")
insights_app = typer.Typer(
    help="Review and act on reflection-proposed insights.",
    invoke_without_command=True,
)
app.add_typer(insights_app, name="insights")

console = Console()


def _setup() -> tuple[Settings, AuditLog]:
    settings = Settings()
    settings.ensure_dirs()
    configure_logging(settings)
    return settings, AuditLog(settings.audit_log_path)


# --- Task lifecycle ---------------------------------------------------------


@app.command()
def submit(
    goal: Annotated[str, typer.Argument(help="One-line goal for the task.")],
) -> None:
    """Create a task and record it to the audit log."""

    async def _run() -> None:
        settings, audit = _setup()
        bus = EventBus(PolicyEngine())
        bus.set_audit_sink(audit.record)
        mgr = TaskManager(bus)
        task = await mgr.create(goal=goal)
        console.print(f"[green]Created task[/green] [bold]{task.id}[/bold] — {goal}")
        console.print(f"Audit log: {settings.audit_log_path}")

    asyncio.run(_run())


@app.command("ls")
def ls() -> None:
    """List tasks reconstructed from the audit log."""

    async def _run() -> None:
        _, audit = _setup()
        events = await audit.read_all()
        statuses: dict[str, str] = {}
        goals: dict[str, str] = {}
        for ev in events:
            if ev.task_id is None:
                continue
            tid = str(ev.task_id)
            if ev.kind == EventKind.TASK_CREATED:
                statuses.setdefault(tid, "proposed")
                goals[tid] = str(ev.payload.get("goal", ""))
            elif ev.kind == EventKind.TASK_STATUS_CHANGED:
                to = ev.payload.get("to")
                if isinstance(to, str):
                    statuses[tid] = to

        if not statuses:
            console.print("[dim]No tasks recorded.[/dim]")
            return

        table = Table(title="Tasks")
        table.add_column("ID", style="bold")
        table.add_column("Status")
        table.add_column("Goal")
        for tid, status in statuses.items():
            table.add_row(tid[:8], status, goals.get(tid, ""))
        console.print(table)

    asyncio.run(_run())


@app.command()
def ps() -> None:
    """Alias for `ls` — matches CLAUDE-PLAN.MD §5.7 Phase 1 UI slice."""
    ls()


@app.command()
def tail(
    task: Annotated[
        str | None, typer.Option(help="Filter by task UUID prefix.")
    ] = None,
    kind: Annotated[
        str | None, typer.Option(help="Filter by event kind (substring match).")
    ] = None,
    agent: Annotated[
        str | None, typer.Option(help="Filter by agent id.")
    ] = None,
) -> None:
    """Print events from the audit log with optional filters."""

    async def _run() -> None:
        _, audit = _setup()
        events = await audit.read_all()
        for ev in events:
            if task and (ev.task_id is None or not str(ev.task_id).startswith(task)):
                continue
            if kind and kind not in ev.kind:
                continue
            if agent and ev.agent_id != agent:
                continue
            console.print(render_event_line(ev))

    asyncio.run(_run())


# --- Trace (CLAUDE-PLAN.MD §5.4) -------------------------------------------


@app.command()
def trace(
    task_id: Annotated[
        str, typer.Argument(help="Task UUID (full or prefix).")
    ],
) -> None:
    """Reconstruct a timeline for a single task from the audit log."""

    async def _run() -> None:
        _, audit = _setup()
        events = await audit.read_all()
        matches = [
            e
            for e in events
            if e.task_id is not None and str(e.task_id).startswith(task_id)
        ]
        if not matches:
            console.print(f"[yellow]No events found for task {task_id}[/yellow]")
            raise typer.Exit(code=1)

        full_id = str(matches[0].task_id)
        agents = sorted({e.agent_id for e in matches if e.agent_id})
        console.print(f"[bold]Trace for task[/bold] [cyan]{full_id}[/cyan]")
        console.print(
            f"  [dim]{len(matches)} events · agents: "
            f"{', '.join(agents) if agents else '(none)'}[/dim]\n"
        )
        render_timeline(matches, console)

    asyncio.run(_run())


# --- Memory -----------------------------------------------------------------


@memory_app.command("list")
def memory_list(
    scope: Annotated[
        str | None,
        typer.Option(help="Scope filter: session | task | project | global"),
    ] = None,
    scope_id: Annotated[
        str | None, typer.Option(help="Scope-id filter (required with --scope).")
    ] = None,
    limit: Annotated[int, typer.Option(help="Max records to display.")] = 20,
) -> None:
    """List recent memory records, optionally filtered by scope."""

    async def _run() -> None:
        settings, _ = _setup()
        store = DurableMemoryStore(settings.memory_db_path)

        if scope is not None:
            try:
                scope_enum = MemoryScope(scope)
            except ValueError as e:
                raise typer.BadParameter(
                    f"invalid scope {scope!r}; use session|task|project|global"
                ) from e
            if scope_id is None:
                raise typer.BadParameter("--scope-id is required when --scope is given")
            records = await store.list_by_scope(scope_enum, scope_id)
        else:
            # Fall back to listing all embedded records (bounded by limit).
            pairs = await store.all_with_embeddings()
            records = [r for r, _ in pairs]

        if limit and len(records) > limit:
            records = records[-limit:]

        if not records:
            console.print("[dim]No records.[/dim]")
            return
        console.print(render_memory_table(records))

    asyncio.run(_run())


@memory_app.command("search")
def memory_search(
    query: Annotated[str, typer.Argument(help="Free-text query.")],
    scope: Annotated[
        str | None,
        typer.Option(help="Scope filter: session | task | project | global"),
    ] = None,
    scope_id: Annotated[str | None, typer.Option(help="Scope-id filter.")] = None,
    limit: Annotated[int, typer.Option(help="Max results.")] = 10,
    alpha: Annotated[
        float,
        typer.Option(
            help="Weight: 1.0=keyword only, 0.0=semantic only, 0.5=hybrid."
        ),
    ] = 0.5,
) -> None:
    """Hybrid (keyword + semantic) search against the durable memory store."""

    async def _run() -> None:
        settings, _ = _setup()
        store = DurableMemoryStore(settings.memory_db_path)
        embedder = DeterministicEmbeddingProvider()
        retrieval = HybridRetrieval(store, embedder)

        scope_enum = None
        if scope is not None:
            try:
                scope_enum = MemoryScope(scope)
            except ValueError as e:
                raise typer.BadParameter(f"invalid scope {scope!r}") from e

        hits = await retrieval.search(
            query,
            scope=scope_enum,
            scope_id=scope_id,
            limit=limit,
            alpha=alpha,
        )
        if not hits:
            console.print("[dim]No matches.[/dim]")
            return

        console.print(f"[bold]{len(hits)} results[/bold] for {query!r} (alpha={alpha})\n")
        for record, score in hits:
            console.print(f"[green]score={score:.3f}[/green]")
            console.print(render_memory_record(record))
            console.print("")

    asyncio.run(_run())


@memory_app.command("show")
def memory_show(
    record_id: Annotated[str, typer.Argument(help="Record UUID (full).")],
) -> None:
    """Show a single memory record with full content."""

    async def _run() -> None:
        settings, _ = _setup()
        store = DurableMemoryStore(settings.memory_db_path)
        try:
            rid = UUID(record_id)
        except ValueError as e:
            raise typer.BadParameter(f"invalid UUID: {record_id}") from e
        record = await store.get(rid)
        if record is None:
            console.print(f"[yellow]No record with id {record_id}[/yellow]")
            raise typer.Exit(code=1)
        console.print(render_memory_record(record, expanded=True))

    asyncio.run(_run())


@memory_app.command("forget")
def memory_forget(
    record_id: Annotated[str, typer.Argument(help="Full UUID of the record to delete.")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
) -> None:
    """Hard-delete one memory record. Audit-logged."""

    async def _run() -> None:
        from exocortex.operator.mcp.handlers import MemoryHandlers  # noqa: PLC0415

        settings, audit = _setup()
        store = DurableMemoryStore(settings.memory_db_path)
        try:
            rid = UUID(record_id)
        except ValueError as e:
            raise typer.BadParameter(f"invalid UUID: {record_id}") from e
        record = await store.get(rid)
        if record is None:
            console.print(f"[yellow]No record with id {record_id}[/yellow]")
            raise typer.Exit(code=1)
        console.print(render_memory_record(record, expanded=True))
        if not yes and not typer.confirm("\nForget this record?", default=False):
            console.print("[dim]aborted[/dim]")
            raise typer.Exit(code=1)
        embedder = DeterministicEmbeddingProvider()
        retrieval = HybridRetrieval(store, embedder)
        handlers = MemoryHandlers(
            store=store, embedder=embedder, retrieval=retrieval, audit=audit
        )
        result = await handlers.memory_forget(record_id=record_id)
        console.print(f"[green]✓[/green] {result['status']}: {record_id}")

    asyncio.run(_run())


@memory_app.command("dedup")
def memory_dedup(
    scope: Annotated[
        str | None, typer.Option(help="Restrict to a scope (session|task|project|global)."),
    ] = None,
    scope_id: Annotated[str | None, typer.Option(help="Scope id within scope.")] = None,
    threshold: Annotated[
        float,
        typer.Option(help="Cosine threshold for near-duplicates. Default 0.92."),
    ] = 0.92,
    merge: Annotated[
        bool,
        typer.Option(
            "--merge",
            help="Auto-merge each cluster: keep canonical, drop the rest.",
        ),
    ] = False,
) -> None:
    """Find (and optionally merge) near-duplicate memory records."""

    async def _run() -> None:
        from exocortex.memory.dedup import find_dedup_clusters, merge_records  # noqa: PLC0415

        settings, _ = _setup()
        store = DurableMemoryStore(settings.memory_db_path)
        scope_enum = None
        if scope is not None:
            try:
                scope_enum = MemoryScope(scope)
            except ValueError as e:
                raise typer.BadParameter(f"invalid scope: {scope}") from e
        clusters = await find_dedup_clusters(
            store, scope=scope_enum, scope_id=scope_id, threshold=threshold
        )
        if not clusters:
            console.print("[dim]No near-duplicate clusters found.[/dim]")
            return
        for i, c in enumerate(clusters, 1):
            console.print(f"\n[bold cyan]Cluster {i}[/bold cyan] ({c.size} records)")
            console.print("  [green]canonical:[/green]")
            console.print(render_memory_record(c.canonical))
            for d in c.duplicates:
                console.print("  [yellow]duplicate:[/yellow]")
                console.print(render_memory_record(d))

        if not merge:
            console.print(
                f"\n[dim]{len(clusters)} cluster(s). Re-run with --merge to "
                f"keep the canonical and drop duplicates.[/dim]"
            )
            return

        total_removed = 0
        for c in clusters:
            removed = await merge_records(
                store,
                keep_id=str(c.canonical.id),
                drop_ids=[str(d.id) for d in c.duplicates],
            )
            total_removed += removed
        console.print(
            f"\n[green]✓[/green] removed {total_removed} duplicate record(s) "
            f"across {len(clusters)} cluster(s)."
        )

    asyncio.run(_run())


@memory_app.command("promote")
def memory_promote(
    scope: Annotated[str | None, typer.Option(help="Restrict to a scope.")] = None,
    scope_id: Annotated[str | None, typer.Option(help="Scope id within scope.")] = None,
    threshold: Annotated[
        float, typer.Option(help="Cosine threshold for clustering.")
    ] = 0.92,
    min_agents: Annotated[
        int,
        typer.Option(help="Min distinct sources needed to promote a cluster."),
    ] = 3,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Actually promote (default is dry-run / report only).",
        ),
    ] = False,
) -> None:
    """Promote confidence on records confirmed by ≥N distinct agents."""

    async def _run() -> None:
        from exocortex.memory.promotion import (  # noqa: PLC0415
            apply_promotions,
            find_promotion_candidates,
        )

        settings, audit = _setup()
        store = DurableMemoryStore(settings.memory_db_path)
        scope_enum = None
        if scope is not None:
            try:
                scope_enum = MemoryScope(scope)
            except ValueError as e:
                raise typer.BadParameter(f"invalid scope: {scope}") from e
        candidates = await find_promotion_candidates(
            store,
            scope=scope_enum,
            scope_id=scope_id,
            threshold=threshold,
            min_agents=min_agents,
        )
        if not candidates:
            console.print("[dim]No records meet the promotion threshold.[/dim]")
            return
        for p in candidates:
            console.print(
                f"  [yellow]{p.record_id[:8]}[/yellow] "
                f"{p.from_confidence.value} → [green]{p.to_confidence.value}[/green] "
                f"(cluster {p.cluster_size}, sources: {', '.join(p.supporting_sources)})"
            )
        if not apply:
            console.print(
                f"\n[dim]{len(candidates)} candidate(s). Re-run with --apply "
                f"to promote.[/dim]"
            )
            return
        applied = await apply_promotions(store, audit, candidates)
        console.print(f"\n[green]✓[/green] promoted {applied} record(s).")

    asyncio.run(_run())


# --- Reflect / Insights -----------------------------------------------------


def _render_insight_line(item: dict[str, Any]) -> Text:
    iid = str(item.get("insight_id", ""))
    ev = Event(kind=EventKind.INSIGHT_PROPOSED, payload=item)
    line = Text()
    line.append(iid[:8], style="bold")
    line.append(" ")
    line.append(str(item.get("status", "?")), style="dim")
    line.append("  ")
    # humanize_event returns free text (e.g. "[gap] title") that may contain
    # literal brackets — append as plain text, not markup, so it can't be
    # misread as a (missing) rich style tag.
    line.append(humanize_event(ev))
    return line


def _insights_list_impl() -> None:
    async def _run() -> None:
        _, audit = _setup()
        svc = ReflectionService(audit=audit)
        items = await svc.list_insights(include_resolved=False)
        if not items:
            console.print("[dim]No open insights.[/dim]")
            return
        for item in items:
            console.print(_render_insight_line(item))

    asyncio.run(_run())


@insights_app.callback(invoke_without_command=True)
def insights_main(ctx: typer.Context) -> None:
    """List open insights, or use a subcommand (show/accept/dismiss)."""
    if ctx.invoked_subcommand is None:
        _insights_list_impl()


@insights_app.command("list")
def insights_list() -> None:
    """List open (unresolved) insights."""
    _insights_list_impl()


@insights_app.command("show")
def insights_show(
    insight_id: Annotated[str, typer.Argument(help="Insight id (full UUID).")],
) -> None:
    """Show full detail for one insight (open or resolved)."""

    async def _run() -> None:
        _, audit = _setup()
        svc = ReflectionService(audit=audit)
        items = await svc.list_insights(include_resolved=True)
        match = next((i for i in items if i.get("insight_id") == insight_id), None)
        if match is None:
            console.print(f"[yellow]No insight with id {insight_id}[/yellow]")
            raise typer.Exit(code=1)
        console.print(_render_insight_line(match))
        console.print(f"  detail: {escape(str(match.get('detail', '')))}")
        refs = match.get("refs") or []
        console.print(f"  refs: {', '.join(str(r) for r in refs)}")
        action = match.get("suggested_action")
        if action:
            console.print(f"  suggested_action: {escape(str(action))}")

    asyncio.run(_run())


@insights_app.command("accept")
def insights_accept(
    insight_id: Annotated[str, typer.Argument(help="Insight id (full UUID).")],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--no-apply",
            help="Actually perform the drafted action (default: just show it).",
        ),
    ] = False,
) -> None:
    """Accept an insight. Without --apply, only shows the drafted action."""

    async def _run() -> None:
        settings, audit = _setup()
        svc = ReflectionService(audit=audit)
        store = DurableMemoryStore(settings.memory_db_path) if apply else None
        embedder = DeterministicEmbeddingProvider() if apply else None
        try:
            result = await svc.accept(
                insight_id, apply=apply, store=store, embedder=embedder
            )
        except ValueError as e:
            console.print(f"[red]error:[/red] {escape(str(e))}")
            raise typer.Exit(code=1) from e
        if apply:
            console.print(f"[green]✓[/green] accepted + applied {insight_id[:8]}")
            console.print(f"  {escape(str(result.get('acted')))}")
        else:
            console.print(f"[green]✓[/green] accepted {insight_id[:8]} (not applied)")
            console.print(f"  drafted action: {escape(str(result.get('proposed_action')))}")

    asyncio.run(_run())


@insights_app.command("dismiss")
def insights_dismiss(
    insight_id: Annotated[str, typer.Argument(help="Insight id (full UUID).")],
    note: Annotated[
        str, typer.Option(help="Optional note explaining the dismissal.")
    ] = "",
) -> None:
    """Dismiss an insight without acting on it."""

    async def _run() -> None:
        _, audit = _setup()
        svc = ReflectionService(audit=audit)
        await svc.dismiss(insight_id, note=note)
        console.print(f"[dim]dismissed[/dim] {insight_id[:8]}")

    asyncio.run(_run())


@app.command()
def reflect(
    since: Annotated[
        int | None,
        typer.Option(
            "--since",
            help="Reflect over the last N days (overrides the default window).",
        ),
    ] = None,
    all_history: Annotated[
        bool, typer.Option("--all", help="Reflect over ALL memory, ignoring the window.")
    ] = False,
) -> None:
    """Run one reflection pass: dispatch a reflective agent over recent memory.

    The dispatched agent proposes insights via the insight_propose tool;
    review them afterwards with `precog insights`.
    """
    from exocortex.operator.mcp.dispatch import DispatchService  # noqa: PLC0415

    async def _run() -> None:
        settings, audit = _setup()
        if not settings.reflect_enabled:
            console.print(
                "[red]reflect is OFF[/red]. Enable with "
                "[bold]EXOCORTEX_REFLECT_ENABLED=true[/bold]."
            )
            raise typer.Exit(code=1)
        store = DurableMemoryStore(settings.memory_db_path)
        dispatcher = DispatchService(settings=settings)
        result = await run_reflection(
            audit=audit,
            store=store,
            settings=settings,
            dispatch=dispatcher.dispatch,
            since_days=since,
            all_history=all_history,
        )
        status = result.get("status", "?")
        color = "green" if status == "completed" else "red"
        console.print(
            f"[{color}]{status}[/{color}] · {result.get('insight_count', 0)} insight(s) "
            f"(reflection {str(result.get('reflection_id', ''))[:8]})"
        )
        if result.get("dispatched_to"):
            console.print(f"  dispatched to: {escape(str(result['dispatched_to']))}")
        if result.get("error"):
            console.print(f"  error: {escape(str(result['error']))}")

    asyncio.run(_run())


@app.command("chat-toggle")
def chat_toggle(
    state: Annotated[
        str,
        typer.Argument(help="'on' / 'off' / 'status'."),
    ] = "status",
) -> None:
    """Flip the master switch for memory chat (RAG over memory).

    Persistent across processes via a flag file in the data dir. When
    OFF (default), the `memory_chat` MCP tool returns a 'disabled' error.
    """
    settings, _ = _setup()
    flag = settings.chat_toggle_path
    requested = state.lower().strip()
    if requested in {"on", "enable", "true", "1"}:
        flag.write_text("enabled\n")
        console.print(
            f"[green]✓[/green] memory chat is now [bold]ON[/bold]\n"
            f"  endpoint: {settings.memory_chat_endpoint}\n"
            f"  embedding model: {settings.memory_chat_embedding_model}\n"
            f"  chat model: {settings.memory_chat_chat_model or '(auto-detect)'}"
        )
    elif requested in {"off", "disable", "false", "0"}:
        if flag.exists():
            flag.unlink()
        console.print("[dim]memory chat is now [bold]OFF[/bold][/dim]")
    elif requested in {"status", ""}:
        on = settings.memory_chat_enabled()
        color = "green" if on else "dim"
        label = "ON" if on else "OFF"
        console.print(f"[{color}]memory chat: [bold]{label}[/bold][/{color}]")
        console.print(f"  endpoint: {settings.memory_chat_endpoint}")
        console.print(
            f"  embedding model: {settings.memory_chat_embedding_model}"
        )
        console.print(
            f"  chat model: {settings.memory_chat_chat_model or '(auto-detect)'}"
        )
    else:
        raise typer.BadParameter(f"unknown state: {state!r}; use on|off|status")


@app.command()
def chat(
    question: Annotated[str, typer.Argument(help="Your question.")],
    top_k: Annotated[int, typer.Option(help="Records to retrieve.")] = 8,
    scope: Annotated[str | None, typer.Option(help="Scope filter.")] = None,
    scope_id: Annotated[str | None, typer.Option(help="Scope-id filter.")] = None,
) -> None:
    """Ask the exocortex a question (RAG over memory). Off by default —
    enable with `precog chat-toggle on`."""

    async def _run() -> None:
        from exocortex.operator.mcp.handlers import MemoryHandlers  # noqa: PLC0415

        settings, audit = _setup()
        if not settings.memory_chat_enabled():
            console.print(
                "[red]memory chat is OFF[/red]. Enable with "
                "[bold]precog chat-toggle on[/bold]."
            )
            raise typer.Exit(code=1)
        store = DurableMemoryStore(settings.memory_db_path)
        embedder = DeterministicEmbeddingProvider()
        retrieval = HybridRetrieval(store, embedder)
        handlers = MemoryHandlers(
            store=store, embedder=embedder, retrieval=retrieval,
            audit=audit, settings=settings,
        )
        result = await handlers.memory_chat(
            question=question, top_k=top_k, scope=scope, scope_id=scope_id
        )
        if result["status"] != "ok":
            console.print(f"[red]{result['status']}[/red]: {result.get('error', '')}")
            raise typer.Exit(code=2)
        console.print(result["answer"])
        cited = result.get("cited_record_ids") or []
        if cited:
            console.print()
            console.print(
                f"[dim]cited {len(cited)} record(s) · model: "
                f"{result['model']} · {result['latency_ms']}ms[/dim]"
            )

    asyncio.run(_run())


# --- Tools ------------------------------------------------------------------


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8756,
) -> None:
    """Run the operator web UI (read-only lens over audit log + memory)."""
    # Deferred imports: web stack only needed when `precog serve` is invoked.
    import uvicorn  # noqa: PLC0415

    from exocortex.operator.web.server import create_app  # noqa: PLC0415

    settings, _ = _setup()
    console.print(
        f"[green]Exocortex operator UI[/green] serving at "
        f"[bold]http://{host}:{port}[/bold]"
    )
    console.print(f"  audit log : {settings.audit_log_path}")
    console.print(f"  memory db : {settings.memory_db_path}")
    console.print(
        "  pages     : /  (dashboard)   /memory  (constellation)\n"
    )
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


@app.command()
def recall(
    agent: Annotated[
        str | None,
        typer.Option(help="Agent id this recall is for (purely informational)."),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of markdown."),
    ] = False,
) -> None:
    """Summarize unfinished work + recent decisions for a fresh session.

    Pipe into an agent's session-start hook (e.g. Claude Code `SessionStart`)
    or let the agent call the MCP `session_startup` tool. The agent then
    prompts the operator: "here's what we were working on — continue or
    start new?"
    """

    async def _run() -> None:
        settings, audit = _setup()
        store = DurableMemoryStore(settings.memory_db_path)
        from exocortex.operator.recall import RecallService  # noqa: PLC0415

        svc = RecallService(store=store, audit=audit)
        summary = await svc.summarize(agent_id=agent)
        if json_out:
            console.print_json(data=summary.to_dict())
            return
        console.print(summary.text_for_user)
        if summary.suggested_prompts:
            console.print("")
            console.print("[dim]Suggested prompts:[/dim]")
            for p in summary.suggested_prompts:
                console.print(f"  • {p}")

    asyncio.run(_run())


daemon_app = typer.Typer(
    help="Long-running exocortex daemon (web server + MCP routing)."
)
app.add_typer(daemon_app, name="daemon")


def _daemon_pid_path(settings: Settings) -> Path:
    return settings.data_dir / "daemon.pid"


def _daemon_log_path(settings: Settings) -> Path:
    return settings.data_dir / "daemon.log"


def _read_daemon_pid(settings: Settings) -> int | None:
    p = _daemon_pid_path(settings)
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
    except (ValueError, OSError):
        return None
    # Check the process actually exists.
    try:
        import os  # noqa: PLC0415

        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        # Stale pid file.
        return None
    return pid


@daemon_app.command("start")
def daemon_start(
    port: Annotated[
        int, typer.Option(help="Port for the web UI + API.")
    ] = 8756,
    foreground: Annotated[
        bool,
        typer.Option(
            "--foreground", "-f", help="Run in the current terminal (no detach)."
        ),
    ] = False,
) -> None:
    """Start the exocortex daemon.

    The daemon is the long-running process that holds the web UI, the
    audit-log tailer, the WebSocket fan-out, the conversation router,
    and (in future) the persistent dispatch queue. It's the same code
    as `precog serve` — `daemon` adds detached lifecycle + PID
    management on top.
    """
    import os  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    settings, _ = _setup()
    pid = _read_daemon_pid(settings)
    if pid is not None:
        console.print(
            f"[yellow]daemon already running[/yellow] (pid {pid}). "
            f"`precog daemon stop` first if you want to restart."
        )
        raise typer.Exit(1)

    if foreground:
        # Inline run — typically for debugging.
        import uvicorn  # noqa: PLC0415

        from exocortex.operator.web.server import create_app  # noqa: PLC0415

        console.print(
            f"[green]▶[/green] daemon foreground on port {port} "
            f"(Ctrl-C to stop)"
        )
        uvicorn.run(create_app(settings), host="127.0.0.1", port=port)
        return

    # Detached: spawn precog serve as a background process.
    log_path = _daemon_log_path(settings)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603
        ["precog", "serve", "--port", str(port)],
        stdout=log_fd,
        stderr=log_fd,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    _daemon_pid_path(settings).write_text(str(proc.pid) + "\n")
    console.print(
        f"[green]▶[/green] daemon started (pid {proc.pid}, port {port}, "
        f"log {log_path})"
    )
    # Quick liveness check.
    import time  # noqa: PLC0415

    time.sleep(1.5)
    still_alive = _read_daemon_pid(settings)
    if still_alive is None:
        console.print(
            f"[red]✗[/red] daemon died on startup. Check {log_path}."
        )
        raise typer.Exit(1)
    _ = os  # touch import to silence linters if unused above


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the exocortex daemon (SIGTERM, then SIGKILL if needed)."""
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415
    import time  # noqa: PLC0415

    settings, _ = _setup()
    pid = _read_daemon_pid(settings)
    if pid is None:
        console.print("[dim]daemon not running.[/dim]")
        # Clear stale pid file if any.
        with contextlib.suppress(FileNotFoundError):
            _daemon_pid_path(settings).unlink()
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _daemon_pid_path(settings).unlink(missing_ok=True)
        console.print("[dim]daemon already gone.[/dim]")
        return
    # Give it a moment to shut down cleanly.
    for _ in range(20):
        time.sleep(0.1)
        if _read_daemon_pid(settings) is None:
            break
    if _read_daemon_pid(settings) is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    _daemon_pid_path(settings).unlink(missing_ok=True)
    console.print(f"[green]■[/green] daemon stopped (pid {pid}).")


@daemon_app.command("status")
def daemon_status() -> None:
    """Is the daemon running? Show its pid, port, log path."""
    settings, _ = _setup()
    pid = _read_daemon_pid(settings)
    if pid is None:
        console.print("[dim]daemon: [bold]NOT RUNNING[/bold][/dim]")
        console.print("  start with: [bold]precog daemon start[/bold]")
        return
    console.print(f"[green]daemon: [bold]RUNNING[/bold][/green] (pid {pid})")
    console.print(f"  log:  {_daemon_log_path(settings)}")
    console.print(f"  pid:  {_daemon_pid_path(settings)}")
    console.print("  ui:   http://127.0.0.1:8756/")


@app.command("mcp-server")
def mcp_server() -> None:
    """Run exocortex as an MCP server over stdio.

    Lets any MCP-client agent (Hermes, Codex, Claude Code) read from and
    write to exocortex's shared memory — regardless of how the agent was
    launched. One-time wiring per agent:

        hermes mcp add exocortex --command "uv run precog mcp-server" \\
            --cwd /path/to/exocortex
        codex mcp add --name exocortex -- uv run precog mcp-server
    """
    # Deferred import: MCP stack only loads when this command is invoked.
    from exocortex.operator.mcp.server import run_stdio  # noqa: PLC0415

    _setup()  # ensure data dirs + logging
    run_stdio()


# --- Profile -------------------------------------------------------------


def _profile_setup() -> tuple[Settings, AuditLog, Any, Any, Any]:
    from exocortex.memory.durable import DurableMemoryStore  # noqa: PLC0415
    from exocortex.memory.embedding import DeterministicEmbeddingProvider  # noqa: PLC0415
    from exocortex.memory.retrieval import HybridRetrieval  # noqa: PLC0415

    settings, audit = _setup()
    store = DurableMemoryStore(settings.memory_db_path)
    embedder = DeterministicEmbeddingProvider()
    retrieval = HybridRetrieval(store, embedder)
    return settings, audit, store, embedder, retrieval


@profile_app.command("show")
def profile_show(
    dimension: Annotated[
        str | None,
        typer.Option(help="Filter to a single dimension (e.g. preference)."),
    ] = None,
) -> None:
    """Print the operator's profile, grouped by dimension."""
    from exocortex.memory.profile import PROFILE_DIMENSIONS, ProfileService  # noqa: PLC0415

    async def _run() -> None:
        settings, audit, store, embedder, retrieval = _profile_setup()
        service = ProfileService(
            store=store,
            embedder=embedder,
            retrieval=retrieval,
            audit=audit,
            user_id=settings.profile_user_id,
            frozen=settings.profile_frozen(),
        )
        records = await service.list_records()
        if not records:
            console.print(
                "[dim]no profile records yet — agents will populate this as they "
                "observe you, or seed manually with[/dim] "
                "[bold]precog memory write --scope user --scope-id "
                f"{settings.profile_user_id} --type profile.preference '...'[/bold]"
            )
            await store.close()
            return

        canonical_types = [d[0] for d in PROFILE_DIMENSIONS]
        target_filter = (
            f"profile.{dimension}"
            if dimension and not dimension.startswith("profile.")
            else dimension
        )

        for t in canonical_types + ["profile.question", "other"]:
            if target_filter and t not in (target_filter, "other"):
                continue
            if t == "other":
                items = [
                    r
                    for r in records
                    if r.type not in canonical_types
                    and r.type != "profile.question"
                ]
            else:
                items = [r for r in records if r.type == t]
            if not items:
                continue
            console.print(
                f"\n[bold cyan]{t.upper()}[/bold cyan] [dim]({len(items)})[/dim]"
            )
            for r in items:
                conf_color = {
                    "asserted": "green",
                    "observed": "blue",
                    "inferred": "yellow",
                    "external_claim": "dim",
                }.get(r.confidence.value, "white")
                console.print(
                    f"  [dim]{str(r.id)[:8]}[/dim] · "
                    f"[{conf_color}]{r.confidence.value}[/{conf_color}] · "
                    f"{r.source} · [dim]{r.timestamp.isoformat()[:19]}[/dim]"
                )
                console.print(f"    {r.content}")
        if settings.profile_frozen():
            console.print(
                "\n[yellow]⏸ profile collection is FROZEN[/yellow] — "
                "run [bold]precog profile freeze off[/bold] to resume"
            )
        await store.close()

    asyncio.run(_run())


@profile_app.command("redact")
def profile_redact(
    record_id: Annotated[str, typer.Argument(help="Record id to redact (full UUID).")],
) -> None:
    """Hard-delete a profile record. Audit-logged."""
    from exocortex.memory.profile import ProfileService  # noqa: PLC0415

    async def _run() -> None:
        settings, audit, store, embedder, retrieval = _profile_setup()
        service = ProfileService(
            store=store,
            embedder=embedder,
            retrieval=retrieval,
            audit=audit,
            user_id=settings.profile_user_id,
            frozen=settings.profile_frozen(),
        )
        try:
            result = await service.redact(record_id=record_id)
        except ValueError as e:
            console.print(f"[red]error:[/red] {e}")
            await store.close()
            raise typer.Exit(1) from e
        if result["status"] == "redacted":
            console.print(f"[green]✓[/green] redacted {record_id[:8]}")
        else:
            console.print(f"[yellow]not found:[/yellow] {record_id}")
        await store.close()

    asyncio.run(_run())


@profile_app.command("freeze")
def profile_freeze(
    state: Annotated[
        str, typer.Argument(help="'on' / 'off' / 'status'.")
    ] = "status",
) -> None:
    """Pause / resume profile collection. When ON, agents observing the
    operator are blocked at the API boundary."""
    settings, _ = _setup()
    flag = settings.profile_freeze_path
    s = state.lower().strip()
    if s in {"on", "freeze", "true", "1"}:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("frozen\n")
        console.print(
            "[yellow]⏸[/yellow] profile collection is now [bold]FROZEN[/bold]"
        )
    elif s in {"off", "resume", "thaw", "false", "0"}:
        if flag.exists():
            flag.unlink()
        console.print("[green]▶[/green] profile collection is now [bold]LIVE[/bold]")
    elif s in {"status", ""}:
        frozen = settings.profile_frozen()
        if frozen:
            console.print("[yellow]profile: [bold]FROZEN[/bold][/yellow]")
        else:
            console.print("[green]profile: [bold]LIVE[/bold][/green]")
        console.print(f"  user_id: {settings.profile_user_id}")
        console.print(f"  flag path: {flag}")
    else:
        raise typer.BadParameter(f"unknown state: {state!r}; use on|off|status")


@profile_app.command("export")
def profile_export() -> None:
    """Print the entire profile as JSON for portability."""
    import json as _json  # noqa: PLC0415

    from exocortex.memory.profile import ProfileService  # noqa: PLC0415

    async def _run() -> None:
        settings, audit, store, embedder, retrieval = _profile_setup()
        service = ProfileService(
            store=store,
            embedder=embedder,
            retrieval=retrieval,
            audit=audit,
            user_id=settings.profile_user_id,
            frozen=settings.profile_frozen(),
        )
        data = await service.export()
        console.print_json(_json.dumps(data))
        await store.close()

    asyncio.run(_run())


@profile_app.command("question")
def profile_question(
    apply: Annotated[
        bool, typer.Option(help="Seed the queue with gap-driven questions.")
    ] = False,
) -> None:
    """Show pending questions (default) or seed new ones from gaps (`--apply`)."""
    from exocortex.memory.profile import ProfileService  # noqa: PLC0415

    async def _run() -> None:
        settings, audit, store, embedder, retrieval = _profile_setup()
        service = ProfileService(
            store=store,
            embedder=embedder,
            retrieval=retrieval,
            audit=audit,
            user_id=settings.profile_user_id,
            frozen=settings.profile_frozen(),
        )
        if apply:
            gaps = await service.find_gaps()
            existing = await service.list_questions(status="open")
            existing_dims = {q.dimension for q in existing}
            added = 0
            for g in gaps[:5]:
                if g.dimension in existing_dims:
                    continue
                rec = await service.question(
                    content=g.suggested_question, dimension=g.dimension
                )
                console.print(
                    f"[green]+[/green] [{rec.type}] {rec.content} "
                    f"[dim]({str(rec.id)[:8]})[/dim]"
                )
                added += 1
            if added == 0:
                console.print("[dim]no new questions to seed.[/dim]")
            else:
                console.print(f"\n[green]✓[/green] seeded {added} question(s).")
        else:
            questions = await service.list_questions(status="open")
            if not questions:
                console.print(
                    "[dim]no open questions. Run with --apply to seed from gaps.[/dim]"
                )
            else:
                for q in questions:
                    console.print(
                        f"[bold]?[/bold] [dim]{q.record_id[:8]}[/dim] "
                        f"[cyan]{q.dimension}[/cyan]"
                    )
                    console.print(f"    {q.content}")
        await store.close()

    asyncio.run(_run())


@profile_app.command("answer")
def profile_answer(
    question_id: Annotated[
        str, typer.Argument(help="Question record id (full UUID).")
    ],
    answer: Annotated[str, typer.Argument(help="The operator's answer.")],
) -> None:
    """Close a profile question with an answer. The answer becomes a new
    asserted profile record."""
    from exocortex.memory.profile import ProfileService  # noqa: PLC0415

    async def _run() -> None:
        settings, audit, store, embedder, retrieval = _profile_setup()
        service = ProfileService(
            store=store,
            embedder=embedder,
            retrieval=retrieval,
            audit=audit,
            user_id=settings.profile_user_id,
            frozen=settings.profile_frozen(),
        )
        try:
            result = await service.answer(question_id=question_id, answer=answer)
        except ValueError as e:
            console.print(f"[red]error:[/red] {e}")
            await store.close()
            raise typer.Exit(1) from e
        console.print(
            f"[green]✓[/green] answered → new {result['dimension']} "
            f"[dim]({result['new_record_id'][:8]})[/dim]"
        )
        await store.close()

    asyncio.run(_run())


@app.command()
def tools() -> None:
    """List registered tools with risk tier + category."""
    registry = ToolRegistry()
    register_builtins(registry)

    table = Table(title="Registered tools")
    table.add_column("Name", style="bold")
    table.add_column("Category")
    table.add_column("Risk")
    table.add_column("Description")
    for spec in registry.all():
        risk_style = {"high": "red", "medium": "yellow", "low": "green"}.get(
            spec.risk_tier.value, "white"
        )
        table.add_row(
            spec.name,
            spec.category.value,
            f"[{risk_style}]{spec.risk_tier.value}[/{risk_style}]",
            spec.description,
        )
    console.print(table)


if __name__ == "__main__":
    app()
