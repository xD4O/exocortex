"""CodexBridge unit tests — capability, JSONL session-id parsing, argv shape,
and the real subprocess path driven by a fake `codex` shell shim.

Real codex invocations live in tests/integration/test_codex_real.py and only
run when EXOCORTEX_RUN_CODEX=1 is set (to avoid burning credits).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from exocortex.agents.bridge import (
    CodexBridge,
    CodexSubprocessError,
    CodexSubprocessProcess,
    ScriptedProcess,
)
from exocortex.agents.bridge.base import BridgeDeps
from exocortex.config import Settings
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
from exocortex.operator.mcp.dispatch import DispatchService
from exocortex.policy.approvals import ApprovalQueue, auto_approve_resolver
from exocortex.policy.rule_engine import DeclarativeRuleEngine, default_rules
from exocortex.tools.builtin import register_builtins
from exocortex.tools.executor import ToolExecutor
from exocortex.tools.registry import ToolRegistry

# --- Capability ------------------------------------------------------------


def test_capability_matches_codex_shape(tmp_path: Path) -> None:
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
    bridge = CodexBridge(
        agent_id="codex", deps=deps, proc=ScriptedProcess([])
    )
    cap = bridge.capability()
    # Codex is MCP-client AND MCP-server capable (`codex mcp-server`).
    assert cap.mcp_client is True
    assert cap.mcp_server is True
    assert cap.run_shell is True
    assert cap.edit_files is True


# --- Session-id extraction from JSONL stream -------------------------------


def test_session_id_from_structured_jsonl() -> None:
    stream = "\n".join(
        [
            json.dumps({"type": "banner", "version": "0.125.0"}),
            json.dumps(
                {"type": "session_configured", "session_id": "abc-123-def"}
            ),
            json.dumps({"type": "message", "content": "done"}),
        ]
    )
    assert (
        CodexSubprocessProcess._extract_session_id(stream) == "abc-123-def"
    )


def test_session_id_from_nested_jsonl() -> None:
    stream = json.dumps(
        {
            "type": "init",
            "session": {"conversation_id": "conv-xyz-9"},
        }
    )
    assert CodexSubprocessProcess._extract_session_id(stream) == "conv-xyz-9"


def test_session_id_regex_fallback() -> None:
    # Malformed JSONL (trailing garbage) — should still match via regex.
    stream = 'noise\n{"session_id":"deadbeef-1234-abcd"} trailing junk\n'
    assert (
        CodexSubprocessProcess._extract_session_id(stream)
        == "deadbeef-1234-abcd"
    )


def test_session_id_absent_returns_none() -> None:
    stream = json.dumps({"type": "plain", "text": "hi"})
    assert CodexSubprocessProcess._extract_session_id(stream) is None


# --- argv construction -----------------------------------------------------


def test_argv_basic_shape(tmp_path: Path) -> None:
    proc = CodexSubprocessProcess(worktree=tmp_path)
    argv = proc._build_argv(
        prompt="hi", output_file="/tmp/x", resume_session_id=None
    )
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "--json" in argv
    assert "-o" in argv
    assert "/tmp/x" in argv
    assert "-s" in argv
    assert "workspace-write" in argv
    assert "-C" in argv
    assert str(tmp_path) in argv
    assert "--skip-git-repo-check" in argv
    # Prompt must be the last positional arg.
    assert argv[-1] == "hi"


def test_argv_with_resume_uses_resume_subcommand() -> None:
    proc = CodexSubprocessProcess()
    argv = proc._build_argv(
        prompt="continue", output_file="/tmp/x", resume_session_id="sess-42"
    )
    # Expect `codex exec resume sess-42 ... continue`
    exec_idx = argv.index("exec")
    assert argv[exec_idx + 1] == "resume"
    assert argv[exec_idx + 2] == "sess-42"


def test_argv_model_and_full_auto() -> None:
    proc = CodexSubprocessProcess(model="gpt-5-codex", full_auto=True)
    argv = proc._build_argv(
        prompt="hi", output_file="/tmp/x", resume_session_id=None
    )
    assert "-m" in argv
    assert "gpt-5-codex" in argv
    assert "--full-auto" in argv


def test_argv_sandbox_mode_toggle() -> None:
    proc = CodexSubprocessProcess(sandbox_mode="read-only")
    argv = proc._build_argv(
        prompt="hi", output_file="/tmp/x", resume_session_id=None
    )
    assert "read-only" in argv
    assert "workspace-write" not in argv


def test_argv_extra_args_appended() -> None:
    proc = CodexSubprocessProcess(extra_args=["--ignore-rules"])
    argv = proc._build_argv(
        prompt="hi", output_file="/tmp/x", resume_session_id=None
    )
    assert "--ignore-rules" in argv


def test_argv_bypass_approvals_off_by_default() -> None:
    """Direct CLI use of CodexSubprocessProcess must NOT bypass approvals."""
    proc = CodexSubprocessProcess()
    argv = proc._build_argv(
        prompt="hi", output_file="/tmp/x", resume_session_id=None
    )
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_argv_bypass_approvals_when_enabled() -> None:
    """Dispatch path opts in so MCP tool calls auto-approve."""
    proc = CodexSubprocessProcess(bypass_approvals=True)
    argv = proc._build_argv(
        prompt="hi", output_file="/tmp/x", resume_session_id=None
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in argv


@pytest.mark.skipif(
    shutil.which("codex") is None, reason="codex binary not on PATH"
)
@pytest.mark.asyncio
async def test_dispatch_codex_factory_enables_bypass(tmp_path: Path) -> None:
    """Critical: the codex bridge factory used inside the dispatch service
    MUST construct CodexSubprocessProcess with bypass_approvals=True. If
    not, dispatched codex subprocesses cancel every MCP tool call and the
    delegated work fails silently."""
    settings = Settings(
        data_dir=tmp_path,
        audit_log_path=tmp_path / "audit.jsonl",
        memory_db_path=tmp_path / "memory.db",
    )
    svc = DispatchService(settings=settings)
    await svc._ensure_init()  # force router registration  # noqa: SLF001
    assert svc._router is not None  # noqa: SLF001
    codex_reg = next(
        (r for r in svc._router.registered() if r.agent_id == "codex"),  # noqa: SLF001
        None,
    )
    assert codex_reg is not None, "codex must be registered when binary present"

    # Factory must wire bypass_approvals=True.
    bridge = codex_reg.bridge_factory(tmp_path / "wt")
    proc = bridge._proc  # type: ignore[attr-defined]
    assert isinstance(proc, CodexSubprocessProcess)
    assert proc._bypass_approvals is True  # type: ignore[attr-defined]


# --- Real subprocess via a fake `codex` shim -------------------------------


def _write_fake_codex(
    path: Path,
    *,
    last_message: str,
    session_id: str | None,
    exit_code: int = 0,
) -> Path:
    """Create a shell script that mimics `codex exec` output:
      - writes JSONL events to stdout
      - writes `last_message` to the `-o` file
      - exits with `exit_code`

    The shim parses its own argv to locate the `-o` path, so the path can be
    anywhere pytest's tmp_path decided to put it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    session_line = (
        f'printf \'{{"type":"session_configured","session_id":"{session_id}"}}\\n\''
        if session_id is not None
        else "true"
    )
    script = f"""#!/bin/sh
out=""
found_o=0
for arg in "$@"; do
    if [ "$found_o" = "1" ]; then
        out="$arg"
        found_o=0
    fi
    if [ "$arg" = "-o" ]; then
        found_o=1
    fi
done

printf '{{"type":"banner","version":"0.125.0"}}\\n'
{session_line}
printf '{{"type":"message","content":"intermediate"}}\\n'
if [ -n "$out" ]; then
    printf '%s' '{last_message}' > "$out"
fi
exit {exit_code}
"""
    path.write_text(script)
    path.chmod(0o755)
    return path


@pytest.mark.asyncio
async def test_real_subprocess_parses_message_and_session(tmp_path: Path) -> None:
    fake = _write_fake_codex(
        tmp_path / "bin" / "codex",
        last_message="The final answer is 42.",
        session_id="sess-final",
    )
    proc = CodexSubprocessProcess(binary=str(fake), worktree=tmp_path)
    await proc.start(Task(goal="What is the meaning of life?"))
    assert proc.session_id == "sess-final"

    first = await proc.next_action()
    assert first is not None
    assert first.__class__.__name__ == "WriteMemory"
    assert first.content == "The final answer is 42."  # type: ignore[attr-defined]
    assert first.durable is True  # type: ignore[attr-defined]

    second = await proc.next_action()
    assert second is not None
    assert second.__class__.__name__ == "TaskDone"

    assert await proc.next_action() is None


@pytest.mark.asyncio
async def test_real_subprocess_nonzero_exit_raises(tmp_path: Path) -> None:
    fake = _write_fake_codex(
        tmp_path / "bin" / "codex",
        last_message="partial",
        session_id=None,
        exit_code=3,
    )
    proc = CodexSubprocessProcess(binary=str(fake), worktree=tmp_path)
    with pytest.raises(CodexSubprocessError) as ei:
        await proc.start(Task(goal="x"))
    assert "3" in str(ei.value)
    assert proc.is_alive is False


@pytest.mark.asyncio
async def test_real_subprocess_missing_binary_raises(tmp_path: Path) -> None:
    proc = CodexSubprocessProcess(binary=str(tmp_path / "no-such-codex"))
    with pytest.raises(CodexSubprocessError):
        await proc.start(Task(goal="x"))
    assert proc.is_alive is False


@pytest.mark.asyncio
async def test_real_subprocess_resumes_on_handoff(tmp_path: Path) -> None:
    # Fake codex that echoes its argv through the last-message file so we can
    # verify `exec resume <id>` was invoked.
    fake = tmp_path / "bin" / "codex"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(
        "#!/bin/sh\n"
        "out=\"\"; found_o=0\n"
        "for arg in \"$@\"; do\n"
        "  if [ \"$found_o\" = \"1\" ]; then out=\"$arg\"; found_o=0; fi\n"
        "  if [ \"$arg\" = \"-o\" ]; then found_o=1; fi\n"
        "done\n"
        "printf 'argv: %s\\n' \"$*\" > \"$out\"\n"
        'printf \'{"type":"session_configured","session_id":"new-sess"}\\n\'\n'
        "exit 0\n"
    )
    fake.chmod(0o755)

    proc = CodexSubprocessProcess(binary=str(fake), worktree=tmp_path)
    handoff = Handoff(
        task_id=uuid4(),
        from_agent="hermes",
        to_agent="codex",
        sequence_no=2,
        goal_restatement="continue the prior codex session",
        constraints_active=[],
        decisions_so_far=[],
        open_questions=[],
        tool_invocation_cursor=ToolInvocationCursor(),
        memory_scope_ids=[f"task:{uuid4()}", "codex_session:prior-codex-sess"],
        expected_output="",
        budget_remaining=Budget(),
    )
    await proc.start(Task(goal="unused"), handoff_in=handoff)

    first = await proc.next_action()
    assert first is not None
    body = first.content  # type: ignore[attr-defined]
    assert "resume" in body
    assert "prior-codex-sess" in body


# --- Integration marker (opt-in only) --------------------------------------


@pytest.mark.skipif(
    os.environ.get("EXOCORTEX_RUN_CODEX") != "1",
    reason="set EXOCORTEX_RUN_CODEX=1 to invoke the real codex binary",
)
@pytest.mark.asyncio
async def test_real_codex_exec_smoke(tmp_path: Path) -> None:
    """Invokes the real `codex exec '2+2' …` — costs credits. Opt-in only."""
    if shutil.which("codex") is None:
        pytest.skip("codex binary not on PATH")

    proc = CodexSubprocessProcess(worktree=tmp_path)
    await proc.start(
        Task(goal="Reply with only the number: what is 2+2?")
    )
    first = await proc.next_action()
    assert first is not None
    assert first.__class__.__name__ == "WriteMemory"
    assert first.content.strip()  # type: ignore[attr-defined]
