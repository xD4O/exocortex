"""Policy-checked gateway for the ad-hoc MCP fs/shell tools (A1).

Before this existed, ``fs_read`` / ``fs_list`` / ``shell_exec`` on the MCP
surface called the filesystem and subprocess primitives directly — bypassing
the policy engine, the approval queue, the audit trail, and any workspace
confinement. That made the entire ``policy/`` package dead code on the surface
agents actually use, and contradicted the load-bearing rule "no tool executes
without a PolicyDecision."

This gateway routes those tools through the same :class:`ToolExecutor` pipeline
the dispatch path uses, so every call is:

  * policy-checked against a real deny-by-default rule set,
  * confined to a configurable sandbox root,
  * hard-denied when it touches a secret-bearing path (``.ssh`` / ``.aws`` /
    ``.env`` / private keys …) regardless of the sandbox width, and
  * recorded to the append-only audit log (proposed → policy-checked →
    executed), restoring full observability of what agents read and run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from exocortex.config import Settings
from exocortex.contracts import PolicyDecisionKind, Provenance
from exocortex.contracts.common import ApprovalState, new_id
from exocortex.core.events import EventBus
from exocortex.observability.audit import AuditLog
from exocortex.policy.approvals import (
    ApprovalQueue,
    auto_approve_resolver,
    auto_deny_resolver,
)
from exocortex.policy.rule_engine import DeclarativeRuleEngine, default_rules
from exocortex.policy.rules import Condition, ConditionKind, Rule
from exocortex.tools.builtin import register_builtins
from exocortex.tools.errors import PolicyViolationError
from exocortex.tools.executor import ToolExecutor
from exocortex.tools.registry import ToolRegistry

# Substrings that mark a path (or shell argv) as touching operator secrets.
# A first-match DENY on these fires before any sandbox allow, so widening the
# sandbox root never re-exposes them.
SECRET_PATH_PATTERNS: list[str] = [
    ".ssh",
    "id_rsa",
    "id_ed25519",
    ".aws",
    ".gnupg",
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".kube",
    "credentials",
    "secrets",
    ".pem",
    ".p12",
    "private_key",
    "id_dsa",
]

_SECRET_FLAG = re.compile(
    r"(?i)(pass(word)?|secret|token|api[-_]?key|apikey|auth|bearer|credential)"
)


def redact_argv(argv: list[str]) -> list[str]:
    """Mask secret-shaped tokens in a command line before it is persisted (A5).

    Handles the common shapes an agent might pass a secret in: ``-pHUNTER2``
    (mysql), ``--password=…`` / ``--token=…``, a secret flag followed by its
    value (``--api-key XYZ``), and inline ``Bearer <token>`` header values.
    """
    out: list[str] = []
    mask_next = False
    for arg in argv:
        if mask_next:
            out.append("«redacted»")
            mask_next = False
            continue
        if "=" in arg:
            key, _, _val = arg.partition("=")
            if _SECRET_FLAG.search(key):
                out.append(f"{key}=«redacted»")
                continue
        if re.fullmatch(r"-p\S+", arg):  # mysql -pPASSWORD
            out.append("-p«redacted»")
            continue
        if arg.startswith("-") and _SECRET_FLAG.search(arg):  # --token <value>
            out.append(arg)
            mask_next = True
            continue
        if re.search(r"(?i)bearer\s+\S+", arg):
            out.append(re.sub(r"(?i)(bearer)\s+\S+", r"\1 «redacted»", arg))
            continue
        out.append(arg)
    return out


def default_mcp_tool_rules(sandbox_root: Path) -> list[Rule]:
    """Deny secrets first, then apply the standard worktree-confinement rules
    (allow reads/lists under the sandbox, require approval for writes/shell,
    deny everything else by fall-through)."""
    return [
        Rule(
            id="fs.deny_secret_paths",
            description="Reads/lists of secret-bearing paths are denied.",
            conditions=[
                Condition(kind=ConditionKind.TOOL_IN, values=["fs.read", "fs.list"]),
                Condition(
                    kind=ConditionKind.ARG_CONTAINS_ANY,
                    arg="path",
                    values=SECRET_PATH_PATTERNS,
                ),
            ],
            outcome=PolicyDecisionKind.DENY,
        ),
        Rule(
            id="shell.deny_secret_argv",
            description="Shell commands referencing secret-bearing paths are denied.",
            conditions=[
                Condition(kind=ConditionKind.TOOL_EQUALS, value="shell.exec"),
                Condition(
                    kind=ConditionKind.ARG_CONTAINS_ANY,
                    arg="argv",
                    values=SECRET_PATH_PATTERNS,
                ),
            ],
            outcome=PolicyDecisionKind.DENY,
        ),
        *default_rules(),
    ]


class McpToolGate:
    """Wraps the ad-hoc fs/shell MCP tools in the full policy pipeline."""

    def __init__(self, *, settings: Settings, audit: AuditLog) -> None:
        self._sandbox = settings.tool_sandbox_root_resolved
        registry = ToolRegistry()
        register_builtins(registry)
        policy = DeclarativeRuleEngine(rules=default_mcp_tool_rules(self._sandbox))
        bus = EventBus(policy)
        bus.set_audit_sink(audit.record)
        resolver = (
            auto_approve_resolver
            if settings.dispatch_auto_approve_tools
            else auto_deny_resolver
        )
        approvals = ApprovalQueue(bus, resolver)
        self._executor = ToolExecutor(
            registry=registry, policy=policy, bus=bus, approvals=approvals
        )

    @property
    def sandbox_root(self) -> Path:
        return self._sandbox

    async def invoke(
        self, *, tool: str, arguments: dict[str, Any], agent_id: str
    ) -> dict[str, Any]:
        """Run ``tool`` through the policy pipeline and return its result dict.

        Raises :class:`PolicyViolationError` (surfaced to the agent as a tool
        error) when the call is denied or fails, with a message the agent can
        act on."""
        prov = Provenance(agent_id=agent_id or "external", task_id=new_id())
        inv = await self._executor.invoke(
            tool=tool,
            arguments=arguments,
            provenance=prov,
            workspace_path=self._sandbox,
            approval_reason=f"MCP {tool} from {agent_id or 'external'}",
        )
        decision = inv.policy_decision
        if inv.approval_state == ApprovalState.REJECTED:
            reason = decision.reason if decision else "denied by policy"
            if decision and decision.kind == PolicyDecisionKind.DENY:
                raise PolicyViolationError(
                    f"{tool} denied: {reason}. Calls are confined to the sandbox "
                    f"root {self._sandbox} and secret-bearing paths are blocked. "
                    f"Set EXOCORTEX_TOOL_SANDBOX_ROOT to widen access."
                )
            raise PolicyViolationError(f"{tool} not approved: {reason}")
        if inv.approval_state == ApprovalState.FAILED:
            err = inv.result or {}
            raise PolicyViolationError(
                f"{tool} failed: {err.get('message', 'unknown error')}"
            )
        return inv.result or {}
