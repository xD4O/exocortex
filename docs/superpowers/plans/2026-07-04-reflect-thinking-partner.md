# Reflect — Thinking-Partner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reflective agent that reads a window of memory (via dispatch) and proposes typed, grounded Insights (contradiction / pattern / gap / synthesis) into a reviewable queue where the operator accepts (proposes) then confirms (applies) a mutation, or dismisses.

**Architecture:** Event-sourced, mirroring the conversations subsystem. New `REFLECTION_*` / `INSIGHT_*` audit events are the source of truth; the queue, web page, and session-startup surfacing are all projections over `AuditLog`. Insights are created only through a new `insight_propose` MCP tool (structured, grounded, validated). A reflective run is an ordinary dispatch to a capable agent. Nothing mutates memory/policy until the operator explicitly applies an accepted insight.

**Tech Stack:** Python **3.12+** (`requires-python = ">=3.12"`; the code uses `StrEnum` and `X | None`) · Pydantic v2 · anyio · FastMCP · FastAPI · typer · pytest + pytest-asyncio. (Match existing patterns in `src/exocortex/`.)

## Global Constraints

- Every contract carries `schema_version: Literal[1] = 1`; additive-only within a major version. (Copy from `contracts/event.py`.)
- Every `MemoryRecord` write carries provenance (`source`, `confidence`, `timestamp`, `scope`). No quick-write path.
- The audit log is append-only and the single source of truth; every view is a projection. No new mutable on-disk state.
- Insights are always `confidence=inferred` and inert until accepted; accept proposes, a separate apply confirms. Never auto-mutate memory or policy.
- Reflect is **off by default** (`EXOCORTEX_REFLECT_ENABLED=false`), like memory-chat.
- Env vars use the `EXOCORTEX_` prefix (pydantic-settings), defaults conservative.
- `ruff check src tests` and `mypy src` must stay clean; the full `pytest` suite must stay green. **Watch for unused imports in test files** — the suite lints `tests/` too.
- Tests use `ScriptedProcess` (from `exocortex.agents.bridge.process`) — never real `codex`/`hermes` binaries.
- **Every new MCP tool MUST be added to `EXPECTED_TOOLS` in `tests/unit/test_mcp_server_smoke.py`.** That test asserts `names == EXPECTED_TOOLS` (exact set) — a new tool without the corresponding `EXPECTED_TOOLS` entry turns the suite red. This plan adds two tools: `insight_propose` (Task 4) and `reflect` (Task 5).

---

## File Structure

- Create `src/exocortex/contracts/insight.py` — `InsightKind`, `SuggestedAction`, `Insight` models.
- Modify `src/exocortex/contracts/event.py` — add `REFLECTION_STARTED/COMPLETED`, `INSIGHT_PROPOSED/ACCEPTED/DISMISSED` to `EventKind`.
- Modify `src/exocortex/contracts/__init__.py` — export the new models.
- Create `src/exocortex/memory/reflect.py` — `ReflectionService` (window computation, queue projection, accept/dismiss/apply).
- Create `src/exocortex/coordination/reflect_goal.py` — `build_reflect_goal(records)` prompt builder.
- Modify `src/exocortex/observability/humanize.py` — sentences for the new event kinds.
- Modify `src/exocortex/config.py` — `reflect_*` settings.
- Modify `src/exocortex/operator/mcp/server.py` — `insight_propose` tool + `reflect` dispatch tool.
- Modify `src/exocortex/operator/mcp/handlers.py` — `session_startup` gains `pending_insights`.
- Modify `src/exocortex/operator/cli.py` — `precog reflect`, `precog insights`.
- Modify `src/exocortex/operator/web/routes.py` + create `static/reflect.html`, `static/reflect.js` — the `/reflect` page (projection). *(UI task; verify live per the project's browser-verification practice.)*
- Tests mirror layout under `tests/unit/`.

---

### Task 1: Insight contracts + event kinds

**Files:**
- Create: `src/exocortex/contracts/insight.py`
- Modify: `src/exocortex/contracts/event.py` (EventKind enum), `src/exocortex/contracts/__init__.py`
- Test: `tests/unit/test_insight_contract.py`

**Interfaces:**
- Produces: `InsightKind` (StrEnum: `CONTRADICTION`, `PATTERN`, `GAP`, `SYNTHESIS`), `SuggestedAction`, `Insight`; `EventKind.REFLECTION_STARTED/REFLECTION_COMPLETED/INSIGHT_PROPOSED/INSIGHT_ACCEPTED/INSIGHT_DISMISSED`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_insight_contract.py
from __future__ import annotations
import uuid
import pytest
from pydantic import ValidationError
from exocortex.contracts import Confidence, Event, EventKind
from exocortex.contracts.insight import Insight, InsightKind, SuggestedAction


def test_insight_requires_grounding() -> None:
    rid = uuid.uuid4()
    ok = Insight(kind=InsightKind.CONTRADICTION, title="t", detail="d",
                 refs=[rid], reflection_id=uuid.uuid4())
    assert ok.confidence == Confidence.INFERRED
    assert ok.suggested_action.type == "none"
    with pytest.raises(ValidationError):  # empty refs rejected
        Insight(kind=InsightKind.GAP, title="t", detail="d", refs=[],
                reflection_id=uuid.uuid4())


def test_suggested_action_supersede() -> None:
    a = SuggestedAction(type="supersede", stale_record_id=uuid.uuid4())
    assert a.type == "supersede" and a.stale_record_id is not None


def test_new_event_kinds_exist() -> None:
    for k in ("REFLECTION_STARTED", "REFLECTION_COMPLETED",
              "INSIGHT_PROPOSED", "INSIGHT_ACCEPTED", "INSIGHT_DISMISSED"):
        assert hasattr(EventKind, k)
    Event(kind=EventKind.INSIGHT_PROPOSED, payload={"insight_id": "x"})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_insight_contract.py -q`
Expected: FAIL — `ModuleNotFoundError: exocortex.contracts.insight`.

- [ ] **Step 3: Add the event kinds**

In `src/exocortex/contracts/event.py`, inside `class EventKind(StrEnum)`, after the `MEMORY_*` block add:

```python
    REFLECTION_STARTED = "reflection.started"
    REFLECTION_COMPLETED = "reflection.completed"
    INSIGHT_PROPOSED = "insight.proposed"
    INSIGHT_ACCEPTED = "insight.accepted"
    INSIGHT_DISMISSED = "insight.dismissed"
```

- [ ] **Step 4: Create the contracts**

```python
# src/exocortex/contracts/insight.py
from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, Field
from exocortex.contracts.common import Confidence, new_id, now


class InsightKind(StrEnum):
    CONTRADICTION = "contradiction"
    PATTERN = "pattern"
    GAP = "gap"
    SYNTHESIS = "synthesis"


class SuggestedAction(BaseModel):
    type: Literal["supersede", "create_rule", "track_gap",
                  "record_decision", "none"] = "none"
    stale_record_id: UUID | None = None      # supersede
    rule: dict[str, Any] | None = None        # create_rule (a Rule literal)
    question: str | None = None               # track_gap
    dimension: str | None = None              # track_gap
    content: str | None = None                # record_decision


class Insight(BaseModel):
    schema_version: Literal[1] = 1
    id: UUID = Field(default_factory=new_id)
    kind: InsightKind
    title: str
    detail: str
    refs: list[UUID] = Field(min_length=1)   # grounding is mandatory
    suggested_action: SuggestedAction = Field(default_factory=SuggestedAction)
    confidence: Confidence = Confidence.INFERRED
    reflection_id: UUID
    created_at: datetime = Field(default_factory=now)
```

- [ ] **Step 5: Export from the package**

In `src/exocortex/contracts/__init__.py` add an import + `__all__` entries:

```python
from exocortex.contracts.insight import Insight, InsightKind, SuggestedAction
```
Add `"Insight"`, `"InsightKind"`, `"SuggestedAction"` to `__all__`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_insight_contract.py -q`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add src/exocortex/contracts/insight.py src/exocortex/contracts/event.py src/exocortex/contracts/__init__.py tests/unit/test_insight_contract.py
git commit -m "feat(reflect): Insight contracts + reflection/insight event kinds"
```

---

### Task 2: Queue projection (fold INSIGHT_* events into queue state)

**Files:**
- Create: `src/exocortex/memory/reflect.py`
- Test: `tests/unit/test_reflect_projection.py`

**Interfaces:**
- Consumes: `AuditLog` (from `exocortex.observability.audit`), `Insight` (Task 1).
- Produces: `ReflectionService(audit: AuditLog)`; `async def list_insights(self, *, include_resolved: bool=False) -> list[dict]` returning `{**insight_payload, "status": "proposed"|"accepted"|"dismissed"}`, newest first; helper `record_proposed`, `record_accepted`, `record_dismissed` are added in later tasks.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reflect_projection.py
from __future__ import annotations
import uuid
from pathlib import Path
import pytest
from exocortex.contracts import Event, EventKind
from exocortex.observability.audit import AuditLog
from exocortex.memory.reflect import ReflectionService


def _proposed(iid: str) -> Event:
    return Event(kind=EventKind.INSIGHT_PROPOSED,
                 payload={"insight_id": iid, "kind": "gap", "title": "t",
                          "detail": "d", "refs": [str(uuid.uuid4())]})


@pytest.mark.asyncio
async def test_projection_status(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    a, b, c = "ins-a", "ins-b", "ins-c"
    await audit.record(_proposed(a))
    await audit.record(_proposed(b))
    await audit.record(_proposed(c))
    await audit.record(Event(kind=EventKind.INSIGHT_ACCEPTED, payload={"insight_id": b}))
    await audit.record(Event(kind=EventKind.INSIGHT_DISMISSED, payload={"insight_id": c}))

    open_q = await svc.list_insights()
    ids = {i["insight_id"] for i in open_q}
    assert ids == {a}                       # only unresolved by default
    assert open_q[0]["status"] == "proposed"

    allq = await svc.list_insights(include_resolved=True)
    by_id = {i["insight_id"]: i["status"] for i in allq}
    assert by_id == {a: "proposed", b: "accepted", c: "dismissed"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_reflect_projection.py -q`
Expected: FAIL — `ModuleNotFoundError: exocortex.memory.reflect`.

- [ ] **Step 3: Implement the projection**

```python
# src/exocortex/memory/reflect.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_reflect_projection.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/exocortex/memory/reflect.py tests/unit/test_reflect_projection.py
git commit -m "feat(reflect): ReflectionService queue projection over insight events"
```

---

### Task 3: Window computation

**Files:**
- Modify: `src/exocortex/memory/reflect.py`
- Test: `tests/unit/test_reflect_window.py`

**Interfaces:**
- Produces: `async def window_from(self, *, max_days: int, override_days: int | None = None, all_history: bool = False) -> datetime | None` — returns the lower time bound to reflect over: the timestamp of the last `REFLECTION_COMPLETED`, floored to `now - max_days`; `override_days` sets an explicit N-day window; `all_history=True` returns `None` (no lower bound).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reflect_window.py
from __future__ import annotations
from datetime import timedelta
from pathlib import Path
import pytest
from exocortex.contracts import Event, EventKind
from exocortex.contracts.common import now
from exocortex.observability.audit import AuditLog
from exocortex.memory.reflect import ReflectionService


@pytest.mark.asyncio
async def test_window_all_and_override(tmp_path: Path) -> None:
    svc = ReflectionService(audit=AuditLog(tmp_path / "a.jsonl"))
    assert await svc.window_from(max_days=7, all_history=True) is None
    lo = await svc.window_from(max_days=7, override_days=2)
    assert (now() - lo) < timedelta(days=2, hours=1)


@pytest.mark.asyncio
async def test_window_since_last_reflection_capped(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    old = now() - timedelta(days=30)
    await audit.record(Event(kind=EventKind.REFLECTION_COMPLETED,
                             timestamp=old, payload={"status": "completed"}))
    lo = await svc.window_from(max_days=7)
    # capped at now-7d even though last reflection was 30d ago
    assert (now() - lo) < timedelta(days=7, hours=1)
    assert (now() - lo) > timedelta(days=6)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_reflect_window.py -q`
Expected: FAIL — `AttributeError: 'ReflectionService' object has no attribute 'window_from'`.

- [ ] **Step 3: Implement window_from**

Add to `ReflectionService` in `src/exocortex/memory/reflect.py` (add `from datetime import datetime, timedelta` and `from exocortex.contracts.common import now` to imports):

```python
    async def window_from(self, *, max_days: int, override_days: int | None = None,
                          all_history: bool = False) -> datetime | None:
        current = now()
        if all_history:
            return None
        cap = current - timedelta(days=override_days if override_days else max_days)
        if override_days:
            return cap
        last = None
        for ev in await self.audit.read_all():
            if ev.kind == EventKind.REFLECTION_COMPLETED:
                last = ev.timestamp
        if last is None:
            return cap
        return max(last, cap)  # never reflect further back than the cap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_reflect_window.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/exocortex/memory/reflect.py tests/unit/test_reflect_window.py
git commit -m "feat(reflect): reflect-window computation (since-last, capped, override, all)"
```

---

### Task 4: `insight_propose` MCP tool + config

**Files:**
- Modify: `src/exocortex/config.py`, `src/exocortex/operator/mcp/server.py`
- Test: `tests/unit/test_insight_propose.py`

**Interfaces:**
- Consumes: `Insight` (Task 1), `handlers.audit` (`AuditLog`).
- Produces: MCP tool `insight_propose(kind, title, detail, refs, reflection_id, action_type="none", action_payload=None)` → emits `INSIGHT_PROPOSED` with the validated `Insight` payload; returns `{"insight_id": ...}`. Raises `ValueError` on empty refs / bad kind. Config: `reflect_enabled: bool=False`, `reflect_window_days: int=7`, `reflect_agent: str=""`, `reflect_max_insights: int=20`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_insight_propose.py
from __future__ import annotations
import uuid
from pathlib import Path
import pytest
from exocortex.contracts import EventKind
from exocortex.observability.audit import AuditLog
from exocortex.operator.mcp.server import _propose_insight  # helper under test


@pytest.mark.asyncio
async def test_propose_emits_event(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    rid = str(uuid.uuid4())
    out = await _propose_insight(audit, kind="contradiction", title="t", detail="d",
                                 refs=[rid], reflection_id=str(uuid.uuid4()),
                                 action_type="supersede",
                                 action_payload={"stale_record_id": rid})
    assert "insight_id" in out
    events = await audit.read_all()
    proposed = [e for e in events if e.kind == EventKind.INSIGHT_PROPOSED]
    assert len(proposed) == 1
    assert proposed[0].payload["kind"] == "contradiction"
    assert proposed[0].payload["refs"] == [rid]


@pytest.mark.asyncio
async def test_propose_rejects_empty_refs(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    with pytest.raises(ValueError):
        await _propose_insight(audit, kind="gap", title="t", detail="d",
                               refs=[], reflection_id=str(uuid.uuid4()))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_insight_propose.py -q`
Expected: FAIL — `ImportError: cannot import name '_propose_insight'`.

- [ ] **Step 3: Add config settings**

In `src/exocortex/config.py`, in `class Settings`, add near the other feature flags:

```python
    # Reflect — reflective thinking-partner. Off by default (opt-in like chat).
    reflect_enabled: bool = False
    reflect_window_days: int = 7
    reflect_agent: str = ""          # preferred agent; "" = capability-routed
    reflect_max_insights: int = 20
```

- [ ] **Step 4: Implement the helper + tool**

In `src/exocortex/operator/mcp/server.py`, add a module-level helper (near the other helpers) — this is what the test imports and what the tool calls:

```python
from exocortex.contracts import Event, EventKind
from exocortex.contracts.insight import Insight, InsightKind, SuggestedAction

async def _propose_insight(audit, *, kind, title, detail, refs, reflection_id,
                           action_type="none", action_payload=None):
    from uuid import UUID
    insight = Insight(
        kind=InsightKind(kind),
        title=title,
        detail=detail,
        refs=[UUID(r) for r in refs],                       # empty → ValidationError
        reflection_id=UUID(reflection_id),
        suggested_action=SuggestedAction(type=action_type, **(action_payload or {})),
    )
    payload = insight.model_dump(mode="json")
    payload["insight_id"] = payload.pop("id")
    await audit.record(Event(kind=EventKind.INSIGHT_PROPOSED, actor="reflect",
                             reason=title, payload=payload))
    return {"insight_id": payload["insight_id"]}
```

Wrap `ValidationError` as `ValueError` at the call site: change the `refs=[UUID(r) ...]` line to guard `if not refs: raise ValueError("insight requires >=1 grounding refs")` before constructing.

Then register the tool inside `build_mcp_server` (after the other tools):

```python
    @mcp.tool()
    async def insight_propose(
        kind: Annotated[Literal["contradiction", "pattern", "gap", "synthesis"], Field(description="Insight type.")],
        title: Annotated[str, Field(description="One-line summary.")],
        detail: Annotated[str, Field(description="The reasoning.")],
        refs: Annotated[list[str], Field(description="Memory record UUIDs this is grounded in. REQUIRED.")],
        reflection_id: Annotated[str, Field(description="The current reflection run id (from your goal).")],
        action_type: Annotated[Literal["supersede", "create_rule", "track_gap", "record_decision", "none"], Field(description="Optional suggested action type.")] = "none",
        action_payload: Annotated[dict[str, Any] | None, Field(description="Fields for the action (e.g. {'stale_record_id': ...}).")] = None,
    ) -> dict[str, Any]:
        """Propose a grounded insight during a reflection run. Rejected if refs is empty."""
        return await _propose_insight(handlers.audit, kind=kind, title=title, detail=detail,
                                      refs=refs, reflection_id=reflection_id,
                                      action_type=action_type, action_payload=action_payload)
```

- [ ] **Step 5: Register the tool in the smoke-test allowlist**

Add `"insight_propose"` to the `EXPECTED_TOOLS` set in `tests/unit/test_mcp_server_smoke.py` (near the top of the file). Without this, `test_expected_tools` fails with `missing/extra tools: {'insight_propose'}`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_insight_propose.py tests/unit/test_mcp_server_smoke.py -q`
Expected: PASS (both — the new tool test and the smoke test that now expects `insight_propose`).

- [ ] **Step 7: Commit**

```bash
git add src/exocortex/config.py src/exocortex/operator/mcp/server.py tests/unit/test_insight_propose.py tests/unit/test_mcp_server_smoke.py
git commit -m "feat(reflect): insight_propose MCP tool + reflect config"
```

---

### Task 5: Reflect goal builder + `reflect` dispatch tool

**Files:**
- Create: `src/exocortex/coordination/reflect_goal.py`
- Modify: `src/exocortex/operator/mcp/server.py`, `src/exocortex/memory/reflect.py`
- Test: `tests/unit/test_reflect_goal.py`, `tests/unit/test_reflect_run.py`

**Interfaces:**
- Consumes: `ReflectionService.window_from` (Task 3), `DispatchService` (`operator/mcp/dispatch.py`).
- Produces: `build_reflect_goal(reflection_id: str, records: list[MemoryRecord], max_insights: int) -> str`; `ReflectionService.start_run() -> str` records `REFLECTION_STARTED` and returns `reflection_id`; `complete_run(reflection_id, status, count)` records `REFLECTION_COMPLETED`. MCP tool `reflect(since_days=None, all_history=False)`.

- [ ] **Step 1: Write the failing test (goal builder is pure)**

```python
# tests/unit/test_reflect_goal.py
from __future__ import annotations
from exocortex.contracts import Confidence, MemoryRecord, MemoryScope
from exocortex.coordination.reflect_goal import build_reflect_goal


def test_goal_mentions_tool_kinds_and_records() -> None:
    rec = MemoryRecord(type="observation", content="chose SQLite over Postgres",
                       source="codex", confidence=Confidence.OBSERVED,
                       scope=MemoryScope.PROJECT, scope_id="exocortex")
    goal = build_reflect_goal("refl-1", [rec], max_insights=20)
    assert "insight_propose" in goal
    for kind in ("contradiction", "pattern", "gap", "synthesis"):
        assert kind in goal
    assert "refl-1" in goal
    assert str(rec.id) in goal            # records are cited so the agent can ground refs
    assert "20" in goal                   # max insights communicated
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_reflect_goal.py -q`
Expected: FAIL — `ModuleNotFoundError: exocortex.coordination.reflect_goal`.

- [ ] **Step 3: Implement the goal builder**

```python
# src/exocortex/coordination/reflect_goal.py
from __future__ import annotations
from exocortex.contracts import MemoryRecord


def build_reflect_goal(reflection_id: str, records: list[MemoryRecord],
                       max_insights: int) -> str:
    lines = [f"- {r.id} [{r.scope.value}/{r.source}/{r.confidence.value}] "
             f"{r.content[:300]}" for r in records]
    catalog = "\n".join(lines) if lines else "(no records in window)"
    return f"""You are exocortex's reflective analyst. Review the memory records below and surface INSIGHTS.

Reflection run id: {reflection_id}

For each finding, call the MCP tool `insight_propose` with reflection_id={reflection_id!r}.
Propose at most {max_insights} insights, highest-value first. Every insight MUST cite the
record UUID(s) it is grounded in via `refs` — an insight with no refs is rejected.

Insight kinds:
- contradiction: two records conflict (e.g. one says X, another not-X). suggested action_type
  "supersede" with action_payload {{"stale_record_id": "<the outdated one>"}}.
- pattern: a recurring decision/approval worth a policy rule. action_type "create_rule".
- gap: an important unanswered question. action_type "track_gap" with
  {{"question": "...", "dimension": "..."}}.
- synthesis: a durable summary of what changed / was learned. action_type "record_decision"
  with {{"content": "..."}}.

Use `memory_search` / `memory_get` if you need fuller record content. Do NOT write files or
run shell commands — the only output that matters is your `insight_propose` calls.

Records in window:
{catalog}
"""
```

- [ ] **Step 4: Run the goal test**

Run: `uv run pytest tests/unit/test_reflect_goal.py -q`
Expected: PASS.

- [ ] **Step 5: Write the run-orchestration test**

```python
# tests/unit/test_reflect_run.py
from __future__ import annotations
from pathlib import Path
import pytest
from exocortex.contracts import EventKind
from exocortex.observability.audit import AuditLog
from exocortex.memory.reflect import ReflectionService


@pytest.mark.asyncio
async def test_start_and_complete_run(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    rid = await svc.start_run(agent="codex", window_from=None)
    await svc.complete_run(rid, status="completed", count=3)
    kinds = [e.kind for e in await audit.read_all()]
    assert EventKind.REFLECTION_STARTED in kinds
    assert EventKind.REFLECTION_COMPLETED in kinds
    completed = [e for e in await audit.read_all()
                 if e.kind == EventKind.REFLECTION_COMPLETED][0]
    assert completed.payload["reflection_id"] == rid
    assert completed.payload["insight_count"] == 3
```

- [ ] **Step 6: Implement start_run / complete_run / count_for_run**

Add to `ReflectionService` (import `Event`, `EventKind` — already imported — and `new_id` from `exocortex.contracts.common`):

```python
    async def start_run(self, *, agent: str, window_from) -> str:
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
```

- [ ] **Step 7: Implement `run_reflection` as a service function (dispatcher injected)**

This is the shared orchestration the MCP tool AND the CLI (Task 9) both call — write it once, testable with a fake dispatcher, so there is no later "extract" refactor. Add to `src/exocortex/memory/reflect.py` (module-level function; import `build_reflect_goal` lazily to avoid a coordination→memory import cycle):

```python
async def run_reflection(*, audit, store, settings, dispatch,
                         since_days: int | None = None,
                         all_history: bool = False) -> dict:
    """Run one reflection pass. `dispatch` is a callable with the same kwargs
    as DispatchService.dispatch (goal, preferred_agent, from_agent,
    max_wait_seconds) — injected so this is unit-testable without a real agent.
    Records REFLECTION_STARTED/COMPLETED; the dispatched agent proposes insights
    via the insight_propose tool during the run."""
    from exocortex.coordination.reflect_goal import build_reflect_goal
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
```

- [ ] **Step 8: Write the end-to-end test with a fake dispatcher**

This is the ScriptedProcess-equivalent for reflection: a fake `dispatch` that stands in for the real agent by proposing insights (parsing the run id out of the goal, which proves the goal carries it). It exercises window → goal → dispatch → propose → count → complete without a real binary.

```python
# tests/unit/test_reflect_run.py  (append to the file from Step 5)
import re
from exocortex.config import Settings
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.contracts import Confidence, MemoryRecord, MemoryScope
from exocortex.operator.mcp.server import _propose_insight
from exocortex.memory.reflect import run_reflection


@pytest.mark.asyncio
async def test_run_reflection_counts_only_this_run(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    store = DurableMemoryStore(tmp_path / "m.db")
    emb = DeterministicEmbeddingProvider()
    rec = MemoryRecord(type="observation", content="chose SQLite", source="codex",
                       confidence=Confidence.OBSERVED, scope=MemoryScope.PROJECT,
                       scope_id="exocortex")
    await store.write(rec, embedding=emb.embed(rec.content))

    async def fake_dispatch(*, goal, **kwargs):
        rid = re.search(r"Reflection run id: (\S+)", goal).group(1)
        await _propose_insight(audit, kind="synthesis", title="t1", detail="d",
                               refs=[str(rec.id)], reflection_id=rid)
        await _propose_insight(audit, kind="gap", title="t2", detail="d",
                               refs=[str(rec.id)], reflection_id=rid)
        return {"dispatched_to": "codex"}

    settings = Settings(reflect_window_days=7, reflect_max_insights=20)
    out = await run_reflection(audit=audit, store=store, settings=settings,
                               dispatch=fake_dispatch, all_history=True)
    assert out["status"] == "completed"
    assert out["insight_count"] == 2          # only this run's two insights
    completed = [e for e in await audit.read_all()
                 if e.kind == EventKind.REFLECTION_COMPLETED][0]
    assert completed.payload["insight_count"] == 2
```

- [ ] **Step 9: Add the thin `reflect` MCP tool (wraps `run_reflection`)**

In `build_mcp_server` (`server.py`). It only gates on the flag and delegates — no orchestration logic to duplicate later:

```python
    @mcp.tool()
    async def reflect(
        since_days: Annotated[int | None, Field(description="Reflect over the last N days (overrides the default window).")] = None,
        all_history: Annotated[bool, Field(description="Reflect over ALL memory.")] = False,
    ) -> dict[str, Any]:
        """Run one reflection pass: dispatch a reflective agent over recent memory; it proposes insights."""
        if not effective_settings.reflect_enabled:
            return {"status": "disabled", "hint": "set EXOCORTEX_REFLECT_ENABLED=true"}
        from exocortex.memory.reflect import run_reflection
        return await run_reflection(audit=handlers.audit, store=handlers.store,
                                    settings=effective_settings,
                                    dispatch=dispatcher.dispatch,
                                    since_days=since_days, all_history=all_history)
```

- [ ] **Step 10: Register the tool + run tests**

Add `"reflect"` to `EXPECTED_TOOLS` in `tests/unit/test_mcp_server_smoke.py`.
Run: `uv run pytest tests/unit/test_reflect_goal.py tests/unit/test_reflect_run.py tests/unit/test_mcp_server_smoke.py -q`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add src/exocortex/coordination/reflect_goal.py src/exocortex/memory/reflect.py src/exocortex/operator/mcp/server.py tests/unit/test_reflect_goal.py tests/unit/test_reflect_run.py tests/unit/test_mcp_server_smoke.py
git commit -m "feat(reflect): goal builder + run_reflection service + reflect MCP tool"
```

---

### Task 6: Accept / dismiss / apply (the two-step action flow)

**Files:**
- Modify: `src/exocortex/memory/reflect.py`
- Test: `tests/unit/test_reflect_actions.py`

**Interfaces:**
- Consumes: the store (`DurableMemoryStore`), `AuditLog`.
- Produces: `accept(insight_id, *, apply=False, store=None, embedder=None) -> dict`; `dismiss(insight_id, note="")`. `accept(apply=False)` records `INSIGHT_ACCEPTED` and returns the proposed mutation; `accept(apply=True)` also performs it (contradiction → writes a superseding `MemoryRecord`; others in v1 return the drafted payload for the caller to persist) and records the result in the event's `acted` field.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reflect_actions.py
from __future__ import annotations
import uuid
from pathlib import Path
import pytest
from exocortex.contracts import (Confidence, Event, EventKind, MemoryRecord, MemoryScope)
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.observability.audit import AuditLog
from exocortex.memory.reflect import ReflectionService


async def _seed_proposed(audit, iid, action):
    await audit.record(Event(kind=EventKind.INSIGHT_PROPOSED, payload={
        "insight_id": iid, "kind": "contradiction", "title": "t", "detail": "d",
        "refs": [str(uuid.uuid4())], "suggested_action": action}))


@pytest.mark.asyncio
async def test_accept_without_apply_is_inert(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    await _seed_proposed(audit, "i1", {"type": "none"})
    out = await svc.accept("i1", apply=False)
    assert out["status"] == "accepted" and out["applied"] is False
    kinds = [e.kind for e in await audit.read_all()]
    assert EventKind.INSIGHT_ACCEPTED in kinds
    # no memory record written
    assert await DurableMemoryStore(tmp_path / "m.db").count() == 0


@pytest.mark.asyncio
async def test_apply_contradiction_supersedes_never_deletes(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    store = DurableMemoryStore(tmp_path / "m.db")
    emb = DeterministicEmbeddingProvider()
    stale = MemoryRecord(type="observation", content="Vietnam", source="codex",
                         confidence=Confidence.OBSERVED, scope=MemoryScope.PROJECT,
                         scope_id="exocortex")
    await store.write(stale, embedding=emb.embed(stale.content))
    svc = ReflectionService(audit=audit)
    await _seed_proposed(audit, "i2",
                         {"type": "supersede", "stale_record_id": str(stale.id)})
    out = await svc.accept("i2", apply=True, store=store, embedder=emb)
    assert out["applied"] is True
    assert await store.get(stale.id) is not None       # original NOT deleted
    assert await store.count() == 2                     # a superseding record added


@pytest.mark.asyncio
async def test_dismiss(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    svc = ReflectionService(audit=audit)
    await _seed_proposed(audit, "i3", {"type": "none"})
    await svc.dismiss("i3", note="not useful")
    assert [i["insight_id"] for i in await svc.list_insights()] == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_reflect_actions.py -q`
Expected: FAIL — `AttributeError: ... 'accept'`.

- [ ] **Step 3: Implement accept/dismiss**

Add to `ReflectionService` (import `Confidence`, `MemoryRecord`, `MemoryScope`):

```python
    async def _find_proposed(self, insight_id: str) -> dict | None:
        for ev in await self.audit.read_all():
            if ev.kind == EventKind.INSIGHT_PROPOSED and \
               (ev.payload or {}).get("insight_id") == insight_id:
                return dict(ev.payload)
        return None

    async def dismiss(self, insight_id: str, *, note: str = "") -> dict:
        await self.audit.record(Event(kind=EventKind.INSIGHT_DISMISSED, actor="operator",
                                      payload={"insight_id": insight_id, "note": note}))
        return {"insight_id": insight_id, "status": "dismissed"}

    async def accept(self, insight_id: str, *, apply: bool = False,
                     store=None, embedder=None) -> dict:
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

    async def _apply_action(self, payload, action, store, embedder) -> dict:
        atype = action.get("type", "none")
        if atype == "supersede" and store is not None and embedder is not None:
            from uuid import UUID
            stale_id = action.get("stale_record_id")
            rec = MemoryRecord(
                type="correction",
                content=f"Supersedes {stale_id}: {payload.get('title', '')} — {payload.get('detail', '')}",
                source="reflect", confidence=Confidence.INFERRED,
                scope=MemoryScope.PROJECT, scope_id="exocortex",
                tags=["supersedes:" + str(stale_id)])
            await store.write(rec, embedding=embedder.embed(rec.content))
            return {"superseded_by": str(rec.id), "stale_record_id": stale_id}
        # create_rule / track_gap / record_decision: v1 returns the drafted payload
        # for the caller (CLI/web) to persist on explicit confirm.
        return {"drafted": action}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_reflect_actions.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/exocortex/memory/reflect.py tests/unit/test_reflect_actions.py
git commit -m "feat(reflect): accept(propose)/apply/dismiss with supersede-never-delete"
```

---

### Task 7: Humanize the new event kinds

**Files:**
- Modify: `src/exocortex/observability/humanize.py`
- Test: `tests/unit/test_humanize.py` (extend)

**Interfaces:**
- Consumes: `_FORMATTERS` dict in `humanize.py`.

- [ ] **Step 1: Write the failing test (append)**

```python
def test_reflect_event_sentences() -> None:
    from exocortex.contracts import Event, EventKind
    from exocortex.observability.humanize import humanize_event
    assert "contradiction" in humanize_event(Event(
        kind=EventKind.INSIGHT_PROPOSED,
        payload={"kind": "contradiction", "title": "X conflicts with Y"}))
    assert "3 insights" in humanize_event(Event(
        kind=EventKind.REFLECTION_COMPLETED,
        payload={"status": "completed", "insight_count": 3}))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_humanize.py::test_reflect_event_sentences -q`
Expected: FAIL.

- [ ] **Step 3: Add formatters**

In `_FORMATTERS` in `humanize.py` add entries:

```python
    EventKind.REFLECTION_STARTED: lambda p: "reflection started",
    EventKind.REFLECTION_COMPLETED: lambda p: f"reflection {p.get('status', '?')} "
    f"({p.get('insight_count', 0)} insights)",
    EventKind.INSIGHT_PROPOSED: lambda p: f"[{p.get('kind', '?')}] "
    + _short(p.get("title") or "", 80),
    EventKind.INSIGHT_ACCEPTED: lambda p: "insight accepted"
    + (" + applied" if (p.get("acted") or {}).get("superseded_by") else ""),
    EventKind.INSIGHT_DISMISSED: lambda p: "insight dismissed",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_humanize.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/exocortex/observability/humanize.py tests/unit/test_humanize.py
git commit -m "feat(reflect): human-readable sentences for reflection/insight events"
```

---

### Task 8: `session_startup` surfaces pending insights

**Files:**
- Modify: `src/exocortex/operator/mcp/handlers.py`
- Test: `tests/unit/test_mcp_handlers.py` (extend) or `tests/unit/test_session_memory.py`

**Interfaces:**
- Consumes: `ReflectionService.list_insights` (Task 2), `self.audit`.
- Produces: `session_startup(...)` result dict gains `pending_insights: {"count": int, "top": [ {insight_id,kind,title} ] }`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_session_insights.py
from __future__ import annotations
import uuid
from pathlib import Path
import pytest
from exocortex.contracts import Event, EventKind
from exocortex.observability.audit import AuditLog

# Build handlers the same way tests/unit/test_mcp_handlers.py does; assume a
# fixture `handlers` exists there. Minimal standalone version:
from exocortex.config import Settings
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.retrieval import HybridRetrieval
from exocortex.operator.mcp.handlers import MemoryHandlers


def _handlers(tmp_path: Path) -> MemoryHandlers:
    s = Settings(data_dir=tmp_path, audit_log_path=tmp_path / "a.jsonl",
                 memory_db_path=tmp_path / "m.db")
    store = DurableMemoryStore(s.memory_db_path)
    emb = DeterministicEmbeddingProvider()
    return MemoryHandlers(store=store, embedder=emb,
                          retrieval=HybridRetrieval(store, emb),
                          audit=AuditLog(s.audit_log_path), settings=s)


@pytest.mark.asyncio
async def test_session_startup_includes_pending_insights(tmp_path: Path) -> None:
    h = _handlers(tmp_path)
    await h.audit.record(Event(kind=EventKind.INSIGHT_PROPOSED, payload={
        "insight_id": str(uuid.uuid4()), "kind": "gap", "title": "unanswered X",
        "detail": "d", "refs": [str(uuid.uuid4())]}))
    result = await h.session_startup(agent_id="codex")
    assert result["pending_insights"]["count"] == 1
    assert result["pending_insights"]["top"][0]["title"] == "unanswered X"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_session_insights.py -q`
Expected: FAIL — `KeyError: 'pending_insights'`.

- [ ] **Step 3: Implement**

In `handlers.py`, `session_startup` builds `out = summary.to_dict()`, sets `out["profile_voice"] = voice`, and ends with `return out`. Insert this immediately **before `return out`** (so it sits alongside `profile_voice`):

```python
        from exocortex.memory.reflect import ReflectionService
        _pending = await ReflectionService(audit=self.audit).list_insights()
        out["pending_insights"] = {
            "count": len(_pending),
            "top": [{"insight_id": i["insight_id"], "kind": i.get("kind"),
                     "title": i.get("title")} for i in _pending[:5]],
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_session_insights.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/exocortex/operator/mcp/handlers.py tests/unit/test_session_insights.py
git commit -m "feat(reflect): surface pending insights in session_startup"
```

---

### Task 9: CLI — `precog reflect` and `precog insights`

**Files:**
- Modify: `src/exocortex/operator/cli.py`
- Test: `tests/unit/test_cli_extended.py` (extend)

**Interfaces:**
- Consumes: `ReflectionService` (list/accept/dismiss), `run_reflection` (Task 5 — the shared service function), `DispatchService`, `render`/`humanize` for display.
- Produces: `precog insights` (list open), `precog insights show <id>`, `precog insights accept <id> [--apply]`, `precog insights dismiss <id>`, `precog reflect [--since N] [--all]`.

- [ ] **Step 1: Write the failing test (list path, no dispatch)**

```python
# tests/unit/test_cli_reflect.py
from __future__ import annotations
import uuid
from pathlib import Path
import pytest
from typer.testing import CliRunner
from exocortex.contracts import Event, EventKind
from exocortex.observability.audit import AuditLog
from exocortex.operator.cli import app

runner = CliRunner()


def test_insights_list_and_dismiss(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOCORTEX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EXOCORTEX_AUDIT_LOG_PATH", str(tmp_path / "a.jsonl"))
    monkeypatch.setenv("EXOCORTEX_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import anyio
    iid = str(uuid.uuid4())
    anyio.run(AuditLog(tmp_path / "a.jsonl").record,
              Event(kind=EventKind.INSIGHT_PROPOSED, payload={
                  "insight_id": iid, "kind": "gap", "title": "unanswered X",
                  "detail": "d", "refs": [str(uuid.uuid4())]}))
    r = runner.invoke(app, ["insights"])
    assert r.exit_code == 0 and "unanswered X" in r.stdout
    r2 = runner.invoke(app, ["insights", "dismiss", iid])
    assert r2.exit_code == 0
    r3 = runner.invoke(app, ["insights"])
    assert "unanswered X" not in r3.stdout
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_cli_reflect.py -q`
Expected: FAIL — no `insights` command.

- [ ] **Step 3: Implement the CLI**

In `cli.py`, follow the existing `precog memory` sub-typer pattern. Add an `insights` Typer group with `list` (default), `show`, `accept` (`--apply/--no-apply`), `dismiss`, each building `AuditLog(settings.audit_log_path)` + `ReflectionService` and running via `asyncio.run`/`anyio.run` like the other commands. Add `reflect` as a top-level command that calls the **already-shared** `run_reflection` from Task 5 (`from exocortex.memory.reflect import run_reflection`), constructing a `DispatchService(settings=settings)` and passing `dispatch=dispatcher.dispatch` — no new orchestration, no extraction. Display insights with `render`/`humanize`. (On `accept` without `--apply` show the drafted action; with `--apply` perform it, passing `store` + `DeterministicEmbeddingProvider()`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli_reflect.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/exocortex/operator/cli.py src/exocortex/memory/reflect.py tests/unit/test_cli_reflect.py
git commit -m "feat(reflect): precog reflect + precog insights CLI"
```

---

### Task 10: Web — `/reflect` page + API (projection)

**Files:**
- Modify: `src/exocortex/operator/web/routes.py`, `src/exocortex/operator/web/server.py`
- Create: `src/exocortex/operator/web/static/reflect.html`, `static/reflect.js`
- Modify: each `static/*.html` nav include (add a `reflect` link)
- Test: `tests/unit/test_web_routes.py` (extend)

**Interfaces:**
- Consumes: `ReflectionService`; the shared `Exo` front-end core (`common.js`) and the local-guard middleware.
- Produces: `GET /api/insights` (list, projection), `POST /api/insights/{id}/accept`, `POST /api/insights/{id}/act`, `POST /api/insights/{id}/dismiss`; a `/reflect` page route.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_web_routes.py
def test_insights_api(web_env: tuple[Path, Path]) -> None:
    import anyio, uuid
    from exocortex.contracts import Event, EventKind
    from exocortex.observability.audit import AuditLog
    audit_path, _ = web_env
    iid = str(uuid.uuid4())
    anyio.run(AuditLog(audit_path).record,
              Event(kind=EventKind.INSIGHT_PROPOSED, payload={
                  "insight_id": iid, "kind": "synthesis", "title": "weekly recap",
                  "detail": "d", "refs": [str(uuid.uuid4())]}))
    with _client(web_env) as client:
        r = client.get("/api/insights")
        assert r.status_code == 200
        assert any(i["insight_id"] == iid for i in r.json()["items"])
        d = client.post(f"/api/insights/{iid}/dismiss", json={})
        assert d.status_code == 200
        assert client.get("/api/insights").json()["items"] == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_web_routes.py::test_insights_api -q`
Expected: FAIL — 404.

- [ ] **Step 3: Implement the routes**

In `routes.py` (inside `build_router`, using the injected `audit`), add:

`DeterministicEmbeddingProvider` is already imported in `routes.py` (used elsewhere); reuse it. No unused params (they trip ruff).

```python
    from exocortex.memory.reflect import ReflectionService

    @router.get("/api/insights")
    async def list_insights(include_resolved: bool = False) -> dict[str, Any]:
        svc = ReflectionService(audit=audit)
        return {"items": await svc.list_insights(include_resolved=include_resolved)}

    @router.post("/api/insights/{insight_id}/dismiss")
    async def dismiss_insight(insight_id: str) -> dict[str, Any]:
        return await ReflectionService(audit=audit).dismiss(insight_id)

    @router.post("/api/insights/{insight_id}/accept")
    async def accept_insight(insight_id: str) -> dict[str, Any]:
        return await ReflectionService(audit=audit).accept(insight_id, apply=False)

    @router.post("/api/insights/{insight_id}/act")
    async def act_insight(insight_id: str) -> dict[str, Any]:
        return await ReflectionService(audit=audit).accept(
            insight_id, apply=True, store=store, embedder=DeterministicEmbeddingProvider())
```

(If `DeterministicEmbeddingProvider` is *not* already imported in `routes.py`, add `from exocortex.memory.embedding import DeterministicEmbeddingProvider` — verify before running.)

Add the `/reflect` page route in `server.py` mirroring the other `FileResponse` pages, and a `reflect.html` + `reflect.js` that fetch `/api/insights` and render cards grouped by kind with accept/dismiss buttons (reuse `Exo.fetchJSON`, `Exo.el`, `Exo.agentColor`, `Exo.connectWs("/api/events", …)` to refetch on `INSIGHT_*`/`REFLECTION_*` events). Add a `reflect` nav link to each page's header.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_web_routes.py -q`
Expected: PASS.

- [ ] **Step 5: Verify the page live (per project practice)**

Start a clean instance (`uv run precog serve --port 8758`), open `/reflect` in the browser, seed an insight, and confirm: cards render grouped by kind, dismiss removes a card, and the WS refetches on new insights. (Frontend rendering isn't covered by the Python tests — verify it in a browser as done for Phase 3.)

- [ ] **Step 6: Commit**

```bash
git add src/exocortex/operator/web/ tests/unit/test_web_routes.py
git commit -m "feat(reflect): /reflect page + insights API (projection over events)"
```

---

### Task 11: Full-suite green + lint/type + docs

**Files:**
- Modify: `CHANGELOG.md`, `README.md` (CLI + pages tables), `.env.example`, `docs/roadmap.md` (mark the reflective-agent item shipped)

- [ ] **Step 1: Run the whole suite + lint + type**

```bash
uv run pytest -q && uv run ruff check src tests && uv run mypy src
```
Expected: all pass, ruff/mypy clean. Fix any regressions before continuing.

- [ ] **Step 2: Document**

Add a `## [Unreleased] — Reflect` CHANGELOG section; add `precog reflect` / `precog insights` to the README CLI list and `/reflect` to the pages table; document the `EXOCORTEX_REFLECT_*` vars in `.env.example`; tick the reflective-agent item in `docs/roadmap.md`.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "docs(reflect): changelog, README, .env.example, roadmap"
```

---

## Self-Review

**1. Spec coverage:**
- Insight model + kinds + grounding → Task 1. Event kinds → Task 1. ✓
- Reflective dispatch, window, goal, `insight_propose` → Tasks 3, 4, 5. ✓
- Reviewable queue projection → Task 2. ✓
- Accept/dismiss + two-step apply (supersede-never-delete; rule/gap/decision drafted-then-confirm) → Task 6 (+ CLI/web confirm in 9/10). ✓
- Surfacing: session_startup (Task 8), `/reflect` + API (Task 10), CLI (Task 9). ✓
- Humanize sentences → Task 7. ✓
- Config (off by default, caps) → Task 4. ✓
- Error handling (failed run keeps proposed insights; empty refs rejected; no-new-memory = zero insights) → Tasks 4, 5, 6. ✓
- Testing via ScriptedProcess / projection → throughout; the reflective run is now exercised **end-to-end** by Task 5 Step 8 (a fake dispatcher stands in for the agent, proposes insights, and the run's count/complete are asserted).

---

## Reviewer pass (Fable 5) — gaps found and fixed

Verified the plan's code claims against the live codebase and corrected these before handing to subagents:

1. **Suite-red blocker:** `test_mcp_server_smoke.py` asserts `names == EXPECTED_TOOLS` (exact set). Adding `insight_propose` / `reflect` without updating that set turns the suite red. → Added explicit `EXPECTED_TOOLS` update steps to Tasks 4 and 5, plus a Global Constraint.
2. **Refactor churn removed (subagent-critical):** the reflect orchestration was written inline in `server.py` (Task 5) then "extracted" in Task 9. → Restructured so `run_reflection()` is a tested service function in `reflect.py` from Task 5; the MCP tool and CLI both call it. No task rewrites another task's code.
3. **Correctness bug:** `REFLECTION_COMPLETED.insight_count` counted *all* insights ever, not this run's. → Added `count_for_run(reflection_id)` filtering by run.
4. **Missing end-to-end test:** the orchestration had no test. → Added a fake-dispatcher test (Task 5 Step 8).
5. **Factual:** header said "Python 3.9+"; the repo is `>=3.12` (uses `StrEnum`, `X | None`). → Corrected.
6. **Lint gate:** Task 3's test imported unused `datetime`/`timezone`; Task 10's dismiss route had an unused `request: Request`. → Removed (the suite lints `tests/`).
7. **Zero-context precision:** Task 8's injection point is now exact (`out["pending_insights"] = …` before `return out`, alongside `profile_voice`).

**Verified-correct claims (no change needed):** `MemoryRecord.tags` exists; `handlers.store/.embedder/.retrieval/.audit` all present; `all_with_embeddings()` returns `(record, embedding)` pairs; `now()` is tz-aware (UTC) so window arithmetic is sound; `build_router` receives `store`; Pydantic v2 `Field(min_length=1)` validates non-empty lists.

**Residual risks (accept for v1, worth a note):** `run_reflection` loads all embeddings via `all_with_embeddings()` then filters by timestamp — fine under the window cap, but a `list_since(ts)` store method would avoid loading vectors; and reflective-agent compliance with `insight_propose` (vs free-texting) remains the key real-world unknown, now testable via the fake-dispatcher harness before any real binary runs.

**2. Placeholder scan:** Task 9 step 3 and Task 10 step 3 describe UI/CLI wiring rather than full literal code, because they must follow existing per-file patterns (the `precog memory` sub-typer; the per-page nav include) that a literal block would misrepresent — the interfaces and behaviors are fully specified, and the reviewer should mirror the cited existing patterns. Every backend task has complete code.

**3. Type consistency:** `insight_id` is the event-payload key throughout (Insight's `id` is renamed to `insight_id` in `_propose_insight` and read under that key by the projection, session_startup, CLI, and web). `suggested_action` shape (`type` + optional fields) is consistent across the contract, `_propose_insight`, and `_apply_action`. `ReflectionService(audit=...)` constructor is used identically everywhere. `list_insights(include_resolved=...)` signature matches all call sites.
