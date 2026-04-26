from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.tools.builtin.fs import FS_LIST_SPEC, FS_READ_SPEC, FS_WRITE_SPEC
from exocortex.tools.builtin.shell import SHELL_EXEC_SPEC
from exocortex.tools.errors import ToolArgumentError, ToolTimeoutError


@pytest.mark.asyncio
async def test_fs_read(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    out = await FS_READ_SPEC.handler({"path": str(tmp_path / "a.txt")})
    assert out["content"] == "hello"


@pytest.mark.asyncio
async def test_fs_write_creates_parent(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "a.txt"
    out = await FS_WRITE_SPEC.handler({"path": str(target), "content": "xyz"})
    assert target.read_text() == "xyz"
    assert out["bytes_written"] == 3


@pytest.mark.asyncio
async def test_fs_list(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("")
    (tmp_path / "b").write_text("")
    out = await FS_LIST_SPEC.handler({"path": str(tmp_path)})
    assert out["entries"] == ["a", "b"]


@pytest.mark.asyncio
async def test_fs_write_requires_strings() -> None:
    with pytest.raises(ToolArgumentError):
        await FS_WRITE_SPEC.handler({"path": 1, "content": "x"})


@pytest.mark.asyncio
async def test_shell_exec_basic(tmp_path: Path) -> None:
    out = await SHELL_EXEC_SPEC.handler(
        {"argv": ["/bin/echo", "hi"], "cwd": str(tmp_path)}
    )
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]


@pytest.mark.asyncio
async def test_shell_exec_timeout(tmp_path: Path) -> None:
    with pytest.raises(ToolTimeoutError):
        await SHELL_EXEC_SPEC.handler(
            {"argv": ["/bin/sleep", "5"], "cwd": str(tmp_path), "timeout_seconds": 1}
        )


@pytest.mark.asyncio
async def test_shell_exec_validates_args(tmp_path: Path) -> None:
    with pytest.raises(ToolArgumentError):
        await SHELL_EXEC_SPEC.handler({"argv": [], "cwd": str(tmp_path)})
    with pytest.raises(ToolArgumentError):
        await SHELL_EXEC_SPEC.handler({"argv": ["/bin/true"], "cwd": 123})
