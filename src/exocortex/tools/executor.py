from __future__ import annotations

from pathlib import Path

from exocortex.contracts import (
    ApprovalRequest,
    ApprovalResolution,
    ApprovalState,
    Event,
    EventKind,
    PolicyDecisionKind,
    Provenance,
    ToolInvocation,
)
from exocortex.core.events import EventBus
from exocortex.observability.logging import get_logger
from exocortex.policy.approvals import ApprovalQueue
from exocortex.policy.engine import PolicyEngine
from exocortex.policy.rule_engine import InvocationContext
from exocortex.tools.errors import PolicyViolationError, ToolError
from exocortex.tools.registry import ToolRegistry

logger = get_logger("exocortex.tools.executor")


class ToolExecutor:
    """Orchestrates the full invocation pipeline:

        propose -> policy -> (auto-approve | approval-gate) -> execute -> record

    Every step emits a normalized event. Denied invocations are NEVER executed,
    even if the approval queue would otherwise resolve them — a deny is final.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PolicyEngine,
        bus: EventBus,
        approvals: ApprovalQueue,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._bus = bus
        self._approvals = approvals

    async def invoke(
        self,
        *,
        tool: str,
        arguments: dict[str, object],
        provenance: Provenance,
        workspace_path: Path | None,
        approval_reason: str = "",
        approval_plan_b: str = "",
        approval_timeout_seconds: int = 300,
    ) -> ToolInvocation:
        spec = self._registry.get(tool)
        inv = ToolInvocation(
            tool=tool,
            arguments=dict(arguments),
            provenance=provenance,
            workspace_ref=str(workspace_path) if workspace_path else None,
        )

        await self._bus.publish(
            Event(
                kind=EventKind.TOOL_PROPOSED,
                task_id=provenance.task_id,
                session_id=provenance.session_id,
                agent_id=provenance.agent_id,
                payload={"tool": tool, "invocation_id": str(inv.id)},
            )
        )

        ctx = InvocationContext(invocation=inv, workspace_path=workspace_path)
        decision = self._policy.evaluate_invocation(ctx)
        inv.policy_decision = decision
        inv.approval_state = ApprovalState.POLICY_CHECKED

        await self._bus.publish(
            Event(
                kind=EventKind.TOOL_POLICY_CHECKED,
                task_id=provenance.task_id,
                session_id=provenance.session_id,
                agent_id=provenance.agent_id,
                policy_decision=decision,
                payload={
                    "invocation_id": str(inv.id),
                    "decision": decision.kind.value,
                },
            )
        )

        if decision.kind == PolicyDecisionKind.DENY:
            inv.approval_state = ApprovalState.REJECTED
            await self._bus.publish(
                Event(
                    kind=EventKind.TOOL_REJECTED,
                    task_id=provenance.task_id,
                    agent_id=provenance.agent_id,
                    payload={
                        "invocation_id": str(inv.id),
                        "rule_id": decision.rule_id,
                        "reason": decision.reason,
                    },
                )
            )
            return inv

        if decision.kind == PolicyDecisionKind.REQUIRE_APPROVAL:
            request = ApprovalRequest(
                invocation_id=inv.id,
                reason_from_agent=approval_reason,
                plan_b=approval_plan_b,
                redacted_context=_redact(inv),
                allowed_duration_seconds=approval_timeout_seconds,
            )
            resolution = await self._approvals.submit(request)
            if resolution != ApprovalResolution.APPROVED:
                inv.approval_state = ApprovalState.REJECTED
                await self._bus.publish(
                    Event(
                        kind=EventKind.TOOL_REJECTED,
                        task_id=provenance.task_id,
                        agent_id=provenance.agent_id,
                        payload={
                            "invocation_id": str(inv.id),
                            "reason": f"approval resolution: {resolution.value}",
                        },
                    )
                )
                return inv
            inv.approval_state = ApprovalState.APPROVED
            await self._bus.publish(
                Event(
                    kind=EventKind.TOOL_APPROVED,
                    task_id=provenance.task_id,
                    agent_id=provenance.agent_id,
                    payload={"invocation_id": str(inv.id)},
                )
            )
        elif decision.kind == PolicyDecisionKind.ALLOW:
            inv.approval_state = ApprovalState.AUTO_APPROVED
        else:
            # DEGRADE is reserved for Phase 3.x (partial/fallback execution);
            # treat as deny for now rather than silently allow.
            inv.approval_state = ApprovalState.REJECTED
            raise PolicyViolationError(
                f"unsupported policy decision kind: {decision.kind}"
            )

        try:
            result = await spec.handler(inv.arguments)
            inv.result = result
            inv.approval_state = ApprovalState.SUCCEEDED
        except ToolError as e:
            inv.approval_state = ApprovalState.FAILED
            inv.result = {"error": type(e).__name__, "message": str(e)}
            logger.exception(
                "tool.failed",
                invocation_id=str(inv.id),
                tool=tool,
            )

        await self._bus.publish(
            Event(
                kind=EventKind.TOOL_EXECUTED,
                task_id=provenance.task_id,
                session_id=provenance.session_id,
                agent_id=provenance.agent_id,
                payload={
                    "invocation_id": str(inv.id),
                    "state": inv.approval_state.value,
                },
            )
        )
        return inv


def _redact(inv: ToolInvocation) -> str:
    args_summary = ", ".join(
        f"{k}={_short(v)}" for k, v in sorted(inv.arguments.items())
    )
    return f"{inv.tool}({args_summary})"


def _short(val: object, limit: int = 80) -> str:
    s = str(val)
    return s if len(s) <= limit else s[:limit] + "…"
