"""S2.5 · task_machine 状态机策略层(逻辑半片)。

坐在 `TaskStateStore`(纯持久化数据层)之上的策略层:
  - retry 预算(max_retries)
  - 退避 `min(base×2^retry_count, cap) + jitter`(复刻 S1.6 `_recovery_window`,n=retry_count)
  - 僵死回收 recover_stale(stale running → failed)

无 caller/runner 接线(Scanner S2.10 / bandit 训练在 Phase2);本模块交付可独立单测的策略原语。
不动 Provider 契约(S2.4 才需要)。jitter_fn 可注入(测试确定值),贴 S1.6 测试钩子模式。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Callable, Optional

from llm_router.store.task_state import (
    InvalidStateTransitionError,
    State,
    TaskNotFoundError,
    TaskStateStore,
)


def _default_jitter(jitter_seconds: int) -> Callable[[], float]:
    def _fn() -> float:
        # secrets.randbelow(jitter+1) ∈ [0, jitter];同 S1.6,防侧信道时序
        return float(secrets.randbelow(jitter_seconds + 1))

    return _fn


@dataclass(frozen=True)
class RetryPlan:
    """plan_retry 的产出:可重试时的退避计划。"""

    delay_seconds: float  # 本次重试前应等待(含 jitter)
    next_retry_count: int  # 重试后 retry_count 将变为(=当前+1)
    next_attempt: int  # 重试后 attempts 将变为(=当前+1)


@dataclass(frozen=True)
class RecoveryReport:
    """recover_stale 的产出:僵死回收结果。"""

    marked_failed: int
    task_ids: list[str] = field(default_factory=list)
    skipped: int = 0  # 并发改态致 mark_failed 未命中,跳过不崩


class TaskMachine:
    """长任务状态机策略层(retry 预算 + 退避 + 僵死回收)。

    无状态(除注入的 config);所有决策基于 TaskStateStore 的当前行。
    """

    def __init__(
        self,
        store: TaskStateStore,
        *,
        max_retries: int = 3,
        base_backoff_seconds: int = 30,
        jitter_seconds: int = 15,
        backoff_cap_seconds: int = 300,
        jitter_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._store = store
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.jitter_seconds = jitter_seconds
        self.backoff_cap_seconds = backoff_cap_seconds
        self._jitter_fn = jitter_fn or _default_jitter(jitter_seconds)

    def _backoff(self, retry_count: int) -> float:
        """min(base × 2^retry_count, cap)+ jitter。复刻 S1.6 _recovery_window。"""
        base = float(
            min(
                self.base_backoff_seconds * (2 ** retry_count),
                self.backoff_cap_seconds,
            )
        )
        return base + self._jitter_fn()

    async def retry_budget_remaining(self, task_id: str) -> int:
        """剩余重试额度 = max(0, max_retries - retry_count)。task 不存在返 0。"""
        row = await self._store.get(task_id)
        if row is None:
            return 0
        return max(0, self.max_retries - row.retry_count)

    async def plan_retry(self, task_id: str) -> Optional[RetryPlan]:
        """failed 且 retry_count < max_retries → 返退避计划;否则 None(不可重试/预算耗尽)。
        task 不存在 → TaskNotFoundError(透传 store.get)。
        """
        row = await self._store.get(task_id)
        if row is None:
            from llm_router.store.task_state import TaskNotFoundError

            raise TaskNotFoundError(task_id)
        if row.state is not State.FAILED:
            return None
        if row.retry_count >= self.max_retries:
            return None
        return RetryPlan(
            delay_seconds=self._backoff(row.retry_count),
            next_retry_count=row.retry_count + 1,
            next_attempt=row.attempts + 1,
        )

    async def recover_stale(
        self,
        cutoff_iso: str,
        *,
        stale_error: str = "heartbeat-timeout",
    ) -> RecoveryReport:
        """僵死回收:把 store.stale_running 捞出的 running 僵死任务标 failed。

        标 failed 后任务可由 plan_retry 决策是否重试(本方法不自动重试)。
        并发改态(InvalidStateTransitionError)或行在捞起后被删(TaskNotFoundError)
        → 计 skipped 跳过,不中断其余僵死任务的回收。
        """
        stale = await self._store.stale_running(cutoff_iso)
        marked: list[str] = []
        skipped = 0
        for row in stale:
            try:
                await self._store.mark_failed(row.task_id, error=stale_error)
                marked.append(row.task_id)
            except (InvalidStateTransitionError, TaskNotFoundError):
                # 并发改态,或行在捞起与标记之间被外部删除 → 跳过,不中断循环
                skipped += 1
        return RecoveryReport(marked_failed=len(marked), task_ids=marked, skipped=skipped)
