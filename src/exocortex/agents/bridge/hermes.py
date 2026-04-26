"""Nous Research Hermes Agent bridge + real-subprocess AgentProcess.

Hermes is a Claude-Code-like interactive CLI with its own tool-calling loop,
hook system, and MCP support (both client and server). That makes it a
Bridge, not a Runner (CLAUDE-PLAN.MD Bet B).

Integration surface used here:
  `hermes chat -q QUERY -Q --pass-session-id --source exocortex [--resume ID]`

In quiet mode (`-Q`) Hermes suppresses banner/spinner/tool-previews and emits
only the final response plus session info — suitable for programmatic use.
Mid-turn tool-call observability requires the MCP integration path (Phase 6.5
— `hermes mcp serve` + exocortex as MCP client) and is deferred.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from exocortex.agents.bridge.actions import (
    AgentAction,
    TaskDone,
    WriteMemory,
)
from exocortex.agents.bridge.base import Bridge
from exocortex.contracts import AgentCapability, Handoff, Task

# Hermes --pass-session-id emits something like `session_id: <uuid>` or
# `Session: <uuid>`. Match both shapes; session id capture is best-effort.
_SESSION_ID_PATTERNS = (
    re.compile(r"session[_-]id[:=]\s*([A-Za-z0-9_-]+)", re.IGNORECASE),
    re.compile(r"^Session:\s+([A-Za-z0-9_-]+)\s*$", re.MULTILINE),
)


class HermesSubprocessError(RuntimeError):
    pass


class HermesSubprocessProcess:
    """Real-agent process: spawns `hermes chat -q` as a subprocess.

    One-shot per task invocation: sends the query, reads the final response
    and session id from stdout, then yields a single WriteMemory action
    carrying the response followed by TaskDone.
    """

    def __init__(
        self,
        *,
        binary: str = "hermes",
        source: str = "exocortex",
        worktree: Path | None = None,
        extra_args: list[str] | None = None,
        model: str | None = None,
        accept_hooks: bool = True,
        max_turns: int | None = None,
    ) -> None:
        self._binary = binary
        self._source = source
        self._worktree = worktree
        self._extra = list(extra_args or [])
        self._model = model
        self._accept_hooks = accept_hooks
        self._max_turns = max_turns
        self._actions: list[AgentAction] = []
        self._alive = True
        self._session_id: str | None = None
        self._last_stdout: str = ""
        self._last_stderr: str = ""

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _build_argv(self, query: str, resume_session_id: str | None) -> list[str]:
        argv = [
            self._binary,
            "chat",
            "-q",
            query,
            "-Q",
            "--pass-session-id",
            "--source",
            self._source,
        ]
        if self._accept_hooks:
            argv.append("--accept-hooks")
        if self._model is not None:
            argv.extend(["-m", self._model])
        if self._max_turns is not None:
            argv.extend(["--max-turns", str(self._max_turns)])
        if resume_session_id is not None:
            argv.extend(["--resume", resume_session_id])
        argv.extend(self._extra)
        return argv

    async def start(
        self, task: Task, handoff_in: Handoff | None = None
    ) -> None:
        query = (
            handoff_in.goal_restatement if handoff_in is not None else task.goal
        )
        resume = None
        # If an incoming handoff carries a prior hermes session id, resume it.
        # We encode it into memory_scope_ids as "hermes_session:<id>".
        if handoff_in is not None:
            for scope in handoff_in.memory_scope_ids:
                kind, _, sid = scope.partition(":")
                if kind == "hermes_session" and sid:
                    resume = sid
                    break

        argv = self._build_argv(query, resume)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self._worktree) if self._worktree else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            self._alive = False
            raise HermesSubprocessError(
                f"hermes binary not found at {self._binary!r}. "
                f"Install Hermes Agent or set binary= to the full path."
            ) from e

        stdout_b, stderr_b = await proc.communicate()
        self._last_stdout = stdout_b.decode("utf-8", errors="replace")
        self._last_stderr = stderr_b.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            self._alive = False
            raise HermesSubprocessError(
                f"hermes exited {proc.returncode}: "
                f"{self._last_stderr.strip() or self._last_stdout.strip()[:500]}"
            )

        self._session_id = self._extract_session_id(self._last_stdout)
        response = self._strip_session_marker(self._last_stdout, self._session_id)
        self._actions = [
            WriteMemory(content=response, durable=True, type="hermes_response"),
            TaskDone(success=True),
        ]

    async def next_action(self) -> AgentAction | None:
        if not self._alive or not self._actions:
            return None
        return self._actions.pop(0)

    async def kill(self) -> None:
        self._alive = False
        self._actions.clear()

    # --- Parsing helpers ----------------------------------------------------

    @staticmethod
    def _extract_session_id(text: str) -> str | None:
        for pattern in _SESSION_ID_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _strip_session_marker(text: str, session_id: str | None) -> str:
        if not session_id:
            return text.strip()
        kept = [
            line for line in text.splitlines() if session_id not in line
        ]
        return "\n".join(kept).strip()


class HermesBridge(Bridge):
    """Wraps Hermes Agent as a Bridge. Hermes speaks MCP as both client and
    server, and honors --worktree + --resume + --accept-hooks natively —
    unusually good alignment with Bets A/E.

    For end-to-end testing against the real `hermes` binary, construct with
    `proc=HermesSubprocessProcess(worktree=<path>)`. For CI and conformance
    tests, pass a ScriptedProcess just like the other bridges.
    """

    def capability(self) -> AgentCapability:
        return AgentCapability(
            agent_id=self.agent_id,
            kind="bridge",
            edit_files=True,
            run_shell=True,
            long_context=True,
            structured_output=True,
            mcp_client=True,
            mcp_server=True,  # `hermes mcp serve`
            interactive=True,
            batch=False,
        )
