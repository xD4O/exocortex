from __future__ import annotations

import asyncio
from typing import Any

from exocortex.tools.errors import ToolArgumentError, ToolTimeoutError
from exocortex.tools.spec import RiskTier, ToolCategory, ToolSpec


async def _shell_exec(args: dict[str, Any]) -> dict[str, Any]:
    argv = args.get("argv")
    cwd = args.get("cwd")
    timeout = args.get("timeout_seconds", 30)

    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        raise ToolArgumentError("shell.exec requires 'argv' (non-empty list of strings)")
    if not isinstance(cwd, str):
        raise ToolArgumentError("shell.exec requires 'cwd' (string)")
    if not isinstance(timeout, int) or timeout <= 0:
        raise ToolArgumentError("'timeout_seconds' must be a positive int")

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise ToolTimeoutError(f"shell.exec timed out after {timeout}s") from e

    return {
        "argv": argv,
        "cwd": cwd,
        "returncode": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


SHELL_EXEC_SPEC = ToolSpec(
    name="shell.exec",
    description=(
        "Run a command in a subprocess. Args are argv + cwd. Policy restricts "
        "cwd to the task worktree; escapes are denied."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "cwd": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "default": 30},
        },
        "required": ["argv", "cwd"],
    },
    category=ToolCategory.SHELL,
    risk_tier=RiskTier.HIGH,
    handler=_shell_exec,
)
