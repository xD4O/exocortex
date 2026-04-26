from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from exocortex.contracts import (
    Event,
    PolicyDecision,
    PolicyDecisionKind,
    ToolInvocation,
)
from exocortex.policy.engine import PolicyEngine
from exocortex.policy.rules import Condition, ConditionKind, Rule


@dataclass(frozen=True)
class InvocationContext:
    invocation: ToolInvocation
    workspace_path: Path | None = None


def _is_under(candidate: str, root: Path) -> bool:
    """Symlink-safe prefix check via realpath resolution."""
    try:
        c = Path(candidate).expanduser().resolve(strict=False)
        r = root.expanduser().resolve(strict=False)
        c.relative_to(r)
        return True
    except (ValueError, RuntimeError, OSError):
        return False


def _eval_condition(cond: Condition, ctx: InvocationContext) -> bool:  # noqa: PLR0911
    inv = ctx.invocation
    match cond.kind:
        case ConditionKind.TOOL_EQUALS:
            return inv.tool == cond.value
        case ConditionKind.TOOL_IN:
            return inv.tool in set(cond.values)
        case ConditionKind.ARG_EQUALS:
            return cond.arg is not None and inv.arguments.get(cond.arg) == cond.value
        case ConditionKind.ARG_CONTAINS_ANY:
            if cond.arg is None:
                return False
            raw = inv.arguments.get(cond.arg)
            hay = raw if isinstance(raw, str) else " ".join(map(str, raw or []))
            return any(needle in hay for needle in cond.values)
        case ConditionKind.PATH_ARG_UNDER_WORKSPACE:
            if ctx.workspace_path is None or cond.arg is None:
                return False
            path = inv.arguments.get(cond.arg)
            return isinstance(path, str) and _is_under(path, ctx.workspace_path)
        case ConditionKind.CWD_UNDER_WORKSPACE:
            if ctx.workspace_path is None:
                return False
            cwd = inv.arguments.get("cwd")
            return isinstance(cwd, str) and _is_under(cwd, ctx.workspace_path)
    return False


def _rule_matches(rule: Rule, ctx: InvocationContext) -> bool:
    return all(_eval_condition(c, ctx) for c in rule.conditions)


class DeclarativeRuleEngine(PolicyEngine):
    """First-match-wins rule engine. Default outcome is DENY so a missing rule
    cannot silently allow something — deny-by-default closes the R2
    composition-escape surface by shrinking the implicit-allow set.

    Phase 3.x can swap this for a Cedar- or OPA-backed engine; the
    PolicyEngine interface is the stable seam.
    """

    def __init__(
        self,
        rules: list[Rule],
        *,
        default_outcome: PolicyDecisionKind = PolicyDecisionKind.DENY,
    ) -> None:
        self._rules = list(rules)
        self._default = default_outcome

    def evaluate_event(self, event: Event) -> PolicyDecision:
        return PolicyDecision(
            kind=PolicyDecisionKind.ALLOW,
            rule_id="rule_engine.event.allow_all",
            reason="Event-level policy is pass-through in Phase 3",
        )

    def evaluate_invocation(self, ctx: InvocationContext) -> PolicyDecision:
        for rule in self._rules:
            if _rule_matches(rule, ctx):
                return PolicyDecision(
                    kind=rule.outcome,
                    rule_id=rule.id,
                    reason=rule.description or f"matched rule {rule.id}",
                )
        return PolicyDecision(
            kind=self._default,
            rule_id="rule_engine.default",
            reason="no matching rule; default outcome applies",
        )


def default_rules() -> list[Rule]:
    """Worktree-confinement defaults. Anything outside is denied by fall-through."""
    return [
        Rule(
            id="fs.read.worktree_allow",
            description="Reads inside the task worktree are auto-approved.",
            conditions=[
                Condition(kind=ConditionKind.TOOL_EQUALS, value="fs.read"),
                Condition(
                    kind=ConditionKind.PATH_ARG_UNDER_WORKSPACE, arg="path"
                ),
            ],
            outcome=PolicyDecisionKind.ALLOW,
        ),
        Rule(
            id="fs.list.worktree_allow",
            description="Listings inside the task worktree are auto-approved.",
            conditions=[
                Condition(kind=ConditionKind.TOOL_EQUALS, value="fs.list"),
                Condition(
                    kind=ConditionKind.PATH_ARG_UNDER_WORKSPACE, arg="path"
                ),
            ],
            outcome=PolicyDecisionKind.ALLOW,
        ),
        Rule(
            id="fs.write.worktree_require_approval",
            description="Writes inside the task worktree require operator approval.",
            conditions=[
                Condition(kind=ConditionKind.TOOL_EQUALS, value="fs.write"),
                Condition(
                    kind=ConditionKind.PATH_ARG_UNDER_WORKSPACE, arg="path"
                ),
            ],
            outcome=PolicyDecisionKind.REQUIRE_APPROVAL,
        ),
        Rule(
            id="shell.exec.worktree_require_approval",
            description="Shell exec with cwd in the worktree requires approval.",
            conditions=[
                Condition(kind=ConditionKind.TOOL_EQUALS, value="shell.exec"),
                Condition(kind=ConditionKind.CWD_UNDER_WORKSPACE),
            ],
            outcome=PolicyDecisionKind.REQUIRE_APPROVAL,
        ),
    ]
