from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from exocortex.contracts import Budget
from exocortex.contracts.common import now


class BudgetExceededError(Exception):
    def __init__(self, dimension: str, used: float, limit: float) -> None:
        super().__init__(
            f"budget exceeded on {dimension}: used {used} > limit {limit}"
        )
        self.dimension = dimension
        self.used = used
        self.limit = limit


@dataclass
class BudgetUsage:
    approvals: int = 0
    tokens: int = 0
    dollars: float = 0.0
    started_at: datetime = field(default_factory=now)

    def wallclock_seconds(self) -> float:
        return (now() - self.started_at).total_seconds()


class BudgetTracker:
    """Running-total tracker enforced against Task.budget.

    Phase 5 enforces approvals + wall-clock + (optional) dollars. Tokens land
    in Phase 6 when Runners can report actual usage from LLM calls.
    """

    def __init__(self, budget: Budget) -> None:
        self._budget = budget
        self._usage = BudgetUsage()

    @property
    def usage(self) -> BudgetUsage:
        return self._usage

    def record_approval(self) -> None:
        self._usage.approvals += 1

    def record_tokens(self, n: int) -> None:
        if n < 0:
            raise ValueError("token count cannot be negative")
        self._usage.tokens += n

    def record_dollars(self, amount: float) -> None:
        if amount < 0:
            raise ValueError("dollar amount cannot be negative")
        self._usage.dollars += amount

    def check(self) -> None:
        b = self._budget
        u = self._usage
        if b.approvals_limit is not None and u.approvals > b.approvals_limit:
            raise BudgetExceededError("approvals", u.approvals, b.approvals_limit)
        if b.tokens_limit is not None and u.tokens > b.tokens_limit:
            raise BudgetExceededError("tokens", u.tokens, b.tokens_limit)
        if b.dollars_limit is not None and u.dollars > b.dollars_limit:
            raise BudgetExceededError("dollars", u.dollars, b.dollars_limit)
        if b.wallclock_seconds is not None:
            wc = u.wallclock_seconds()
            if wc > b.wallclock_seconds:
                raise BudgetExceededError(
                    "wallclock_seconds", wc, b.wallclock_seconds
                )
