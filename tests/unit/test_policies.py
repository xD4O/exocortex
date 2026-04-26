from __future__ import annotations

import pytest

from exocortex.coordination.policies import (
    CoordinatorPolicies,
    FallbackPolicy,
    RetryPolicy,
    TimeoutPolicy,
)


def test_defaults_preserve_phase5_behavior() -> None:
    p = CoordinatorPolicies()
    assert p.retry.max_attempts == 1  # no retry
    assert p.retry.backoff_seconds == 0.0
    assert p.timeout.per_hop_seconds is None  # no timeout
    assert p.fallback.enabled is False  # no fallback


def test_retry_policy_validation() -> None:
    RetryPolicy(max_attempts=1)
    RetryPolicy(max_attempts=5, backoff_seconds=0.1)

    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=-1)
    with pytest.raises(ValueError):
        RetryPolicy(backoff_seconds=-0.01)


def test_timeout_policy_validation() -> None:
    TimeoutPolicy()  # None is fine
    TimeoutPolicy(per_hop_seconds=0.001)

    with pytest.raises(ValueError):
        TimeoutPolicy(per_hop_seconds=0)
    with pytest.raises(ValueError):
        TimeoutPolicy(per_hop_seconds=-1)


def test_fallback_policy_validation() -> None:
    FallbackPolicy()
    FallbackPolicy(enabled=True, max_alternatives=0)  # pathological but valid
    FallbackPolicy(enabled=True, max_alternatives=5)

    with pytest.raises(ValueError):
        FallbackPolicy(max_alternatives=-1)


def test_policies_are_frozen() -> None:
    p = RetryPolicy(max_attempts=3)
    with pytest.raises((AttributeError, Exception)):
        p.max_attempts = 10  # type: ignore[misc]
