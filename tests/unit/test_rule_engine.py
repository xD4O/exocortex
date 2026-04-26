from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from exocortex.contracts import (
    PolicyDecisionKind,
    Provenance,
    ToolInvocation,
)
from exocortex.policy.rule_engine import (
    DeclarativeRuleEngine,
    InvocationContext,
    default_rules,
)
from exocortex.policy.rules import Condition, ConditionKind, Rule


def _inv(tool: str, **args: object) -> ToolInvocation:
    return ToolInvocation(
        tool=tool,
        arguments=dict(args),
        provenance=Provenance(agent_id="codex", task_id=uuid4()),
    )


def test_default_outcome_is_deny() -> None:
    engine = DeclarativeRuleEngine(rules=[])
    ctx = InvocationContext(invocation=_inv("anything.at.all"))
    assert engine.evaluate_invocation(ctx).kind == PolicyDecisionKind.DENY


def test_first_matching_rule_wins() -> None:
    engine = DeclarativeRuleEngine(
        rules=[
            Rule(
                id="first",
                conditions=[Condition(kind=ConditionKind.TOOL_EQUALS, value="x")],
                outcome=PolicyDecisionKind.ALLOW,
            ),
            Rule(
                id="second",
                conditions=[Condition(kind=ConditionKind.TOOL_EQUALS, value="x")],
                outcome=PolicyDecisionKind.DENY,
            ),
        ]
    )
    d = engine.evaluate_invocation(InvocationContext(invocation=_inv("x")))
    assert d.rule_id == "first"
    assert d.kind == PolicyDecisionKind.ALLOW


def test_tool_in_and_arg_contains(tmp_path: Path) -> None:
    engine = DeclarativeRuleEngine(
        rules=[
            Rule(
                id="dangerous_shell",
                conditions=[
                    Condition(kind=ConditionKind.TOOL_EQUALS, value="shell.exec"),
                    Condition(
                        kind=ConditionKind.ARG_CONTAINS_ANY,
                        arg="argv",
                        values=["rm -rf", "sudo"],
                    ),
                ],
                outcome=PolicyDecisionKind.DENY,
            ),
            Rule(
                id="default_shell_ok",
                conditions=[Condition(kind=ConditionKind.TOOL_EQUALS, value="shell.exec")],
                outcome=PolicyDecisionKind.ALLOW,
            ),
        ]
    )
    bad = engine.evaluate_invocation(
        InvocationContext(
            invocation=_inv("shell.exec", argv=["rm -rf /"], cwd=str(tmp_path)),
            workspace_path=tmp_path,
        )
    )
    assert bad.kind == PolicyDecisionKind.DENY
    good = engine.evaluate_invocation(
        InvocationContext(
            invocation=_inv("shell.exec", argv=["echo hi"], cwd=str(tmp_path)),
            workspace_path=tmp_path,
        )
    )
    assert good.kind == PolicyDecisionKind.ALLOW


def test_path_under_workspace_respects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secrets.txt").write_text("shh")

    worktree = tmp_path / "work"
    worktree.mkdir()
    escape = worktree / "escape"
    escape.symlink_to(outside)

    rules = default_rules()
    engine = DeclarativeRuleEngine(rules=rules)

    # A read through a symlink that escapes the worktree must NOT be allowed.
    ctx = InvocationContext(
        invocation=_inv("fs.read", path=str(escape / "secrets.txt")),
        workspace_path=worktree,
    )
    assert engine.evaluate_invocation(ctx).kind == PolicyDecisionKind.DENY


def test_default_rules_allow_read_inside_worktree(tmp_path: Path) -> None:
    engine = DeclarativeRuleEngine(rules=default_rules())
    ctx = InvocationContext(
        invocation=_inv("fs.read", path=str(tmp_path / "x.txt")),
        workspace_path=tmp_path,
    )
    d = engine.evaluate_invocation(ctx)
    assert d.kind == PolicyDecisionKind.ALLOW
    assert d.rule_id == "fs.read.worktree_allow"


def test_default_rules_require_approval_for_writes_inside_worktree(tmp_path: Path) -> None:
    engine = DeclarativeRuleEngine(rules=default_rules())
    ctx = InvocationContext(
        invocation=_inv("fs.write", path=str(tmp_path / "x.txt"), content="hi"),
        workspace_path=tmp_path,
    )
    assert engine.evaluate_invocation(ctx).kind == PolicyDecisionKind.REQUIRE_APPROVAL


def test_default_rules_deny_shell_outside_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "work"
    worktree.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    engine = DeclarativeRuleEngine(rules=default_rules())
    ctx = InvocationContext(
        invocation=_inv(
            "shell.exec",
            argv=["/bin/echo", "escape"],
            cwd=str(outside),
        ),
        workspace_path=worktree,
    )
    assert engine.evaluate_invocation(ctx).kind == PolicyDecisionKind.DENY
