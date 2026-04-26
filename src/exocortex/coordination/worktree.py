from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from exocortex.observability.logging import get_logger

logger = get_logger("exocortex.worktree")


class WorktreeError(Exception):
    pass


class WorktreeManager:
    """Per-task git worktrees (CLAUDE-PLAN.MD Bet E).

    Prevents concurrent agents from corrupting each other's state by giving
    each task an isolated branch + checkout. Conflict detection becomes a
    merge-gate problem at task completion rather than a concurrent-write
    problem during execution.
    """

    def __init__(self, repo_path: Path, *, worktree_root: Path | None = None) -> None:
        self.repo_path = repo_path.resolve()
        self.worktree_root = (
            worktree_root.resolve()
            if worktree_root is not None
            else self.repo_path.parent / "worktrees"
        )
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    async def create(self, task_id: UUID, *, base: str = "HEAD") -> Path:
        branch = f"exocortex/task-{task_id}"
        path = self.worktree_root / f"task-{task_id}"
        if path.exists():
            raise WorktreeError(f"worktree already exists: {path}")
        await self._git("worktree", "add", "-b", branch, str(path), base)
        logger.info("worktree.created", task_id=str(task_id), path=str(path), branch=branch)
        return path

    async def remove(self, path: Path) -> None:
        await self._git("worktree", "remove", "--force", str(path))
        logger.info("worktree.removed", path=str(path))

    async def list_all(self) -> list[str]:
        out = await self._git("worktree", "list", "--porcelain")
        return out.splitlines()

    async def _git(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self.repo_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise WorktreeError(
                f"git {' '.join(args)} failed ({proc.returncode}): "
                f"{stderr.decode('utf-8', 'replace').strip()}"
            )
        return stdout.decode("utf-8", "replace")
