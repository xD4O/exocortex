from __future__ import annotations

from pathlib import Path
from typing import Any

from exocortex.tools.errors import ToolArgumentError
from exocortex.tools.spec import RiskTier, ToolCategory, ToolSpec


async def _fs_read(args: dict[str, Any]) -> dict[str, Any]:
    path = args.get("path")
    if not isinstance(path, str):
        raise ToolArgumentError("fs.read requires 'path' (string)")
    p = Path(path)
    return {"path": str(p), "content": p.read_text(encoding="utf-8")}


async def _fs_write(args: dict[str, Any]) -> dict[str, Any]:
    path = args.get("path")
    content = args.get("content")
    if not isinstance(path, str):
        raise ToolArgumentError("fs.write requires 'path' (string)")
    if not isinstance(content, str):
        raise ToolArgumentError("fs.write requires 'content' (string)")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": str(p), "bytes_written": len(content.encode("utf-8"))}


async def _fs_list(args: dict[str, Any]) -> dict[str, Any]:
    path = args.get("path")
    if not isinstance(path, str):
        raise ToolArgumentError("fs.list requires 'path' (string)")
    p = Path(path)
    return {"path": str(p), "entries": sorted(e.name for e in p.iterdir())}


FS_READ_SPEC = ToolSpec(
    name="fs.read",
    description="Read a text file and return its contents.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    category=ToolCategory.FS,
    risk_tier=RiskTier.LOW,
    handler=_fs_read,
)

FS_WRITE_SPEC = ToolSpec(
    name="fs.write",
    description="Write text to a file (creates parent dirs).",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    category=ToolCategory.FS,
    risk_tier=RiskTier.MEDIUM,
    handler=_fs_write,
)

FS_LIST_SPEC = ToolSpec(
    name="fs.list",
    description="List entries in a directory.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    category=ToolCategory.FS,
    risk_tier=RiskTier.LOW,
    handler=_fs_list,
)
