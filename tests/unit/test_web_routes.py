"""REST-route tests for the operator web UI.

We use fastapi.testclient.TestClient against a fresh app built on a temp audit
log + temp memory DB. No real agents are involved — the routes are pure lenses
over the stores, so unit-scope fixtures are sufficient.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from exocortex.config import Settings
from exocortex.contracts import (
    Confidence,
    Event,
    EventKind,
    MemoryRecord,
    MemoryScope,
)
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.observability.audit import AuditLog
from exocortex.operator.web.server import create_app


@pytest.fixture
def web_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    audit_path = data / "audit.jsonl"
    memory_path = data / "memory.db"
    monkeypatch.setenv("EXOCORTEX_DATA_DIR", str(data))
    monkeypatch.setenv("EXOCORTEX_AUDIT_LOG_PATH", str(audit_path))
    monkeypatch.setenv("EXOCORTEX_MEMORY_DB_PATH", str(memory_path))
    return audit_path, memory_path


def _seed_events(audit_path: Path, task_id: UUID) -> None:
    async def _go() -> None:
        audit = AuditLog(audit_path)
        await audit.record(
            Event(
                kind=EventKind.TASK_CREATED,
                task_id=task_id,
                payload={"goal": "Refactor the auth middleware"},
            )
        )
        await audit.record(
            Event(
                kind=EventKind.SESSION_OPENED,
                task_id=task_id,
                agent_id="codex",
            )
        )
        await audit.record(
            Event(
                kind=EventKind.TOOL_PROPOSED,
                task_id=task_id,
                agent_id="codex",
                payload={"tool": "fs.read", "argv": ["fs.read", "auth.py"]},
            )
        )
        await audit.record(
            Event(
                kind=EventKind.HANDOFF_INITIATED,
                task_id=task_id,
                agent_id="codex",
                payload={"to_agent": "claude_code"},
            )
        )
        await audit.record(
            Event(
                kind=EventKind.TASK_STATUS_CHANGED,
                task_id=task_id,
                payload={"from": "proposed", "to": "in_progress"},
            )
        )

    asyncio.run(_go())


def _seed_memory(memory_path: Path, count: int = 5) -> list[MemoryRecord]:
    async def _go() -> list[MemoryRecord]:
        store = DurableMemoryStore(memory_path)
        emb = DeterministicEmbeddingProvider()
        out: list[MemoryRecord] = []
        for i in range(count):
            rec = MemoryRecord(
                type="observation",
                content=f"record number {i} about the auth flow",
                source=["codex", "claude_code", "hermes", "operator"][i % 4],
                confidence=Confidence.OBSERVED,
                scope=MemoryScope.TASK,
                scope_id="task-1",
            )
            await store.write(rec, embedding=emb.embed(rec.content))
            out.append(rec)
        await store.close()
        return out

    return asyncio.run(_go())


def _client(web_env: tuple[Path, Path]) -> TestClient:
    # Fresh Settings each test (picks up env overrides)
    app = create_app(Settings())
    # Note: TestClient runs lifespan on __enter__.
    return TestClient(app)


def test_status_returns_counts(web_env: tuple[Path, Path]) -> None:
    audit_path, memory_path = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_events(audit_path, uuid4())
    _seed_memory(memory_path, count=3)

    with _client(web_env) as client:
        r = client.get("/api/status")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["tasks"] == 1
        assert body["memory_records"] == 3
        assert body["bridges_registered"] == 3
        assert body["events_total"] >= 5


def test_tasks_list(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    t1 = uuid4()
    t2 = uuid4()
    _seed_events(audit_path, t1)
    _seed_events(audit_path, t2)

    with _client(web_env) as client:
        r = client.get("/api/tasks?limit=10")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 2
        ids = {t["id"] for t in body["tasks"]}
        assert ids == {str(t1), str(t2)}
        for t in body["tasks"]:
            assert t["status"] == "in_progress"
            assert t["goal"] == "Refactor the auth middleware"
            assert "codex" in t["agents"]


def test_task_trace_by_prefix(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    t1 = uuid4()
    _seed_events(audit_path, t1)

    with _client(web_env) as client:
        r = client.get(f"/api/tasks/{str(t1)[:8]}/trace")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["task_id"] == str(t1)
        assert body["count"] == 5
        kinds = [e["kind"] for e in body["events"]]
        assert "task.created" in kinds
        assert "handoff.initiated" in kinds


def test_task_trace_not_found(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/api/tasks/deadbeef/trace")
        assert r.status_code == 404


def test_memory_records_endpoint(web_env: tuple[Path, Path]) -> None:
    _, memory_path = web_env
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_memory(memory_path, count=4)

    with _client(web_env) as client:
        r = client.get("/api/memory/records?scope=task&scope_id=task-1")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 4
        contents = [rec["content"] for rec in body["records"]]
        assert any("record number 0" in c for c in contents)


def test_memory_records_invalid_scope(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/api/memory/records?scope=nope&scope_id=x")
        assert r.status_code == 400


def test_memory_records_scope_requires_scope_id(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/api/memory/records?scope=task")
        assert r.status_code == 400


def test_memory_search(web_env: tuple[Path, Path]) -> None:
    _, memory_path = web_env
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_memory(memory_path, count=5)

    with _client(web_env) as client:
        r = client.get("/api/memory/search?q=auth&alpha=1.0&limit=5")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["query"] == "auth"
        assert body["count"] > 0
        for hit in body["hits"]:
            assert "score" in hit and "record" in hit


def test_memory_constellation(web_env: tuple[Path, Path]) -> None:
    _, memory_path = web_env
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_memory(memory_path, count=6)

    with _client(web_env) as client:
        r = client.get("/api/memory/constellation")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 6
        for point in body["points"]:
            assert "id" in point
            assert "x" in point and "y" in point
            assert "scope" in point and "source" in point


def test_memory_constellation_empty(web_env: tuple[Path, Path]) -> None:
    # Don't seed; empty DB path will be created by app factory.
    with _client(web_env) as client:
        r = client.get("/api/memory/constellation")
        assert r.status_code == 200
        assert r.json() == {"count": 0, "points": []}


def test_agents_endpoint(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/api/agents")
        assert r.status_code == 200, r.text
        body = r.json()
        # Empty audit → only the 3 registered bridges show up.
        assert body["count"] == 3
        ids = {a["id"] for a in body["agents"]}
        assert ids == {"codex", "claude_code", "hermes"}
        for a in body["agents"]:
            assert isinstance(a["capabilities"], list)
            assert "recently_active" in a
            # New stat fields default to 0 when no events.
            assert a["total_events"] == 0
            assert a["memory_writes"] == 0
            assert a["tool_invocations"] == 0
            assert a["dispatches"] == 0
            assert a["chat_queries"] == 0


def test_agents_endpoint_aggregates_stats(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_events(audit_path, uuid4())  # codex: 1 session_opened + 1 tool_proposed + 1 handoff

    with _client(web_env) as client:
        r = client.get("/api/agents")
        body = r.json()
        codex = next(a for a in body["agents"] if a["id"] == "codex")
        assert codex["total_events"] >= 3
        assert codex["tool_invocations"] >= 1
        assert codex["last_active_at"] is not None
        assert codex["first_seen_at"] is not None


def test_tasks_filter_by_status(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_events(audit_path, uuid4())  # ends in_progress → "open"

    with _client(web_env) as client:
        r_open = client.get("/api/tasks?status=open")
        assert r_open.status_code == 200
        assert r_open.json()["count"] == 1

        r_done = client.get("/api/tasks?status=completed")
        assert r_done.status_code == 200
        assert r_done.json()["count"] == 0


def test_tasks_includes_last_decision(web_env: tuple[Path, Path]) -> None:
    audit_path, memory_path = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.parent.mkdir(parents=True, exist_ok=True)

    t1 = uuid4()
    _seed_events(audit_path, t1)
    # Override scope_id of seeded memory to match task.
    async def _seed_decision() -> None:
        store = DurableMemoryStore(memory_path)
        emb = DeterministicEmbeddingProvider()
        rec = MemoryRecord(
            type="decision.architecture",
            content="picked SQLite over Postgres for the MVP",
            source="codex",
            confidence=Confidence.OBSERVED,
            scope=MemoryScope.TASK,
            scope_id=str(t1),
        )
        await store.write(rec, embedding=emb.embed(rec.content))
        await store.close()

    asyncio.run(_seed_decision())

    with _client(web_env) as client:
        r = client.get("/api/tasks")
        body = r.json()
        match = next(t for t in body["tasks"] if t["id"] == str(t1))
        assert "SQLite" in match["last_decision"]
        assert match["scope"] == "task"
        assert match["scope_id"] == str(t1)


def test_activity_feed_returns_recent_events(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_events(audit_path, uuid4())

    with _client(web_env) as client:
        r = client.get("/api/activity?limit=20")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] >= 5
        # Newest first.
        ts = [item["timestamp_ms"] for item in body["items"]]
        assert ts == sorted(ts, reverse=True)
        # Each item has the shape the UI expects.
        for item in body["items"]:
            assert "event_id" in item
            assert "kind" in item
            assert "agent_id" in item
            assert "payload_preview" in item


def test_agent_history_filters_by_agent(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_events(audit_path, uuid4())

    with _client(web_env) as client:
        r = client.get("/api/agents/codex/history?limit=50")
        assert r.status_code == 200
        body = r.json()
        assert body["agent_id"] == "codex"
        assert all(item["agent_id"] == "codex" for item in body["items"])
        # Should include the tool_proposed and handoff events.
        kinds = {item["kind"] for item in body["items"]}
        assert "tool.proposed" in kinds
        assert "handoff.initiated" in kinds


def test_agent_history_kind_filter(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_events(audit_path, uuid4())

    with _client(web_env) as client:
        r = client.get("/api/agents/codex/history?kind=tool.proposed")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["items"][0]["kind"] == "tool.proposed"


def test_agent_event_context_returns_preceding(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    t1 = uuid4()
    _seed_events(audit_path, t1)

    with _client(web_env) as client:
        history = client.get("/api/agents/codex/history").json()
        # Pick the latest codex event (handoff.initiated, since task_status is unagent'd).
        target = next(i for i in history["items"] if i["kind"] == "handoff.initiated")
        r = client.get(
            f"/api/agents/codex/context/{target['event_id']}"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["event"]["event_id"] == target["event_id"]
        # Preceding context exists (events earlier in same task).
        assert len(body["preceding"]) >= 1
        # Each preceding event is strictly earlier.
        target_ts = target["timestamp"]
        for prev in body["preceding"]:
            assert prev["timestamp"] < target_ts


def test_agent_event_context_404_on_unknown(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get(f"/api/agents/codex/context/{uuid4()}")
        assert r.status_code == 404


def test_index_page_served(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "Exocortex" in r.text


def test_memory_page_served(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/memory")
        assert r.status_code == 200
        assert "constellation" in r.text.lower()


def test_agents_page_served(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/agents")
        assert r.status_code == 200


def test_chat_page_served(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/chat")
        assert r.status_code == 200


def test_profile_page_served(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/profile")
        assert r.status_code == 200


def test_profile_get_returns_sections(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/api/profile")
        assert r.status_code == 200
        body = r.json()
        assert body["frozen"] is False
        assert body["user_id"] == "operator"
        # Eight canonical sections, all empty.
        types = [s["type"] for s in body["sections"]]
        assert "profile.preference" in types
        assert "profile.communication_style" in types


def test_profile_freeze_toggle_round_trip(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        s1 = client.get("/api/settings/profile_freeze").json()
        assert s1["frozen"] is False
        t1 = client.post("/api/settings/profile_freeze/toggle").json()
        assert t1["frozen"] is True
        s2 = client.get("/api/settings/profile_freeze").json()
        assert s2["frozen"] is True
        client.post("/api/settings/profile_freeze/toggle")
        s3 = client.get("/api/settings/profile_freeze").json()
        assert s3["frozen"] is False


def test_profile_seed_and_answer_flow(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        # Seed questions from gaps (empty profile → all dimensions are gaps).
        r = client.post("/api/profile/seed_questions")
        assert r.status_code == 200
        added = r.json()["added"]
        assert len(added) >= 1

        # Confirm questions are listed.
        q = client.get("/api/profile/questions").json()
        assert q["count"] >= 1
        first = q["items"][0]

        # Answer the first question.
        ans = client.post(
            "/api/profile/answer",
            json={"question_id": first["id"], "answer": "I prefer terse output, no fluff."},
        )
        assert ans.status_code == 200
        new_id = ans.json()["new_record_id"]
        assert new_id

        # Question is now answered, not in open queue.
        q2 = client.get("/api/profile/questions?status=open").json()
        ids_open = {it["id"] for it in q2["items"]}
        assert first["id"] not in ids_open

        # New record landed in the right section.
        prof = client.get("/api/profile").json()
        sect = next(s for s in prof["sections"] if s["type"] == first["dimension"])
        assert sect["count"] >= 1


def test_profile_redact(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        # Seed + answer to create a record.
        client.post("/api/profile/seed_questions")
        q = client.get("/api/profile/questions").json()
        first = q["items"][0]
        ans = client.post(
            "/api/profile/answer",
            json={"question_id": first["id"], "answer": "test answer"},
        )
        new_id = ans.json()["new_record_id"]

        r = client.post("/api/profile/redact", json={"record_id": new_id})
        assert r.status_code == 200
        assert r.json()["status"] == "redacted"

        # Confirm gone.
        prof = client.get("/api/profile").json()
        sect = next(s for s in prof["sections"] if s["type"] == first["dimension"])
        ids = {item["id"] for item in sect["items"]}
        assert new_id not in ids


def test_profile_redact_refuses_non_user_scope(web_env: tuple[Path, Path]) -> None:
    _, memory_path = web_env
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    recs = _seed_memory(memory_path, count=1)
    with _client(web_env) as client:
        r = client.post("/api/profile/redact", json={"record_id": str(recs[0].id)})
        assert r.status_code == 400
        assert "USER-scope" in r.json()["detail"]


def test_debug_page_served(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/debug")
        assert r.status_code == 200


def test_dashboard_attention_empty_clear(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/api/dashboard/attention")
        assert r.status_code == 200
        body = r.json()
        # Empty audit + memory_chat OFF → no attention items.
        assert body["count"] == 0
        assert body["items"] == []


def test_dashboard_attention_surfaces_dispatch_failed(
    web_env: tuple[Path, Path],
) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    async def _seed() -> None:
        a = AuditLog(audit_path)
        await a.record(
            Event(
                kind=EventKind.DISPATCH_FAILED,
                agent_id="exocortex",
                payload={
                    "reason": "no_bridges_registered",
                    "preferred_agent": None,
                    "goal_preview": "test goal",
                    "detail": "neither codex nor hermes installed",
                },
            )
        )

    asyncio.run(_seed())
    with _client(web_env) as client:
        r = client.get("/api/dashboard/attention")
        body = r.json()
        assert body["count"] == 1
        item = body["items"][0]
        assert item["kind"] == "dispatch_failed"
        assert item["severity"] == "high"
        assert "no_bridges_registered" in item["title"]
        assert item["action_url"].startswith("/debug?event=")


def test_dashboard_growth_returns_zero_state(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/api/dashboard/growth")
        assert r.status_code == 200
        body = r.json()
        assert body["records_today"] == 0
        assert body["records_week"] == 0
        assert body["chat_queries_today"] == 0
        assert body["profile_questions_open"] == 0
        assert body["top_tags"] == []


def test_debug_failures_lists_dispatch_failed(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    async def _seed() -> None:
        a = AuditLog(audit_path)
        for reason in ("no_bridges_registered", "preferred_agent_not_registered"):
            await a.record(
                Event(
                    kind=EventKind.DISPATCH_FAILED,
                    agent_id="exocortex",
                    payload={"reason": reason, "goal_preview": "x"},
                )
            )

    asyncio.run(_seed())
    with _client(web_env) as client:
        r = client.get("/api/debug/failures")
        body = r.json()
        assert body["count"] == 2
        assert body["counts_by_kind"]["dispatch.failed"] == 2
        for item in body["items"]:
            assert item["severity"] == "high"
            assert item["kind"] == "dispatch.failed"


def test_debug_failure_context_returns_hints(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    target_id: list[str] = []

    async def _seed() -> None:
        a = AuditLog(audit_path)
        ev = Event(
            kind=EventKind.DISPATCH_FAILED,
            agent_id="exocortex",
            payload={
                "reason": "no_fallback_for_unsupported_agent",
                "preferred_agent": "claude_code",
                "goal_preview": "x",
            },
        )
        await a.record(ev)
        target_id.append(str(ev.id))

    asyncio.run(_seed())
    with _client(web_env) as client:
        r = client.get(f"/api/debug/failures/{target_id[0]}/context")
        assert r.status_code == 200
        body = r.json()
        assert body["event"]["kind"] == "dispatch.failed"
        # The hint must mention claude_code's missing bridge.
        hints_joined = " ".join(body["hints"]).lower()
        assert "claude_code" in hints_joined or "bridge" in hints_joined


def test_debug_failure_context_404_on_unknown(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get(f"/api/debug/failures/{uuid4()}/context")
        assert r.status_code == 404


def _seed_handoff_chain(
    audit_path: Path,
    *,
    root_task: UUID,
    child_task: UUID,
    grandchild_task: UUID,
) -> None:
    """3-task chain: root → child → grandchild, each with a different agent."""
    async def _go() -> None:
        a = AuditLog(audit_path)
        # Root
        await a.record(
            Event(
                kind=EventKind.TASK_CREATED,
                task_id=root_task,
                agent_id="hermes",
                payload={"goal": "build the auth refactor", "constraints": []},
            )
        )
        await a.record(
            Event(
                kind=EventKind.HANDOFF_INITIATED,
                task_id=root_task,
                agent_id="exocortex",
                payload={
                    "to_agent": "hermes",
                    "child_task_id": str(root_task),
                    "parent_task_id": None,
                    "goal_preview": "build the auth refactor",
                },
            )
        )
        # Child (hermes → codex)
        await a.record(
            Event(
                kind=EventKind.TASK_CREATED,
                task_id=child_task,
                agent_id="codex",
                payload={"goal": "scaffold the new module", "constraints": []},
            )
        )
        await a.record(
            Event(
                kind=EventKind.HANDOFF_INITIATED,
                task_id=child_task,
                agent_id="exocortex",
                payload={
                    "to_agent": "codex",
                    "child_task_id": str(child_task),
                    "parent_task_id": str(root_task),
                    "goal_preview": "scaffold the new module",
                },
            )
        )
        # Grandchild (codex → claude_code, but auto-fallback to hermes)
        await a.record(
            Event(
                kind=EventKind.TASK_CREATED,
                task_id=grandchild_task,
                agent_id="hermes",
                payload={"goal": "review the scaffolding", "constraints": []},
            )
        )
        await a.record(
            Event(
                kind=EventKind.HANDOFF_INITIATED,
                task_id=grandchild_task,
                agent_id="exocortex",
                payload={
                    "to_agent": "hermes",
                    "child_task_id": str(grandchild_task),
                    "parent_task_id": str(child_task),
                    "goal_preview": "review the scaffolding",
                    "fallback_used": True,
                },
            )
        )
        # All three completed
        for tid in (grandchild_task, child_task, root_task):
            await a.record(
                Event(
                    kind=EventKind.TASK_COMPLETED,
                    task_id=tid,
                    agent_id="exocortex",
                    payload={},
                )
            )

    asyncio.run(_go())


def test_handoffs_chains_returns_linked_tasks(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    root, child, grand = uuid4(), uuid4(), uuid4()
    _seed_handoff_chain(audit_path, root_task=root, child_task=child, grandchild_task=grand)

    with _client(web_env) as client:
        r = client.get("/api/handoffs/chains?min_depth=1")
        assert r.status_code == 200
        body = r.json()
        # Single root → 1 chain.
        assert body["count"] == 1
        chain = body["items"][0]
        assert chain["chain_id"] == str(root)
        assert chain["depth"] == 3
        assert chain["agents_path"] == ["hermes", "codex", "hermes"]
        assert chain["status"] == "completed"


def test_handoffs_chains_filters_by_min_depth(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    # Single solo task — depth 1.
    _seed_events(audit_path, uuid4())

    with _client(web_env) as client:
        all_chains = client.get("/api/handoffs/chains?min_depth=1").json()
        assert all_chains["count"] == 1
        deep_only = client.get("/api/handoffs/chains?min_depth=2").json()
        assert deep_only["count"] == 0


def test_handoffs_chain_for_task_walks_to_root(web_env: tuple[Path, Path]) -> None:
    audit_path, _ = web_env
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    root, child, grand = uuid4(), uuid4(), uuid4()
    _seed_handoff_chain(audit_path, root_task=root, child_task=child, grandchild_task=grand)

    with _client(web_env) as client:
        # From the leaf, the chain endpoint should still return the root's chain.
        r = client.get(f"/api/handoffs/chain/{grand}")
        assert r.status_code == 200
        body = r.json()
        assert body["chain_id"] == str(root)
        assert body["depth"] == 3


def test_handoffs_chain_for_task_404_unknown(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get(f"/api/handoffs/chain/{uuid4()}")
        assert r.status_code == 404


def test_conversations_create_and_list(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        # List empty.
        r = client.get("/api/conversations")
        assert r.status_code == 200
        assert r.json() == {"count": 0, "items": []}

        # Create.
        r = client.post(
            "/api/conversations",
            json={"topic": "should we use postgres?", "participants": ["hermes", "codex"]},
        )
        assert r.status_code == 200
        body = r.json()
        cid = body["id"]
        assert body["topic"] == "should we use postgres?"
        assert body["participants"] == ["hermes", "codex"]
        assert body["status"] == "open"
        assert body["turn_count"] == 0

        # List shows it.
        listed = client.get("/api/conversations").json()
        assert listed["count"] == 1
        assert listed["items"][0]["id"] == cid


def test_conversations_create_validates_participants(
    web_env: tuple[Path, Path],
) -> None:
    with _client(web_env) as client:
        # Too few.
        r = client.post(
            "/api/conversations",
            json={"topic": "x", "participants": ["hermes"]},
        )
        assert r.status_code == 400

        # Duplicates.
        r = client.post(
            "/api/conversations",
            json={"topic": "x", "participants": ["hermes", "hermes"]},
        )
        assert r.status_code == 400

        # Empty topic.
        r = client.post(
            "/api/conversations",
            json={"topic": "", "participants": ["hermes", "codex"]},
        )
        assert r.status_code == 400


def test_conversations_turn_and_history(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        cid = client.post(
            "/api/conversations",
            json={"topic": "x", "participants": ["hermes", "codex"]},
        ).json()["id"]

        t1 = client.post(
            f"/api/conversations/{cid}/turn",
            json={"from_agent": "hermes", "to_agent": "codex", "content": "hi"},
        )
        assert t1.status_code == 200

        t2 = client.post(
            f"/api/conversations/{cid}/turn",
            json={"from_agent": "codex", "to_agent": "hermes", "content": "hey"},
        )
        assert t2.status_code == 200

        hist = client.get(f"/api/conversations/{cid}").json()
        assert hist["turn_count"] == 2
        assert len(hist["turns"]) == 2
        assert hist["turns"][0]["from_agent"] == "hermes"
        assert hist["turns"][1]["from_agent"] == "codex"


def test_conversations_delete_hides_from_listing(
    web_env: tuple[Path, Path],
) -> None:
    with _client(web_env) as client:
        cid = client.post(
            "/api/conversations",
            json={"topic": "to-delete", "participants": ["hermes", "codex"]},
        ).json()["id"]

        # Visible before delete.
        listed = client.get("/api/conversations").json()
        assert any(c["id"] == cid for c in listed["items"])

        # Delete.
        r = client.delete(f"/api/conversations/{cid}")
        assert r.status_code == 200
        assert r.json()["status"] == "deleted"

        # Hidden from listing.
        listed_after = client.get("/api/conversations").json()
        assert not any(c["id"] == cid for c in listed_after["items"])

        # GET /{id} returns 404 (deleted is "as if it never existed" to the API).
        r2 = client.get(f"/api/conversations/{cid}")
        assert r2.status_code == 404


def test_conversations_delete_blocks_further_turns(
    web_env: tuple[Path, Path],
) -> None:
    with _client(web_env) as client:
        cid = client.post(
            "/api/conversations",
            json={"topic": "x", "participants": ["hermes", "codex"]},
        ).json()["id"]
        client.delete(f"/api/conversations/{cid}")

        rejected = client.post(
            f"/api/conversations/{cid}/turn",
            json={"from_agent": "hermes", "to_agent": "codex", "content": "ghost"},
        )
        assert rejected.status_code == 400


def test_conversations_delete_unknown_404(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.delete(f"/api/conversations/{uuid4()}")
        assert r.status_code == 404


def test_conversations_close_blocks_further_turns(
    web_env: tuple[Path, Path],
) -> None:
    with _client(web_env) as client:
        cid = client.post(
            "/api/conversations",
            json={"topic": "x", "participants": ["hermes", "codex"]},
        ).json()["id"]

        client.post(
            f"/api/conversations/{cid}/turn",
            json={"from_agent": "hermes", "to_agent": "codex", "content": "hi"},
        )
        close = client.post(f"/api/conversations/{cid}/close")
        assert close.status_code == 200
        assert close.json()["status"] == "closed"

        rejected = client.post(
            f"/api/conversations/{cid}/turn",
            json={"from_agent": "codex", "to_agent": "hermes", "content": "late"},
        )
        assert rejected.status_code == 400


def test_conversations_get_404_unknown(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get(f"/api/conversations/{uuid4()}")
        assert r.status_code == 404


def test_conversations_page_served(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/conversations")
        assert r.status_code == 200


def test_profile_gaps_returns_dimensions(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/api/profile/gaps")
        assert r.status_code == 200
        body = r.json()
        # Empty profile → every dimension shows as a gap.
        assert body["count"] == 8
        dims = {item["dimension"] for item in body["items"]}
        assert "profile.preference" in dims
        assert "profile.communication_style" in dims


def test_static_css_served(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/static/app.css")
        assert r.status_code == 200
        assert "--bg" in r.text


# --- A2: local-trust guard (CSRF + cross-site WS hijack) --------------------

def test_guard_allows_no_origin(web_env: tuple[Path, Path]) -> None:
    """CLI / curl / same-origin loads send no Origin and must pass."""
    with _client(web_env) as client:
        assert client.get("/api/status").status_code == 200


def test_guard_allows_loopback_origin(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/api/status", headers={"Origin": "http://localhost:8756"})
        assert r.status_code == 200
        r2 = client.get("/api/status", headers={"Origin": "http://127.0.0.1:9999"})
        assert r2.status_code == 200


def test_guard_rejects_cross_origin(web_env: tuple[Path, Path]) -> None:
    with _client(web_env) as client:
        r = client.get("/api/status", headers={"Origin": "http://evil.example.com"})
        assert r.status_code == 403


def test_guard_rejects_cross_origin_websocket(web_env: tuple[Path, Path]) -> None:
    """A cross-site page must not be able to stream the audit feed."""
    with (
        _client(web_env) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/api/events", headers={"Origin": "http://evil.example.com"}
        ) as ws,
    ):
        ws.receive_text()


def test_guard_allows_same_origin_websocket(web_env: tuple[Path, Path]) -> None:
    with (
        _client(web_env) as client,
        client.websocket_connect("/api/events") as ws,
    ):
        # First frame is the hello handshake; connection stayed open.
        assert ws.receive_json() is not None


def test_guard_token_required_when_configured(
    web_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOCORTEX_WEB_TOKEN", "s3cret")
    with _client(web_env) as client:
        assert client.get("/api/status").status_code == 403
        ok = client.get("/api/status", headers={"X-Exocortex-Token": "s3cret"})
        assert ok.status_code == 200
        viaquery = client.get("/api/status?token=s3cret")
        assert viaquery.status_code == 200
