from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from exocortex.coordination.worktree import WorktreeError, WorktreeManager

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    base_env = [
        "-c",
        "user.email=test@exocortex.local",
        "-c",
        "user.name=exocortex-test",
        "-c",
        "init.defaultBranch=main",
        "-c",
        "commit.gpgsign=false",
    ]
    for args in (
        ["init"],
        [*base_env, "commit", "--allow-empty", "-m", "initial"],
    ):
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        assert proc.returncode == 0, stderr.decode()


@pytest.mark.asyncio
async def test_worktree_create_and_remove(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    await _init_repo(repo)

    mgr = WorktreeManager(repo, worktree_root=tmp_path / "wts")

    tid = uuid4()
    wt = await mgr.create(tid)
    assert wt.exists()
    assert wt.is_dir()
    # branch contains task id
    listing = await mgr.list_all()
    assert any(f"task-{tid}" in line for line in listing)

    await mgr.remove(wt)
    assert not wt.exists()


@pytest.mark.asyncio
async def test_worktree_create_rejects_duplicate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    await _init_repo(repo)

    mgr = WorktreeManager(repo, worktree_root=tmp_path / "wts")
    tid = uuid4()
    await mgr.create(tid)

    with pytest.raises(WorktreeError):
        await mgr.create(tid)
