from __future__ import annotations

import pytest

from exocortex.tools.builtin import register_builtins
from exocortex.tools.errors import ToolNotFoundError
from exocortex.tools.registry import ToolRegistry
from exocortex.tools.spec import RiskTier, ToolCategory, ToolSpec


async def _noop(_args: dict[str, object]) -> dict[str, object]:
    return {}


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"test {name}",
        input_schema={"type": "object"},
        category=ToolCategory.FS,
        risk_tier=RiskTier.LOW,
        handler=_noop,
    )


def test_register_and_get() -> None:
    r = ToolRegistry()
    r.register(_spec("fs.read"))
    assert "fs.read" in r
    assert r.get("fs.read").name == "fs.read"


def test_duplicate_rejected() -> None:
    r = ToolRegistry()
    r.register(_spec("fs.read"))
    with pytest.raises(ValueError):
        r.register(_spec("fs.read"))


def test_unknown_raises() -> None:
    r = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        r.get("nothing.here")


def test_all_is_sorted() -> None:
    r = ToolRegistry()
    r.register(_spec("z.last"))
    r.register(_spec("a.first"))
    assert [t.name for t in r.all()] == ["a.first", "z.last"]


def test_to_mcp_tools_shape() -> None:
    r = ToolRegistry()
    register_builtins(r)
    mcp = r.to_mcp_tools()
    names = {t["name"] for t in mcp}
    assert names == {"fs.read", "fs.write", "fs.list", "shell.exec"}
    for t in mcp:
        assert set(t) >= {"name", "description", "inputSchema"}


def test_register_builtins_includes_expected_categories() -> None:
    r = ToolRegistry()
    register_builtins(r)
    assert r.get("shell.exec").risk_tier == RiskTier.HIGH
    assert r.get("fs.read").risk_tier == RiskTier.LOW
    assert r.get("fs.write").risk_tier == RiskTier.MEDIUM
