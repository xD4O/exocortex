"""HermesBridge unit tests — capability, session-id parsing, and the real
subprocess path driven via a tiny shell script that mimics hermes output.

Real hermes invocations live in tests/integration/test_hermes_real.py and
only run when EXOCORTEX_RUN_HERMES=1 is set (to avoid burning credits).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from exocortex.agents.bridge import (
    HermesBridge,
    HermesSubprocessError,
    HermesSubprocessProcess,
    ScriptedProcess,
)
from exocortex.agents.bridge.base import BridgeDeps
from exocortex.contracts import (
    Budget,
    Handoff,
    Task,
    ToolInvocationCursor,
)
from exocortex.core.events import EventBus
from exocortex.core.session_manager import SessionManager
from exocortex.memory.durable import DurableMemoryStore
from exocortex.memory.embedding import DeterministicEmbeddingProvider
from exocortex.memory.session import SessionMemoryStore
from exocortex.memory.summarizer import TruncatingSummarizer
from exocortex.observability.audit import AuditLog
from exocortex.policy.approvals import ApprovalQueue, auto_approve_resolver
from exocortex.policy.rule_engine import DeclarativeRuleEngine, default_rules
from exocortex.tools.builtin import register_builtins
from exocortex.tools.executor import ToolExecutor
from exocortex.tools.registry import ToolRegistry

# --- Capability ------------------------------------------------------------


def test_capability_matches_hermes_shape(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_builtins(registry)
    policy = DeclarativeRuleEngine(rules=default_rules())
    bus = EventBus(policy)
    bus.set_audit_sink(AuditLog(tmp_path / "a.jsonl").record)
    approvals = ApprovalQueue(bus, auto_approve_resolver)
    executor = ToolExecutor(
        registry=registry, policy=policy, bus=bus, approvals=approvals
    )
    deps = BridgeDeps(
        bus=bus,
        executor=executor,
        session_manager=SessionManager(bus),
        session_memory=SessionMemoryStore(),
        durable_memory=DurableMemoryStore(tmp_path / "mem.db"),
        embedder=DeterministicEmbeddingProvider(),
        summarizer=TruncatingSummarizer(),
    )
    bridge = HermesBridge(
        agent_id="hermes", deps=deps, proc=ScriptedProcess([])
    )
    cap = bridge.capability()
    # Hermes is MCP-client AND MCP-server capable (`hermes mcp serve`).
    assert cap.mcp_client is True
    assert cap.mcp_server is True
    assert cap.run_shell is True
    assert cap.edit_files is True


# --- Session-id parsing ----------------------------------------------------


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ("Response text\nsession_id: abc-123\n", "abc-123"),
        ("Response text\nSession-id: xyz_789\n", "xyz_789"),
        ("body\nSession: 01HZABC\n", "01HZABC"),
        ("only response, no id", None),
    ],
)
def test_session_id_extraction(stdout: str, expected: str | None) -> None:
    assert HermesSubprocessProcess._extract_session_id(stdout) == expected


def test_strip_session_marker_removes_line() -> None:
    out = "response line one\nresponse line two\nsession_id: abc\n"
    stripped = HermesSubprocessProcess._strip_session_marker(out, "abc")
    assert "abc" not in stripped
    assert "response line one" in stripped


def test_strip_session_marker_noop_without_id() -> None:
    out = "plain response"
    assert (
        HermesSubprocessProcess._strip_session_marker(out, None) == "plain response"
    )


# --- argv construction -----------------------------------------------------


def test_argv_includes_resume_when_given() -> None:
    proc = HermesSubprocessProcess(source="exocortex")
    argv = proc._build_argv(query="hi", resume_session_id="sess-42")
    assert "--resume" in argv
    idx = argv.index("--resume")
    assert argv[idx + 1] == "sess-42"


def test_argv_always_quiet_and_passes_session_id() -> None:
    proc = HermesSubprocessProcess()
    argv = proc._build_argv(query="hi", resume_session_id=None)
    assert "-Q" in argv
    assert "--pass-session-id" in argv
    assert "--accept-hooks" in argv
    # No --resume unless asked.
    assert "--resume" not in argv


def test_argv_respects_model_and_max_turns() -> None:
    proc = HermesSubprocessProcess(
        model="anthropic/claude-sonnet-4", max_turns=5
    )
    argv = proc._build_argv(query="hi", resume_session_id=None)
    assert "-m" in argv
    assert "anthropic/claude-sonnet-4" in argv
    assert "--max-turns" in argv
    assert "5" in argv


def test_argv_extra_args_appended() -> None:
    proc = HermesSubprocessProcess(extra_args=["--verbose", "--ignore-rules"])
    argv = proc._build_argv(query="hi", resume_session_id=None)
    assert "--verbose" in argv
    assert "--ignore-rules" in argv


# --- Real subprocess path via a fake `hermes` shim -------------------------


def _write_fake_hermes(
    path: Path, *, response: str, session_id: str | None, exit_code: int = 0
) -> Path:
    """Create a tiny shell script that mimics `hermes chat -q ... -Q` output.

    Hermes-style stdout: response body, then `session_id: <id>` line, exit 0.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    script = "#!/bin/sh\n"
    script += f"printf '%s\\n' '{response}'\n"
    if session_id is not None:
        script += f"printf 'session_id: %s\\n' '{session_id}'\n"
    script += f"exit {exit_code}\n"
    path.write_text(script)
    path.chmod(0o755)
    return path


@pytest.mark.asyncio
async def test_real_subprocess_parses_response_and_session_id(
    tmp_path: Path,
) -> None:
    fake = _write_fake_hermes(
        tmp_path / "bin" / "hermes",
        response="The answer is 42.",
        session_id="sess-abc-123",
    )
    proc = HermesSubprocessProcess(binary=str(fake), worktree=tmp_path)
    await proc.start(
        Task(goal="What is the meaning of life?"), handoff_in=None
    )
    assert proc.session_id == "sess-abc-123"

    first = await proc.next_action()
    assert first is not None
    assert first.__class__.__name__ == "WriteMemory"
    assert "The answer is 42" in first.content  # type: ignore[attr-defined]
    assert "sess-abc-123" not in first.content  # type: ignore[attr-defined]
    assert first.durable is True  # type: ignore[attr-defined]

    second = await proc.next_action()
    assert second is not None
    assert second.__class__.__name__ == "TaskDone"

    third = await proc.next_action()
    assert third is None


@pytest.mark.asyncio
async def test_real_subprocess_propagates_nonzero_exit(tmp_path: Path) -> None:
    fake = _write_fake_hermes(
        tmp_path / "bin" / "hermes",
        response="something broke",
        session_id=None,
        exit_code=2,
    )
    proc = HermesSubprocessProcess(binary=str(fake), worktree=tmp_path)
    with pytest.raises(HermesSubprocessError) as ei:
        await proc.start(Task(goal="x"))
    assert "2" in str(ei.value)
    assert proc.is_alive is False


@pytest.mark.asyncio
async def test_real_subprocess_missing_binary_raises(tmp_path: Path) -> None:
    proc = HermesSubprocessProcess(binary=str(tmp_path / "does-not-exist"))
    with pytest.raises(HermesSubprocessError):
        await proc.start(Task(goal="x"))
    assert proc.is_alive is False


@pytest.mark.asyncio
async def test_real_subprocess_resumes_on_handoff(tmp_path: Path) -> None:
    # Fake hermes that echoes its argv so we can verify --resume was passed.
    fake = tmp_path / "bin" / "hermes"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text('#!/bin/sh\necho "got argv: $*"\necho "session_id: new-1"\nexit 0\n')
    fake.chmod(0o755)

    proc = HermesSubprocessProcess(binary=str(fake))
    handoff = Handoff(
        task_id=uuid4(),
        from_agent="codex",
        to_agent="hermes",
        sequence_no=1,
        goal_restatement="continue the prior session",
        constraints_active=[],
        decisions_so_far=[],
        open_questions=[],
        tool_invocation_cursor=ToolInvocationCursor(),
        memory_scope_ids=[f"task:{uuid4()}", "hermes_session:prior-sess-42"],
        expected_output="",
        budget_remaining=Budget(),
    )
    await proc.start(Task(goal="irrelevant"), handoff_in=handoff)

    # The response in our fake echoes argv; we should see --resume prior-sess-42.
    first = await proc.next_action()
    assert first is not None
    body = first.content  # type: ignore[attr-defined]
    assert "--resume" in body
    assert "prior-sess-42" in body


# --- Integration marker (opt-in only) --------------------------------------


@pytest.mark.skipif(
    os.environ.get("EXOCORTEX_RUN_HERMES") != "1",
    reason="set EXOCORTEX_RUN_HERMES=1 to invoke the real hermes binary",
)
@pytest.mark.asyncio
async def test_real_hermes_chat_smoke(tmp_path: Path) -> None:
    """Invokes the real `hermes chat -q '2+2'` — costs credits. Opt-in only."""
    if shutil.which("hermes") is None:
        pytest.skip("hermes binary not on PATH")

    proc = HermesSubprocessProcess(worktree=tmp_path)
    await proc.start(Task(goal="What is 2+2? Answer with a single number."))
    first = await proc.next_action()
    assert first is not None
    assert first.__class__.__name__ == "WriteMemory"
    # Don't assert exact content — just that we got *something* back.
    assert first.content.strip()  # type: ignore[attr-defined]
