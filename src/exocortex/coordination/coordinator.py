from __future__ import annotations

import asyncio
from pathlib import Path

from exocortex.agents.bridge import Bridge
from exocortex.contracts import Handoff, Task, TaskStatus
from exocortex.coordination.budget import BudgetTracker
from exocortex.coordination.merge_gate import MergeGate
from exocortex.coordination.policies import CoordinatorPolicies, TimeoutPolicy
from exocortex.coordination.router import (
    AgentRegistration,
    CapabilityRouter,
    NoSuitableAgentError,
)
from exocortex.coordination.worktree import WorktreeManager
from exocortex.core.task_manager import TaskManager
from exocortex.observability.logging import get_logger

logger = get_logger("exocortex.coordinator")


class CoordinatorError(Exception):
    pass


class Coordinator:
    """Glues router + worktree + budget + merge-gate + task FSM. Drives a task
    through agent assignments and handoffs to completion, honoring the
    per-hop retry / timeout / fallback policies.

    Each agent hop gets a fresh Bridge instance via the router's bridge
    factory, sharing only the per-task worktree — no bridge state leaks
    between agents.
    """

    def __init__(
        self,
        *,
        router: CapabilityRouter,
        worktree_manager: WorktreeManager,
        merge_gate: MergeGate,
        task_manager: TaskManager,
        policies: CoordinatorPolicies | None = None,
        max_handoffs: int = 10,
    ) -> None:
        self._router = router
        self._worktree_mgr = worktree_manager
        self._merge_gate = merge_gate
        self._tasks = task_manager
        self._policies = policies or CoordinatorPolicies()
        self._max_handoffs = max_handoffs

    async def submit(self, task: Task) -> Task:
        worktree = await self._worktree_mgr.create(task.id)
        budget = BudgetTracker(task.budget)

        await self._tasks.transition(task.id, TaskStatus.ROUTED)
        await self._tasks.transition(task.id, TaskStatus.IN_PROGRESS)

        required_raw = task.inputs.get("required_capabilities", [])
        required_caps: set[str] = (
            {str(c) for c in required_raw}
            if isinstance(required_raw, list)
            else set()
        )

        handoff_in: Handoff | None = None
        current = self._router.route(task)

        final_handoff: Handoff | None = None
        last_agent: str = current.agent_id

        for hop in range(self._max_handoffs + 1):
            last_agent = current.agent_id
            logger.info(
                "coordinator.hop",
                task_id=str(task.id),
                hop=hop,
                agent=current.agent_id,
            )

            try:
                handoff_out = await self._run_hop_with_retry(
                    current, task, handoff_in, worktree
                )
            except Exception as exc:
                handled = await self._try_fallback(
                    failed=current,
                    task=task,
                    required_caps=required_caps,
                    tried_ids={current.agent_id},
                    handoff_in=handoff_in,
                    worktree=worktree,
                )
                if handled is None:
                    await self._tasks.transition(task.id, TaskStatus.FAILED)
                    raise CoordinatorError(
                        f"agent {current.agent_id!r} failed and no fallback "
                        f"available: {type(exc).__name__}: {exc}"
                    ) from exc
                handoff_out, current = handled

            final_handoff = handoff_out
            budget.check()

            if not handoff_out.to_agent:
                break

            try:
                next_reg = self._router.resolve(handoff_out.to_agent)
            except NoSuitableAgentError:
                await self._tasks.transition(task.id, TaskStatus.FAILED)
                raise CoordinatorError(
                    f"handoff target {handoff_out.to_agent!r} not registered"
                ) from None

            await self._tasks.transition(task.id, TaskStatus.AWAITING_HANDOFF)
            await self._tasks.transition(task.id, TaskStatus.IN_PROGRESS)

            handoff_in = handoff_out
            current = next_reg
        else:
            await self._tasks.transition(task.id, TaskStatus.FAILED)
            raise CoordinatorError(
                f"task {task.id} exceeded max_handoffs={self._max_handoffs}"
            )

        review = await self._merge_gate.request(
            task_id=task.id,
            worktree_path=str(worktree),
            from_agent=last_agent,
            summary=(
                final_handoff.goal_restatement
                if final_handoff is not None
                else task.goal
            ),
        )
        await self._merge_gate.resolve(review.id, accepted=True)

        await self._tasks.transition(task.id, TaskStatus.COMPLETED)
        return self._tasks.get(task.id)

    async def _run_hop_with_retry(
        self,
        reg: AgentRegistration,
        task: Task,
        handoff_in: Handoff | None,
        worktree: Path,
    ) -> Handoff:
        retry = self._policies.retry
        timeout = self._policies.timeout

        last_exc: Exception | None = None
        for attempt in range(1, retry.max_attempts + 1):
            bridge: Bridge = reg.bridge_factory(worktree)
            try:
                return await self._run_bridge(bridge, task, handoff_in, timeout)
            except Exception as e:
                last_exc = e
                logger.warning(
                    "coordinator.hop_failed",
                    agent=reg.agent_id,
                    attempt=attempt,
                    max_attempts=retry.max_attempts,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                if attempt < retry.max_attempts:
                    if retry.backoff_seconds > 0:
                        await asyncio.sleep(retry.backoff_seconds)
                    continue
        assert last_exc is not None
        raise last_exc

    async def _run_bridge(
        self,
        bridge: Bridge,
        task: Task,
        handoff_in: Handoff | None,
        timeout: TimeoutPolicy,
    ) -> Handoff:
        coro = bridge.run_task(task, handoff_in=handoff_in)
        if timeout.per_hop_seconds is None:
            return await coro
        try:
            return await asyncio.wait_for(coro, timeout.per_hop_seconds)
        except TimeoutError:
            logger.warning(
                "coordinator.hop_timeout",
                agent=bridge.agent_id,
                limit_seconds=timeout.per_hop_seconds,
            )
            # Best-effort kill of the dangling process; raise the timeout up.
            try:
                await bridge.kill()
            except Exception:  # pragma: no cover - cleanup best-effort
                logger.exception("coordinator.kill_on_timeout_failed")
            raise

    async def _try_fallback(
        self,
        *,
        failed: AgentRegistration,
        task: Task,
        required_caps: set[str],
        tried_ids: set[str],
        handoff_in: Handoff | None,
        worktree: Path,
    ) -> tuple[Handoff, AgentRegistration] | None:
        policy = self._policies.fallback
        if not policy.enabled:
            return None

        alternatives_tried = 0
        excluded = set(tried_ids)

        while alternatives_tried < policy.max_alternatives:
            alternative = self._router.find_fallback(
                exclude_ids=excluded, required=required_caps or None
            )
            if alternative is None:
                return None

            alternatives_tried += 1
            excluded.add(alternative.agent_id)
            logger.warning(
                "coordinator.fallback",
                from_agent=failed.agent_id,
                to_agent=alternative.agent_id,
                attempt=alternatives_tried,
                max_alternatives=policy.max_alternatives,
            )
            try:
                handoff_out = await self._run_hop_with_retry(
                    alternative, task, handoff_in, worktree
                )
                return handoff_out, alternative
            except Exception as e:
                logger.warning(
                    "coordinator.fallback_failed",
                    agent=alternative.agent_id,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                continue

        return None
