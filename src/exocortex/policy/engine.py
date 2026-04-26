from __future__ import annotations

from typing import TYPE_CHECKING

from exocortex.contracts import Event, PolicyDecision, PolicyDecisionKind

if TYPE_CHECKING:
    from exocortex.policy.rule_engine import InvocationContext


class PolicyEngine:
    """Base class + permissive default. Subclasses override evaluate_* with
    real logic. See CLAUDE-PLAN.MD §Bet D — policy is middleware, not a
    subsystem; the engine is the only place rule evaluation happens.
    """

    def __init__(self, rule_id: str = "default.allow") -> None:
        self._rule_id = rule_id

    def evaluate_event(self, event: Event) -> PolicyDecision:
        return PolicyDecision(
            kind=PolicyDecisionKind.ALLOW,
            rule_id=self._rule_id,
            reason="Base engine: allow all, log everything",
        )

    def evaluate_invocation(self, ctx: InvocationContext) -> PolicyDecision:
        return PolicyDecision(
            kind=PolicyDecisionKind.ALLOW,
            rule_id=self._rule_id,
            reason="Base engine: allow all, log everything",
        )
