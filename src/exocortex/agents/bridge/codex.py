"""OpenAI Codex CLI bridge + real-subprocess AgentProcess.

Codex is a CLI with a non-interactive `exec` subcommand, an MCP server mode
(`codex mcp-server`), its own sandbox system, and session resume support.
That makes it a Bridge, not a Runner (CLAUDE-PLAN.MD Bet B).

Integration surface used here:
  `codex exec "PROMPT" --json -o OUT_FILE -C WORKTREE -s SANDBOX \\
              [--skip-git-repo-check] [-m MODEL] \\
              [resume SESSION_ID]`

`--json` prints structured JSONL events to stdout; `-o FILE` writes the
final agent message cleanly to a file, so we don't have to parse the message
out of the event stream. `-s workspace-write` + our git worktree give
double-layered isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from exocortex.agents.bridge.actions import AgentAction
from exocortex.agents.bridge.base import Bridge
from exocortex.agents.bridge.protocol import build_response_actions, compose_agent_prompt
from exocortex.contracts import AgentCapability, Handoff, Task

# Session / conversation id fields that may appear in codex's JSONL stream.
_SESSION_FIELD_NAMES = ("session_id", "conversation_id", "rollout_id")
# Fallback regex: match "...id":"<uuid-like>"... anywhere in the stream.
_UUID_IN_JSON = re.compile(
    r'"(?:session|conversation|rollout)_id"\s*:\s*"([0-9a-fA-F-]{8,})"'
)


class CodexSubprocessError(RuntimeError):
    pass


class CodexSubprocessProcess:
    """Real-agent process: spawns `codex exec` as a subprocess.

    One-shot per task invocation. Stdout is JSONL events (discarded for MVP
    except for session-id extraction); the final message is read from the
    `-o` output file. Yields a single WriteMemory action carrying the
    response followed by TaskDone.
    """

    def __init__(
        self,
        *,
        binary: str = "codex",
        worktree: Path | None = None,
        model: str | None = None,
        sandbox_mode: str | None = "workspace-write",
        full_auto: bool = False,
        skip_git_repo_check: bool = True,
        ephemeral: bool = True,
        bypass_approvals: bool = False,
        extra_args: list[str] | None = None,
    ) -> None:
        # bypass_approvals: pass `--dangerously-bypass-approvals-and-sandbox`.
        # Required for dispatch contexts (no human to answer MCP-approval
        # prompts in the spawned subprocess). Off by default to keep direct
        # CLI use safe — only the dispatch path opts in.
        self._binary = binary
        self._worktree = worktree
        self._model = model
        self._sandbox_mode = sandbox_mode
        self._full_auto = full_auto
        self._skip_git_repo_check = skip_git_repo_check
        self._ephemeral = ephemeral
        self._bypass_approvals = bypass_approvals
        self._extra = list(extra_args or [])
        self._actions: list[AgentAction] = []
        self._alive = True
        self._session_id: str | None = None
        self._last_stdout: str = ""
        self._last_stderr: str = ""
        self._last_message: str = ""

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _build_argv(
        self, *, prompt: str, output_file: str, resume_session_id: str | None
    ) -> list[str]:
        argv: list[str] = [self._binary, "exec"]
        if resume_session_id is not None:
            argv.extend(["resume", resume_session_id])
        argv.append("--json")
        argv.extend(["-o", output_file])
        if self._sandbox_mode is not None:
            argv.extend(["-s", self._sandbox_mode])
        if self._full_auto:
            argv.append("--full-auto")
        if self._bypass_approvals:
            # Required for non-interactive subprocess dispatch — codex's
            # `exec` mode otherwise auto-cancels MCP tool calls when no
            # human is there to answer the approval prompt. EXTREMELY
            # DANGEROUS for direct CLI use; safe in our dispatch path
            # because the operator already authorized it via Hermes.
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        if self._worktree is not None:
            # Always pass an ABSOLUTE path. The subprocess's cwd kwarg is
            # applied first (codex starts inside the worktree), so a
            # relative `-C` would re-resolve from there and look for a
            # nested copy of the path that doesn't exist (ENOENT). This
            # was the silent failure mode behind every codex conversation
            # turn taking 0.04s and emitting `os error 2`.
            argv.extend(["-C", str(self._worktree.resolve())])
        if self._skip_git_repo_check:
            argv.append("--skip-git-repo-check")
        if self._ephemeral:
            argv.append("--ephemeral")
        if self._model is not None:
            argv.extend(["-m", self._model])
        argv.extend(self._extra)
        argv.append(prompt)
        return argv

    async def start(
        self, task: Task, handoff_in: Handoff | None = None
    ) -> None:
        # B2: give the receiving agent the FULL inbound bundle (constraints,
        # prior decisions, open questions, expected output) — not just the
        # restated goal it used to see.
        prompt = compose_agent_prompt(task, handoff_in)

        resume = None
        if handoff_in is not None:
            for scope in handoff_in.memory_scope_ids:
                kind, _, sid = scope.partition(":")
                if kind == "codex_session" and sid:
                    resume = sid
                    break

        # `-o` needs a path; use a temp file we can read back after exec.
        output_fd, output_path = tempfile.mkstemp(
            prefix="codex-last-", suffix=".txt"
        )
        with contextlib.suppress(OSError):
            os.close(output_fd)

        argv = self._build_argv(
            prompt=prompt, output_file=output_path, resume_session_id=resume
        )
        # cwd must be ABSOLUTE — see the `-C` comment above for why.
        cwd_abs = str(self._worktree.resolve()) if self._worktree else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd_abs,
                stdin=asyncio.subprocess.DEVNULL,  # prevent codex from reading stdin
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            self._alive = False
            raise CodexSubprocessError(
                f"codex binary not found at {self._binary!r}. "
                f"Install OpenAI Codex CLI or set binary= to the full path."
            ) from e

        stdout_b, stderr_b = await proc.communicate()
        self._last_stdout = stdout_b.decode("utf-8", errors="replace")
        self._last_stderr = stderr_b.decode("utf-8", errors="replace")

        # Look for structured error / turn.failed events in stdout first —
        # codex emits those to stdout as JSONL even when it eventually exits 0.
        structured_error = _extract_structured_error(self._last_stdout)

        if proc.returncode != 0 or structured_error is not None:
            self._alive = False
            Path(output_path).unlink(missing_ok=True)
            detail = structured_error or (
                self._last_stderr.strip() or self._last_stdout.strip()[:500]
            )
            raise CodexSubprocessError(
                f"codex exited {proc.returncode}: {detail}"
            )

        try:
            self._last_message = Path(output_path).read_text(encoding="utf-8").strip()
        finally:
            Path(output_path).unlink(missing_ok=True)

        self._session_id = self._extract_session_id(self._last_stdout)

        response = self._last_message or self._last_stdout.strip()
        # B1: if the agent's final message asks to hand off (@handoff-to: …),
        # emit RequestHandoff so the chain can actually continue past this hop.
        self._actions = build_response_actions(response, response_type="codex_response")

    async def next_action(self) -> AgentAction | None:
        if not self._alive or not self._actions:
            return None
        return self._actions.pop(0)

    async def kill(self) -> None:
        self._alive = False
        self._actions.clear()

    # --- Parsing helpers ----------------------------------------------------

    @staticmethod
    def _extract_session_id(jsonl_stream: str) -> str | None:
        """Best-effort scan of codex's JSONL stream for a session/conversation
        id. Tries proper JSON parse first (per line), falls back to regex.
        """
        for raw in jsonl_stream.splitlines():
            stripped = raw.strip()
            if not stripped or not stripped.startswith("{"):
                continue
            try:
                obj: Any = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            sid = _find_session_id_in_obj(obj)
            if sid:
                return sid

        match = _UUID_IN_JSON.search(jsonl_stream)
        if match:
            return match.group(1)
        return None


def _extract_structured_error(jsonl_stream: str) -> str | None:
    """Look for `{"type":"error", ...}` or `{"type":"turn.failed", ...}` events
    in codex's JSONL stream. These indicate an actual failure regardless of
    exit code, and usually carry a much more useful message than stderr.
    """
    for line in jsonl_stream.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") in ("error", "turn.failed"):
            msg = obj.get("message")
            if isinstance(msg, str) and msg:
                return msg
            err = obj.get("error")
            if isinstance(err, dict):
                inner = err.get("message")
                if isinstance(inner, str) and inner:
                    return inner
            return json.dumps(obj)
    return None


def _find_session_id_in_obj(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key in _SESSION_FIELD_NAMES:
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
        for val in obj.values():
            found = _find_session_id_in_obj(val)
            if found:
                return found
    elif isinstance(obj, list):
        for val in obj:
            found = _find_session_id_in_obj(val)
            if found:
                return found
    return None


class CodexBridge(Bridge):
    """Wraps the OpenAI Codex CLI as a Bridge.

    Pass `proc=CodexSubprocessProcess(worktree=<path>)` for real-binary
    integration; `proc=ScriptedProcess([...])` for conformance / CI tests.
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
            mcp_server=True,  # `codex mcp-server`
            interactive=True,
            batch=False,
        )
