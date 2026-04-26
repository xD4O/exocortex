from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1  # 1 = no retry
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be >= 0")


@dataclass(frozen=True)
class TimeoutPolicy:
    # Per-hop wall-clock ceiling. None = no timeout.
    per_hop_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.per_hop_seconds is not None and self.per_hop_seconds <= 0:
            raise ValueError("per_hop_seconds must be positive")


@dataclass(frozen=True)
class FallbackPolicy:
    enabled: bool = False
    # Upper bound on how many alternatives we try before giving up.
    max_alternatives: int = 2

    def __post_init__(self) -> None:
        if self.max_alternatives < 0:
            raise ValueError("max_alternatives must be >= 0")


@dataclass(frozen=True)
class CoordinatorPolicies:
    """Reliability policies for the Coordinator. Defaults are the Phase-5
    behavior (single attempt, no timeout, no fallback) — so existing tests
    continue to pass unchanged. Opt into each policy explicitly.
    """

    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    fallback: FallbackPolicy = field(default_factory=FallbackPolicy)
