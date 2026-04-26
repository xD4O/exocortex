from exocortex.tools.builtin.fs import FS_LIST_SPEC, FS_READ_SPEC, FS_WRITE_SPEC
from exocortex.tools.builtin.shell import SHELL_EXEC_SPEC
from exocortex.tools.registry import ToolRegistry


def register_builtins(registry: ToolRegistry) -> None:
    for spec in (FS_READ_SPEC, FS_WRITE_SPEC, FS_LIST_SPEC, SHELL_EXEC_SPEC):
        registry.register(spec)


__all__ = [
    "FS_LIST_SPEC",
    "FS_READ_SPEC",
    "FS_WRITE_SPEC",
    "SHELL_EXEC_SPEC",
    "register_builtins",
]
