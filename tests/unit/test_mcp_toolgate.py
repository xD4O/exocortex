"""A1/A5: the ad-hoc MCP fs/shell tools are policy-checked, sandbox-confined,
secret-denying, and audited — no longer a direct bypass of the policy engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.config import Settings
from exocortex.contracts import EventKind
from exocortex.observability.audit import AuditLog
from exocortex.operator.mcp.toolgate import McpToolGate, redact_argv
from exocortex.tools.errors import PolicyViolationError


def _gate(tmp_path: Path) -> tuple[McpToolGate, AuditLog, Path]:
    sandbox = tmp_path / "work"
    sandbox.mkdir()
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path)
    settings = Settings(
        tool_sandbox_root=sandbox,
        audit_log_path=audit_path,
        data_dir=tmp_path,
        memory_db_path=tmp_path / "mem.db",
    )
    return McpToolGate(settings=settings, audit=audit), audit, sandbox


@pytest.mark.asyncio
async def test_fs_read_inside_sandbox_allowed(tmp_path: Path) -> None:
    gate, _audit, sandbox = _gate(tmp_path)
    target = sandbox / "notes.txt"
    target.write_text("hello from the worktree", encoding="utf-8")

    result = await gate.invoke(
        tool="fs.read", arguments={"path": str(target)}, agent_id="codex"
    )
    assert "hello from the worktree" in result["content"]


@pytest.mark.asyncio
async def test_fs_read_outside_sandbox_denied(tmp_path: Path) -> None:
    gate, _audit, _sandbox = _gate(tmp_path)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("not for you", encoding="utf-8")

    with pytest.raises(PolicyViolationError):
        await gate.invoke(
            tool="fs.read", arguments={"path": str(outside)}, agent_id="codex"
        )


@pytest.mark.asyncio
async def test_fs_read_secret_path_denied_even_inside_sandbox(tmp_path: Path) -> None:
    """The exact exploit from the audit: a secret path is denied by a
    first-match rule regardless of the sandbox root."""
    gate, _audit, sandbox = _gate(tmp_path)
    ssh = sandbox / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")

    with pytest.raises(PolicyViolationError):
        await gate.invoke(
            tool="fs.read",
            arguments={"path": str(ssh / "id_rsa")},
            agent_id="codex",
        )


@pytest.mark.asyncio
async def test_denied_call_is_audited(tmp_path: Path) -> None:
    gate, audit, sandbox = _gate(tmp_path)
    with pytest.raises(PolicyViolationError):
        await gate.invoke(
            tool="fs.read",
            arguments={"path": str(sandbox / ".aws" / "credentials")},
            agent_id="hermes",
        )
    events = await audit.read_all()
    kinds = [e.kind for e in events]
    assert EventKind.TOOL_PROPOSED in kinds
    assert EventKind.TOOL_POLICY_CHECKED in kinds
    assert EventKind.TOOL_REJECTED in kinds


@pytest.mark.asyncio
async def test_shell_exec_inside_sandbox_runs_and_audits(tmp_path: Path) -> None:
    gate, audit, sandbox = _gate(tmp_path)
    result = await gate.invoke(
        tool="shell.exec",
        arguments={"argv": ["echo", "hi"], "cwd": str(sandbox)},
        agent_id="codex",
    )
    assert result["returncode"] == 0
    assert "hi" in result["stdout"]
    kinds = [e.kind for e in await audit.read_all()]
    assert EventKind.TOOL_EXECUTED in kinds


def test_redact_argv_masks_secrets() -> None:
    assert redact_argv(["mysql", "-pHUNTER2"]) == ["mysql", "-p«redacted»"]
    assert redact_argv(["curl", "--token=abc123"]) == ["curl", "--token=«redacted»"]
    assert redact_argv(["tool", "--api-key", "xyz"]) == [
        "tool",
        "--api-key",
        "«redacted»",
    ]
    assert redact_argv(["curl", "-H", "Authorization: Bearer sk-secret"]) == [
        "curl",
        "-H",
        "Authorization: Bearer «redacted»",
    ]
    # Non-secret args are untouched.
    assert redact_argv(["git", "status", "--short"]) == ["git", "status", "--short"]


@pytest.mark.asyncio
async def test_write_auto_approved_by_default(tmp_path: Path) -> None:
    gate, _audit, sandbox = _gate(tmp_path)  # dispatch_auto_approve_tools=True
    result = await gate.invoke(
        tool="fs.write",
        arguments={"path": str(sandbox / "out.txt"), "content": "data"},
        agent_id="codex",
    )
    assert result.get("bytes_written") == 4
    assert (sandbox / "out.txt").read_text() == "data"


@pytest.mark.asyncio
async def test_write_denied_when_auto_approve_off(tmp_path: Path) -> None:
    """A4: with auto-approve disabled, a REQUIRE_APPROVAL call is denied
    rather than silently executed."""
    sandbox = tmp_path / "work"
    sandbox.mkdir()
    audit_path = tmp_path / "audit.jsonl"
    settings = Settings(
        tool_sandbox_root=sandbox,
        audit_log_path=audit_path,
        data_dir=tmp_path,
        memory_db_path=tmp_path / "mem.db",
        dispatch_auto_approve_tools=False,
    )
    gate = McpToolGate(settings=settings, audit=AuditLog(audit_path))
    with pytest.raises(PolicyViolationError):
        await gate.invoke(
            tool="fs.write",
            arguments={"path": str(sandbox / "out.txt"), "content": "x"},
            agent_id="codex",
        )
    assert not (sandbox / "out.txt").exists()
