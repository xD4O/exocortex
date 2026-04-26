from __future__ import annotations

from datetime import timedelta

import pytest

from exocortex.contracts import Budget
from exocortex.contracts.common import now
from exocortex.coordination.budget import BudgetExceededError, BudgetTracker


def test_check_ok_under_limits() -> None:
    t = BudgetTracker(
        Budget(approvals_limit=5, tokens_limit=1000, dollars_limit=10.0)
    )
    t.record_approval()
    t.record_tokens(500)
    t.record_dollars(3.0)
    t.check()  # no raise


def test_approvals_limit_enforced() -> None:
    t = BudgetTracker(Budget(approvals_limit=2))
    t.record_approval()
    t.record_approval()
    t.check()  # still ok at limit
    t.record_approval()
    with pytest.raises(BudgetExceededError) as ei:
        t.check()
    assert ei.value.dimension == "approvals"


def test_tokens_limit_enforced() -> None:
    t = BudgetTracker(Budget(tokens_limit=100))
    t.record_tokens(101)
    with pytest.raises(BudgetExceededError) as ei:
        t.check()
    assert ei.value.dimension == "tokens"


def test_dollars_limit_enforced() -> None:
    t = BudgetTracker(Budget(dollars_limit=1.0))
    t.record_dollars(1.5)
    with pytest.raises(BudgetExceededError) as ei:
        t.check()
    assert ei.value.dimension == "dollars"


def test_wallclock_limit_enforced() -> None:
    t = BudgetTracker(Budget(wallclock_seconds=60))
    # Backdate the start to force an over-budget read.
    t.usage.started_at = now() - timedelta(seconds=120)
    with pytest.raises(BudgetExceededError) as ei:
        t.check()
    assert ei.value.dimension == "wallclock_seconds"


def test_negative_values_rejected() -> None:
    t = BudgetTracker(Budget())
    with pytest.raises(ValueError):
        t.record_tokens(-1)
    with pytest.raises(ValueError):
        t.record_dollars(-0.1)


def test_no_limit_never_exceeds() -> None:
    t = BudgetTracker(Budget())  # all limits None
    for _ in range(1000):
        t.record_approval()
        t.record_tokens(10)
        t.record_dollars(1.0)
    t.check()  # no raise
